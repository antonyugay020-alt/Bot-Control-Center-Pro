"""Business-code feedback must not poison caches or stop independent WS data."""
import time
from types import SimpleNamespace

import pytest
import requests

from bcc.data_cache import CacheBusy, PublicCache
from bcc.market import PublicData, DataUnavailable
from test_ws_portfolio_observations import portfolio

BASE='https://api.bybit.com/v5/market/'
PARAMS={'category':'linear','symbol':'BTCUSDT'}


@pytest.fixture
def producer(tmp_path,monkeypatch):
    for name in ('BCC_PUBLIC_CACHE','BCC_MARKET_HUB_MODE','BCC_BYBIT_WS_QUOTES','BCC_BYBIT_WS_BOOKS'):
        monkeypatch.delenv(name,raising=False)
    source=PublicData();source.shared=PublicCache(tmp_path/'cache.sqlite')
    try:yield source
    finally:source.close()


class Response:
    def __init__(self,code,status=200,headers=None):
        self.status_code=status;self.headers=headers or {}
        self.payload={'retCode':code,'retMsg':'Too many visits' if code==10006 else 'request rejected','time':100000}
    def json(self):return self.payload
    def raise_for_status(self):
        if self.status_code>=400:raise requests.HTTPError('rate limit')


@pytest.mark.parametrize('code,status',[(10006,200),(10018,200),(0,429)])
def test_rate_feedback_gates_http_fleet_without_publishing_error_payload(producer,code,status):
    calls=[]
    def get(*args,**kwargs):calls.append(1);return Response(code,status,{'Retry-After':'17'})
    producer._thread.session=SimpleNamespace(get=get)
    with pytest.raises(DataUnavailable):producer.get(BASE+'risk-limit',PARAMS)
    other=PublicData();other.shared=PublicCache(producer.shared.path)
    other._thread.session=SimpleNamespace(get=lambda *a,**k:pytest.fail('HTTP during shared cooldown'))
    try:
        with pytest.raises(DataUnavailable):other.get(BASE+'kline',dict(PARAMS,interval='1',limit=1000))
        with producer.shared.db() as db:
            assert db.execute('SELECT COUNT(*) FROM cache').fetchone()[0]==0
            assert db.execute('SELECT COUNT(*) FROM lease').fetchone()[0]==0
            assert db.execute('SELECT until FROM cooldown').fetchone()[0]>=time.time()+16
        assert calls==[1] and producer.shared.denied_status('api.bybit.com') is None
    finally:other.close()


def test_nonrate_business_error_never_becomes_long_lived_risk_data(producer):
    producer._thread.session=SimpleNamespace(get=lambda *a,**k:Response(10001))
    with pytest.raises(DataUnavailable,match='rejected'):producer.get(BASE+'risk-limit',PARAMS,ttl=240)
    with producer.shared.db() as db:
        assert db.execute('SELECT COUNT(*) FROM cache').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM lease').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM cooldown').fetchone()[0]==0


def test_rate_refusal_with_lost_lease_does_not_delete_successor(producer):
    cache=producer.shared;key,old,_=cache.begin(BASE+'risk-limit',PARAMS,'api.bybit.com')
    with cache.db() as db:db.execute('UPDATE lease SET until=0 WHERE k=?',(key,))
    _,new,_=cache.begin(BASE+'risk-limit',PARAMS,'api.bybit.com')
    cache.record_http_cooldown(key,old,'api.bybit.com',cooldown=10)
    with cache.db() as db:assert db.execute('SELECT owner FROM lease WHERE k=?',(key,)).fetchone()[0]==new
    assert not cache.finish(key,old,'api.bybit.com',{'retCode':10006},ttl=240)
    with pytest.raises(CacheBusy):cache.begin(BASE+'instruments-info',PARAMS,'api.bybit.com')


def test_http_cooldown_preserves_real_ws_cache_publication_and_mark(portfolio):
    symbols,reader,hub,stream=portfolio
    url=BASE+'risk-limit';params={'category':'linear','symbol':symbols[0]}
    key,owner,_=hub.source.shared.begin(url,params,'api.bybit.com')
    hub.source.shared.record_http_cooldown(key,owner,'api.bybit.com',cooldown=90)
    symbol=symbols[0]
    assert stream.ingest({'type':'snapshot','topic':'tickers.'+symbol,'ts':int(time.time()*1000),
                          'data':{'symbol':symbol,'bid1Price':'100.4','ask1Price':'100.5','markPrice':'100.45'}},generation=stream._generation)
    assert hub.publish_ws_event('tickers',symbol)
    assert reader._ws_position_mark(symbol)['bid']==100.45


@pytest.mark.parametrize('hint',['nan','inf','garbage',None,'999999'])
def test_retry_hints_are_finite_and_bounded(hint):
    delay=PublicData._rate_delay(Response(10006,headers={'Retry-After':hint}))
    assert 10<=delay<=3600
    reset=PublicData._rate_delay(Response(10006,headers={'X-Bapi-Limit-Reset-Timestamp':'140000'}),{'time':100000})
    assert reset==40.25


def test_legacy_error_row_is_never_read_and_can_be_replaced_immediately(producer):
    import json
    cache=producer.shared;url=BASE+'risk-limit'
    key,owner,_=cache.begin(url,PARAMS,'api.bybit.com')
    cache.finish(key,owner,'api.bybit.com')
    with cache.db() as db:
        db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?)',
                   (key,json.dumps({'retCode':10006,'retMsg':'old rate refusal'}),time.time()+240,'api.bybit.com'))
    assert cache.read_fresh(url,PARAMS,'api.bybit.com') is None
    assert cache.read_fresh_many([(url,PARAMS)],'api.bybit.com')==[None]
    _,owner,value=cache.begin(url,PARAMS,'api.bybit.com');assert owner and value is None
    assert cache.finish(key,owner,'api.bybit.com',{'retCode':0,'result':{'list':[]}},ttl=240)
    assert cache.read_fresh(url,PARAMS,'api.bybit.com')['retCode']==0


def test_cache_boundary_rejects_business_errors_even_from_a_legacy_producer(producer):
    cache=producer.shared;url=BASE+'risk-limit'
    key,owner,_=cache.begin(url,PARAMS,'api.bybit.com')
    assert cache.finish(key,owner,'api.bybit.com',{'retCode':10006},ttl=240) is False
    assert cache.read_fresh(url,PARAMS,'api.bybit.com') is None
    with cache.db() as db:assert db.execute('SELECT COUNT(*) FROM lease').fetchone()[0]==0
