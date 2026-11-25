import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from bcc.ws_sharded_quotes import (ShardedBybitQuoteStream, StablePair, SHARDS,
                                    RECOVERY_TOPICS_PER_SHARD, RETRY_INTERVAL)
from bcc.ws_ticker_demand import renew_tickers, KLINE, TICKER
from bcc.ws_quotes import StreamUnavailable


class Pair:
    def __init__(self, **kwargs):
        self.names=set();self.priority=set();self.mark_value={};self.stopped=False
        self.fail=False;self.calls=[]
    def sync_symbols(self,names):
        if self.fail:raise StreamUnavailable('injected shard disconnect')
        self.names=set(names)
    def set_priority_symbols(self,names):self.priority=set(names)
    def independent_observations_for_hub(self,s):return self.mark_value.get(s),(None)
    def snapshot_for_hub(self,s):return None,None,None
    def refresh_stale_marks(self,names):self.calls.append(set(names));return 0
    def stop(self):self.stopped=True


def test_500_symbols_stable_shards_no_position_cap():
    s=ShardedBybitQuoteStream(pair_factory=Pair)
    names={f'C{i}USDT' for i in range(500)}
    assert s.sync_symbols(names)==0
    assert len(s._pairs)==SHARDS
    assert set.union(*(p.names for p in s._pairs))==names
    assert sum(len(p.names) for p in s._pairs)==500
    assert min(len(p.names) for p in s._pairs)>100
    old={n:i for i,p in enumerate(s._pairs) for n in p.names}
    s.set_priority_symbols(sorted(names)[:30])
    s.subscribe('BTCUSDT')
    assert all(s._index(n)==i and n in s._pairs[i].names for n,i in old.items())
    s.sync_symbols(names-{'C10USDT'})
    assert s.independent_observations_for_hub('C10USDT')==(None,None)
    assert sum(len(p.priority) for p in s._pairs)==30
    s.stop();assert all(p.stopped for p in s._pairs)


def test_shard_failure_does_not_prevent_other_subscription_updates():
    s=ShardedBybitQuoteStream(pair_factory=Pair)
    s._pairs[0].fail=True
    names={f'C{i}USDT' for i in range(50)}
    s.sync_symbols(names)
    for i,p in enumerate(s._pairs[1:],1):
        assert p.names=={n for n in names if s._index(n)==i}
    s._pairs[0].fail=False
    s.sync_symbols(names)
    assert set.union(*(p.names for p in s._pairs))==names
    s.stop()


def test_unsubscribed_symbol_cannot_publish_cached_pair_evidence():
    s=ShardedBybitQuoteStream(pair_factory=Pair)
    s.sync_symbols(['BTCUSDT'])
    p=s._pairs[s._index('BTCUSDT')]
    mark=(100.,123.,'a'*32,123000,1,1)
    p.mark_value['BTCUSDT']=mark
    assert s.independent_observations_for_hub('BTCUSDT')[0]==mark
    s.sync_symbols([])
    assert s.independent_observations_for_hub('BTCUSDT')==(None,None)
    with pytest.raises(StreamUnavailable):s.mark('BTCUSDT')
    s.stop()


def isolated_pair():
    now=[100.];calls=[];marks={};names={f'C{i}USDT' for i in range(25)}
    p=object.__new__(StablePair)
    p._lock=threading.RLock();p._mono=lambda:now[0]
    p._demand=set(names);p._priority={'C20USDT'};p._recovery_demand=set(names)
    p._missing_since={};p._retry_at={};p._recovery_attempts=0;p._deferred=0
    def refresh(names,*,max_topics):
        assert len(names)<=max_topics==RECOVERY_TOPICS_PER_SHARD
        calls.append(list(names));return len(names)
    p._recovery=SimpleNamespace(_lock=threading.RLock(),_wire_symbols=set(),refresh_stale_marks=refresh)
    p.independent_observations_for_hub=lambda s:(marks.get(s),None)
    return p,now,calls,marks,names


def test_recovery_requires_continuous_both_lane_gap_and_bounds_requests():
    p,now,calls,marks,names=isolated_pair()
    assert p.refresh_stale_marks(names)==0
    now[0]+=1.99;assert p.refresh_stale_marks(names)==0
    now[0]+=.02
    assert p.refresh_stale_marks(names)==4
    first=set(calls[-1]);assert 'C20USDT' in first
    for i in range(10):
        p.refresh_stale_marks(names)
    assert sum(map(len,calls))==25
    assert len(set(n for call in calls for n in call))==25
    now[0]+=RETRY_INTERVAL-.01
    assert p.refresh_stale_marks(names)==0
    now[0]+=.02;assert p.refresh_stale_marks(names)==4


def test_fresh_observation_resets_gap_not_original_retry_budget():
    p,now,calls,marks,names=isolated_pair();s='C20USDT'
    p.refresh_stale_marks([s]);now[0]+=2.1
    assert p.refresh_stale_marks([s])==1
    now[0]+=.1;marks[s]=(100.,now[0],'fence')
    assert p.refresh_stale_marks([s])==0
    marks.clear();now[0]+=.1
    assert p.refresh_stale_marks([s])==0
    now[0]+=2.1;assert p.refresh_stale_marks([s])==0
    now[0]+=RETRY_INTERVAL;assert p.refresh_stale_marks([s])==1


