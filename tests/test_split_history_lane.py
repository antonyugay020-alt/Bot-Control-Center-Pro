"""Real SQLite registry and independent bounded producer execution."""
import sqlite3
import threading
import time
from bcc.market import PublicData
from bcc.market_hub import MarketDataHub

BASE='https://api.bybit.com/v5/market/'
KLINE=BASE+'kline'
RISK=BASE+'risk-limit'
INSTRUMENT=BASE+'instruments-info'


def test_history_and_metadata_progress_independently(tmp_path, monkeypatch):
    source=PublicData()
    hub=MarketDataHub(tmp_path/'cache.sqlite',source=source,max_workers=6)
    hub.enable_split_history()
    release=threading.Event()
    lock=threading.Lock()
    started=set()
    risk=set()

    def get(url,params=None):
        symbol=params['symbol']
        if url==KLINE:
            with lock:started.add(symbol)
            assert release.wait(12)
        else:
            with lock:risk.add(symbol)
        return {'retCode':0}

    monkeypatch.setattr(source,'get',get)
    try:
        for i in range(20):
            hub.registry.subscribe(KLINE,dict(category='linear',symbol=f'H{i}USDT',
                interval='1',limit=100),priority=i<19)
            hub.registry.subscribe(RISK,dict(category='linear',symbol=f'R{i}USDT'),priority=True)
        deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            hub.poll_nonblocking()
            with lock:
                if len(started)==3 and len(risk)>=6:break
            time.sleep(.01)
        with lock:
            assert len(started)==3 and 'H19USDT' in started
            assert len(risk)>=6
        assert len(hub._inflight)<=6
    finally:
        release.set()
        hub.close()


def test_metadata_cache_hits_do_not_take_writer_or_repeat_each_second(tmp_path, monkeypatch):
    source=PublicData()
    hub=MarketDataHub(tmp_path/'cache.sqlite',source=source,max_workers=6)
    hub.enable_split_history()
    params={'category':'linear','symbol':'BTCUSDT'}
    try:
        key,owner,_=source.shared.begin(INSTRUMENT,params,'api.bybit.com')
        assert source.shared.finish(key,owner,'api.bybit.com',{'retCode':0},ttl=3600)
        assert 3595<=hub._refresh_delay(INSTRUMENT,params)<=3600
        def forbidden():
            raise AssertionError('Cache hit acquired the WS writer guard')
        monkeypatch.setattr(source.shared,'db',forbidden)
        _,owner,value=source.shared.begin(INSTRUMENT,params,'api.bybit.com')
        assert owner is None and value=={'retCode':0}
    finally:
        hub.close()


def test_failed_sql_ack_retains_finished_future_until_retry(tmp_path, monkeypatch):
    source=PublicData()
    hub=MarketDataHub(tmp_path/'cache.sqlite',source=source,max_workers=6)
    hub.enable_split_history()
    calls=[]
    params={'category':'linear','symbol':'BTCUSDT','interval':'1','limit':100}
    key=hub.registry.subscribe(KLINE,params)
    monkeypatch.setattr(source,'get',lambda *a,**k:calls.append(1))
    real=hub._acknowledge
    def fail(*args,**kwargs):
        raise sqlite3.OperationalError('database is locked')
    try:
        hub.poll_nonblocking()
        hub._inflight[key][0].result(timeout=2)
        monkeypatch.setattr(hub,'_acknowledge',fail)
        try:
            hub.poll_nonblocking()
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError('Synthetic lock was not exercised')
        assert key in hub._inflight and calls==[1]
        monkeypatch.setattr(hub,'_acknowledge',real)
        assert (key,True) in hub.poll_nonblocking()
        assert key not in hub._inflight and calls==[1]
    finally:
        hub.close()


