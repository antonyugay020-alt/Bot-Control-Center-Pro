"""Deadline retention is original-source bounded, even after OS time changes."""
import time
from types import SimpleNamespace

import pytest

from bcc.data_cache import PublicCache
from bcc.market_hub import HubRegistry
from bcc import ws_source_deadline as policy
from bcc import data_cache

HOST='api.bybit.com'
MARK='https://api.bybit.com/v5/market/tickers'
BOOK='https://api.bybit.com/v5/market/orderbook'


def ticker(at, *, quote=False, quote_at=None):
    row={'symbol':'BTCUSDT','markPrice':'100.5'}
    if quote:
        row.update(bid1Price='100',ask1Price='101')
    return dict(retCode=0,_bcc_ws_mark=True,_bcc_ws_quote=quote,
                _bcc_ws_connection_fence='a'*32,_bcc_ws_mark_at=at,
                time=quote_at if quote else at,result={'list':[row]})


def book(at):
    return dict(retCode=0,_bcc_ws_book=True,time=at,
                result={'s':'BTCUSDT','ts':at,'b':[['100','1']],
                        'a':[['101','1']],'u':1,'seq':1})


@pytest.fixture
def controlled(monkeypatch):
    base=time.time()
    wall=[base]
    utc=[base]
    monkeypatch.setattr(data_cache,'time',SimpleNamespace(time=lambda:wall[0],
                        monotonic=time.monotonic,sleep=time.sleep))
    monkeypatch.setattr(policy,'book_age_bounds',lambda stamp:(utc[0]-stamp,utc[0]-stamp+.1))
    return wall,utc


@pytest.mark.parametrize('factory,url,params',[
    (ticker,MARK,dict(category='linear',symbol='BTCUSDT')),
    (book,BOOK,dict(category='linear',symbol='BTCUSDT',limit=50)),
])
def test_mark_and_book_survive_1p5s_but_never_original_deadline(tmp_path,controlled,factory,url,params):
    wall,utc=controlled
    registry=HubRegistry(tmp_path/'cache.sqlite')
    cache=PublicCache(registry.path)
    try:
        assert registry.claim_leader('owner')
        token=registry.leader_token('owner')
        registry.subscribe(url,params)
        stamp=int(utc[0]*1000)
        cache.bybit_ws_source_deadlines=True
        value=factory(stamp)
        record=dict(url=url,params=params,host=HOST,value=value,ttl=1.5)
        assert cache.publish_websockets([record],leader_owner='owner',leader_token=token)==[True]
        wall[0]+=1.8;utc[0]+=1.8
        result=cache.read_fresh(url,params,HOST)
        assert result['_bcc_ws_source_deadline'] is True
        assert result['time']==stamp
        # Move wall clock backwards. Reader checks original conservative age,
        # not just SQLite residence. This must not resurrect stale evidence.
        wall[0]-=2.;utc[0]+=1.3
        assert cache.read_fresh(url,params,HOST) is None
        assert cache.read_fresh_many([(url,params)],HOST)==[None]
        assert cache.publish_websockets([record],leader_owner='owner',leader_token=token)==[False]
    finally:
        cache.close()


def test_combined_ticker_uses_earliest_mark_or_quote_deadline(controlled):
    wall,utc=controlled
    now=int(utc[0]*1000)
    both=ticker(now,quote=True,quote_at=now-500)
    assert 1.39<policy.remaining_bybit_ws_seconds(both)<1.41
    old_mark=ticker(now-2800,quote=True,quote_at=now)
    assert 0.09<policy.remaining_bybit_ws_seconds(old_mark)<.11
    assert policy.remaining_bybit_ws_seconds(ticker(now-3200,quote=True,quote_at=now))==0


def test_default_residence_unchanged(tmp_path,controlled):
    wall,utc=controlled
    registry=HubRegistry(tmp_path/'cache.sqlite');cache=PublicCache(registry.path)
    params=dict(category='linear',symbol='BTCUSDT')
    try:
        assert registry.claim_leader('owner')
        registry.subscribe(MARK,params)
        record=dict(url=MARK,params=params,host=HOST,value=ticker(int(utc[0]*1000)),ttl=1.5)
        assert cache.publish_websockets([record],leader_owner='owner',leader_token=registry.leader_token('owner'))==[True]
        wall[0]+=1.8;utc[0]+=1.8
        assert cache.read_fresh(MARK,params,HOST) is None
    finally:
        cache.close()


@pytest.mark.parametrize('mutate',[
    lambda d:d.update(_bcc_ws_connection_fence='old'),
    lambda d:d.update(_bcc_ws_mark_at=True),
    lambda d:d.update(_bcc_ws_book=True),
    lambda d:d.update(retCode=10006),
])
def test_invalid_source_deadline_cannot_admit(mutate,controlled):
    _,utc=controlled
    data=ticker(int(utc[0]*1000));mutate(data)
    assert policy.remaining_bybit_ws_seconds(data)==0
