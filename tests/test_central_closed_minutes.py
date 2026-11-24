"""Real stream -> Hub -> shared SQLite -> two closed-bar consumers."""
import time

import pytest

from bcc import market, market_clock
from bcc.data_cache import PublicCache, ProviderDenied
from bcc.market import PublicData, SourceAccessDenied
from bcc.market_hub import MarketDataHub, HubRegistry
from bcc.ws_klines import BybitKlineWS
from bcc.ws_dispatch import MarketEventQueue

URL = 'https://api.bybit.com/v5/market/kline'
HOST = 'api.bybit.com'
SYMBOL = 'BTCUSDT'
PARAMS = dict(category='linear', symbol=SYMBOL, interval='1', limit=10)
BOUNDARY = 60000000
START = BOUNDARY-60000


def frame(start, ts, confirmed, close='100.5'):
    return dict(type='snapshot', topic='kline.1.'+SYMBOL, ts=ts,
                data=[dict(start=start, end=start+59999, interval='1',
                           open='100', high='102', low='99', close=close,
                           volume='2', confirm=confirmed)])


@pytest.fixture
def connected(tmp_path, monkeypatch):
    now = [BOUNDARY/1000+.1]
    monkeypatch.setattr(market.clock, 'ensure', lambda:None)
    monkeypatch.setattr(market, 'market_now', lambda:now[0])
    monkeypatch.setattr(market_clock, 'market_now', lambda:now[0])
    monkeypatch.delenv('BCC_PUBLIC_CACHE', raising=False)
    stream=BybitKlineWS(wall=lambda:now[0], monotonic=lambda:now[0],
                        retain_confirmed=True)
    stream.subscribe(SYMBOL)
    generation=stream.connect()
    source=PublicData(kline_stream=stream)
    path=tmp_path/'market.sqlite'
    hub=MarketDataHub(path, source=source)
    hub.source.shared.central_closed_history=True
    try:
        assert hub.acquire_leadership()
        hub.registry.subscribe(URL, PARAMS, priority=True)
        yield now,stream,generation,hub,path
    finally:
        hub.close()


def test_late_finality_survives_new_forming_candle_and_is_shared(connected, monkeypatch):
    now,stream,gen,hub,path=connected
    assert stream.ingest(frame(BOUNDARY,BOUNDARY+100,False,close='101'),generation=gen)
    assert stream.ingest(frame(START,BOUNDARY,True),generation=gen)
    assert stream.get_candle(SYMBOL).start_ms==BOUNDARY
    assert stream.get_candle(SYMBOL).confirmed is False
    assert hub.publish_ws_event('kline1',SYMBOL)
    raw=hub.source.shared.read_fresh(URL,PARAMS,HOST)
    assert raw['_bcc_ws_kline_confirmed'] is False
    assert raw['result']['list'][0][0]==str(BOUNDARY)
    assert '_bcc_ws_finalized_minutes' not in raw
    assert hub.source.shared.stats['finalized_minutes_published']==1
    assert hub.publish_ws_event('kline1',SYMBOL)
    assert hub.source.shared.stats['finalized_minutes_published']==1

    monkeypatch.setenv('BCC_MARKET_HUB_MODE','client')
    monkeypatch.setenv('BCC_BYBIT_WS_KLINES','1')
    monkeypatch.setenv('BCC_BYBIT_CLOSED_HISTORY_CANDIDATE','1')
    for _ in range(2):
        consumer=PublicData()
        consumer.shared=PublicCache(path)
        previous=[dict(t=START-i*60000,open=100.,high=102.,low=99.,
                       close=100.,volume=1.) for i in range(100,0,-1)]
        consumer.cache[('closed_bars',SYMBOL)]=(time.time(),previous)
        monkeypatch.setattr(consumer,'bybit',lambda *a,**kw:pytest.fail('Unexpected REST history request'))
        try:
            bars=consumer.closed_candles(SYMBOL,100,priority=True)
            assert len(bars)==100 and bars[-1]['t']==START
            assert bars[-1]['close']==100.5
            assert consumer.closed_candles(SYMBOL,100)==bars
        finally:
            consumer.close()