def test_pending_wire_topic_is_not_rearmed():
    p,now,calls,marks,names=isolated_pair();s='C20USDT'
    p._recovery._wire_symbols.add(s)
    p.refresh_stale_marks([s]);now[0]+=5.
    assert p.refresh_stale_marks([s])==0
    assert calls[-1]==[]  # existing control timeouts still checked
    p._recovery._wire_symbols.clear()
    assert p.refresh_stale_marks([s])==1


def identity(url,params):
    return (hashlib.sha256(json.dumps([url,params],sort_keys=True,allow_nan=False).encode()).hexdigest(),
            json.dumps(params,sort_keys=True,separators=(',',':')))


@pytest.fixture
def db(tmp_path):
    d=sqlite3.connect(tmp_path/'registry.sqlite');d.execute('PRAGMA journal_mode=WAL')
    d.executescript('''CREATE TABLE hub_leader(singleton INT,owner TEXT,lease_token TEXT,expires REAL,boot_epoch REAL);
        INSERT INTO hub_leader VALUES(1,'owner','token',200,1000);
        CREATE TABLE hub_subscriptions(k TEXT PRIMARY KEY,url TEXT,params TEXT,active_until REAL,
          next_due REAL,priority_until REAL,subscription_token TEXT,last_error TEXT DEFAULT '');''')
    yield d;d.close()


def add(db,symbol='BTCUSDT',until=180,priority=160,params=None,url=KLINE,key=None):
    p=params if params is not None else dict(category='linear',symbol=symbol,interval='1',limit=10)
    k,raw=identity(url,p)
    db.execute('INSERT INTO hub_subscriptions VALUES(?,?,?,?,?,?,?,?)',
               (key or k,url,raw,until,77,priority,'old',''));db.commit()


def renew(db,now=100,token='token',boot=1000):
    return renew_tickers(db,now=now,owner='owner',token=token,boot=boot,identity=identity)


def test_500_derived_ticker_leases_never_extend_parent_or_rotate_active_tokens(db):
    for i in range(500):add(db,f'C{i}USDT',until=150+i/100,priority=125)
    assert renew(db)==500
    rows=db.execute('SELECT k,active_until,priority_until,subscription_token FROM hub_subscriptions WHERE url=?',(TICKER,)).fetchall()
    assert len(rows)==500 and min(r[1] for r in rows)==150 and max(r[1] for r in rows)==154.99
    assert all(r[2]==125 for r in rows)
    assert renew(db)==0
    assert db.execute('SELECT k,active_until,priority_until,subscription_token FROM hub_subscriptions WHERE url=?',(TICKER,)).fetchall()==rows
    assert renew(db,now=190)==0


@pytest.mark.parametrize('kind',['expired','historic','wrong_symbol','wrong_category','wrong_interval','wrong_limit','wrong_key'])
def test_invalid_or_inactive_parent_never_derives_demand(db,kind):
    p=dict(category='linear',symbol='BTCUSDT',interval='1',limit=10)
    until=180;key=None
    if kind=='expired':until=99
    elif kind=='historic':p['end']=123456
    elif kind=='wrong_symbol':p['symbol']='invalid'
    elif kind=='wrong_category':p['category']='spot'
    elif kind=='wrong_interval':p['interval']='5'
    elif kind=='wrong_limit':p['limit']=100
    else:key='forged'
    add(db,until=until,params=p,key=key)
    assert renew(db)==0
    assert db.execute('SELECT count(*) FROM hub_subscriptions WHERE url=?',(TICKER,)).fetchone()[0]==0


def test_fenced_leadership_and_preserve_explicit_existing_longer_demand(db):
    add(db)
    add(db,until=190,priority=175,params=dict(category='linear',symbol='BTCUSDT'),url=TICKER)
    assert renew(db,token='stale') is None
    assert renew(db,boot=2000) is None
    assert renew(db)==0
    assert db.execute('SELECT active_until,next_due,priority_until,subscription_token FROM hub_subscriptions WHERE url=?',(TICKER,)).fetchone()==(190,77,175,'old')


def test_slow_control_command_does_not_block_reading_another_shard():
    s=ShardedBybitQuoteStream(pair_factory=Pair)
    names=[f'C{i}USDT' for i in range(25)]
    s.sync_symbols(names)
    slow=s._pairs[0]
    symbol=next(n for n in names if s._index(n)==1)
    mark=(100.,123.,'a'*32,123000,1,1)
    s._pairs[1].mark_value[symbol]=mark
    entered,release,done=threading.Event(),threading.Event(),threading.Event()
    original=slow.sync_symbols
    def blocked(names):
        entered.set()
        assert release.wait(3)
        original(names)
    slow.sync_symbols=blocked
    control=threading.Thread(target=lambda:s.sync_symbols(names))
    result=[]
    reader=threading.Thread(target=lambda:(result.append(s.independent_observations_for_hub(symbol)[0]),done.set()))
    try:
        control.start();assert entered.wait(1)
        reader.start();assert done.wait(1)
        assert result==[mark]
    finally:
        release.set();control.join(3);reader.join(3);s.stop()