def test_due_snapshot_prevents_duplicate_on_concurrent_priority_promotion(tmp_path, monkeypatch):
    """Force a real concurrent WAL update between regular SELECT and OFFSET."""
    from contextlib import contextmanager
    from types import SimpleNamespace
    from bcc.market_hub import HubRegistry

    path=tmp_path/'cache.sqlite'
    reader=HubRegistry(path)
    writer=HubRegistry(path)
    a=dict(category='linear',symbol='AUSDT',interval='1',limit=100)
    b=dict(category='linear',symbol='BUSDT',interval='1',limit=100)
    key_a=reader.subscribe(KLINE,a,priority=True)
    key_b=reader.subscribe(KLINE,b,priority=False)
    original=reader._connect
    with original() as db:
        db.execute('UPDATE hub_subscriptions SET next_due=1 WHERE k=?',(key_a,))
        db.execute('UPDATE hub_subscriptions SET next_due=2 WHERE k=?',(key_b,))
    promoted=[]
    class InterleavingConnection:
        def __init__(self, db):
            self.db=db
        def __getattr__(self, name):
            return getattr(self.db,name)
        def execute(self, sql, *args):
            cursor=self.db.execute(sql,*args)
            if ('SELECT k,url,params,subscription_token' in sql
                    and 'priority_until<=?' in sql and not promoted):
                rows=cursor.fetchall()
                writer.subscribe(KLINE,b,priority=True)
                promoted.append(True)
                return SimpleNamespace(fetchall=lambda:rows)
            return cursor
    @contextmanager
    def interleaving():
        with original() as db:
            yield InterleavingConnection(db)
    monkeypatch.setattr(reader,'_connect',interleaving)
    rows=reader.due(limit=3,reserve_regular=2,include_priority=True,
                    include_generation=True,history_lane=True)
    assert promoted==[True]
    assert [row[0] for row in rows]==[key_a,key_b]
    assert [row[3] for row in rows]==[True,False]
    subsequent=reader.due(limit=3,reserve_regular=2,include_priority=True,
                          include_generation=True,history_lane=True)
    assert len(subsequent)==2
    assert {row[0] for row in subsequent}=={key_a,key_b}
    assert all(row[3] for row in subsequent)


def test_duplicate_rest_demand_retains_original_future_without_stopping_hub(tmp_path,monkeypatch,caplog):
    from bcc.hub_rest_lanes import admit_rest_lanes
    source=PublicData()
    hub=MarketDataHub(tmp_path/'cache.sqlite',source=source,max_workers=6)
    hub.enable_split_history()
    release=threading.Event()
    params={'category':'linear','symbol':'BTCUSDT','interval':'1','limit':100}
    key=hub.registry.subscribe(KLINE,params)
    assert hub.acquire_leadership()
    rows=hub.registry.due(limit=3,include_priority=True,
                          include_generation=True,history_lane=True)
    assert len(rows)==1
    calls=[]
    def get(*args,**kwargs):
        calls.append(1)
        assert release.wait(5)
    monkeypatch.setattr(source,'get',get)
    monkeypatch.setattr(hub.registry,'due',
                        lambda **kw: rows*2 if kw['history_lane'] else [])
    try:
        admit_rest_lanes(hub)
        assert list(hub._inflight)==[key]
        assert 'Duplicate REST demand skipped' in caplog.text
        release.set()
        hub._inflight[key][0].result(timeout=2)
        assert calls==[1]
    finally:
        release.set()
        hub.close()


def test_canonical_repairs_have_capacity_without_starving_bootstrap(tmp_path, monkeypatch):
    from bcc.hub_minute_repair import params_for
    from bcc.hub_rest_lanes import admit_rest_lanes
    source=PublicData()
    hub=MarketDataHub(tmp_path/'cache.sqlite',source=source,max_workers=6)
    hub.enable_split_history()
    source.shared.central_repair=True
    release=threading.Event()
    def get(*a, **kw):
        assert release.wait(5)
    monkeypatch.setattr(source,'get',get)
    try:
        assert hub.acquire_leadership()
        repair_keys={hub.registry.subscribe(KLINE,params_for(f'R{i}USDT',60000000))
                     for i in range(8)}
        ordinary_keys={hub.registry.subscribe(KLINE,dict(category='linear',
            symbol=f'H{i}USDT',interval='1',limit=100),priority=True)
            for i in range(8)}
        assert set(row[0] for row in hub.registry.due(limit=10,history_lane=True,repair_lane=True))==repair_keys
        assert set(row[0] for row in hub.registry.due(limit=10,history_lane=True,repair_lane=False))==ordinary_keys
        admit_rest_lanes(hub)
        assert len(hub._inflight)==3
        assert len(set(hub._inflight)&repair_keys)==2
        assert len(set(hub._inflight)&ordinary_keys)==1
    finally:
        release.set()
        hub.close()