def test_final_only_does_not_republish_expired_forming_candle(connected):
    now,stream,gen,hub,path=connected
    assert stream.ingest(frame(START,BOUNDARY,True),generation=gen)
    now[0]+=11
    assert hub.publish_ws_event('kline1',SYMBOL)
    assert hub.source.shared.read_fresh(URL,PARAMS,HOST) is None
    reader=PublicCache(path)
    try:
        assert reader.read_closed_minutes(SYMBOL,[START])[0]['confirmed_ms']==BOUNDARY
        stream.disconnect(generation=gen)
        assert stream.get_confirmed_candles(SYMBOL)==[]
        assert not hub.publish_ws_event('kline1',SYMBOL)
        # An already committed historical fact remains available; unlike
        # executable quotes it is not invalidated by a later reconnect.
        assert reader.read_closed_minutes(SYMBOL,[START])
        hub.source.shared.record_http_denial('a'*64,'test',HOST,cooldown=90,denial_status=403)
        with pytest.raises(ProviderDenied):
            reader.read_closed_minutes(SYMBOL,[START])
    finally:
        reader.close()


def test_no_publication_without_active_subscription(connected):
    now,stream,gen,hub,path=connected
    assert stream.ingest(frame(START,BOUNDARY,True),generation=gen)
    k=HubRegistry.identity(URL,PARAMS)[0]
    with hub.registry._connect() as db:
        db.execute('UPDATE hub_subscriptions SET active_until=0 WHERE k=?',(k,))
    assert not hub.publish_ws_event('kline1',SYMBOL)
    assert hub.source.shared.read_closed_minutes(SYMBOL,[START])==[]


def test_busy_publication_requeues_original_notification(connected,monkeypatch):
    from bcc.data_cache import CacheContention
    now,stream,gen,hub,path=connected
    assert stream.ingest(frame(START,BOUNDARY,True),generation=gen)
    q=MarketEventQueue()
    q.configure({('kline1',SYMBOL)})
    hub.set_ws_dispatcher(q)
    publish=hub.source.shared.publish_websockets
    def blocked(*a,**kw):
        raise CacheContention('synthetic SQLite lock')
    monkeypatch.setattr(hub.source.shared,'publish_websockets',blocked)
    assert not hub.publish_ws_event('kline1',SYMBOL)
    assert q.take_batch(timeout=0)==[('kline1',SYMBOL)]
    monkeypatch.setattr(hub.source.shared,'publish_websockets',publish)
    assert hub.publish_ws_event('kline1',SYMBOL)
    assert hub.source.shared.read_closed_minutes(SYMBOL,[START])


def test_missing_confirmation_uses_small_exact_rest_tail(connected,monkeypatch):
    now,stream,gen,hub,path=connected
    now[0]+=3
    monkeypatch.setenv('BCC_MARKET_HUB_MODE','client')
    monkeypatch.setenv('BCC_BYBIT_WS_KLINES','1')
    monkeypatch.setenv('BCC_BYBIT_CLOSED_HISTORY_CANDIDATE','1')
    consumer=PublicData()
    consumer.shared=PublicCache(path)
    consumer.cache[('closed_bars',SYMBOL)]=(time.time(),[
        dict(t=START-i*60000,open=100.,high=102.,low=99.,close=100.,volume=1.)
        for i in range(100,0,-1)])
    calls=[]
    def bybit(route,params):
        calls.append((route,params))
        return dict(time=int(now[0]*1000),result={'list':[
            [str(START),'100','102','99','100.5','2']]})
    monkeypatch.setattr(consumer,'bybit',bybit)
    try:
        assert consumer.closed_candles(SYMBOL,100)[-1]['t']==START
        assert calls==[('kline',dict(symbol=SYMBOL,interval='1',limit=1,end=BOUNDARY-1))]
    finally:
        consumer.close()
