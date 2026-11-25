"""Real candidate stream classes and real shared cache; injected socket only."""
import json
import time
import pytest

from bcc import market_clock, ws_split_ticker
from bcc.market import PublicData
from bcc.market_hub import MarketDataHub
from bcc.ws_sharded_quotes import ShardedBybitQuoteStream
from bcc.ws_ticker_demand import KLINE, TICKER
from bcc.ws_dispatch import MarketEventQueue


class Socket:
    def __init__(self):
        self.sent=[]
        self.closed=False
    def send(self,raw):
        self.sent.append(json.loads(raw))
    def close(self):
        self.closed=True


@pytest.fixture
def fleet(monkeypatch):
    now=[round(time.time(),3)]
    bounds=lambda t:(now[0]-t,now[0]-t+.01)
    monkeypatch.setattr(market_clock,'book_age_bounds',bounds)
    monkeypatch.setattr(market_clock,'latest_now',lambda:now[0]+.01)
    monkeypatch.setattr(ws_split_ticker,'book_age_bounds',bounds)
    stream=ShardedBybitQuoteStream(wall=lambda:now[0],monotonic=lambda:now[0])
    sockets=[]
    for pair in stream._pairs:
        a,b=Socket(),Socket()
        pair._primary.connect(a)
        pair._recovery.connect(b)
        sockets.append((a,b))
    try:
        yield now,stream,sockets
    finally:
        stream.stop()


def frame(child,symbol,now,cs=1,kind='snapshot',**fields):
    return child.ingest(dict(topic='tickers.'+symbol,ts=round(now*1000),cs=cs,type=kind,
                             data=dict(symbol=symbol,**fields)),generation=child._generation)


def test_real_50_ticker_pairs_keep_both_lanes_and_never_reset_healthy_symbols(fleet):
    now,stream,sockets=fleet
    names=[f'C{i}USDT' for i in range(50)]
    stream.set_priority_symbols(names)
    stream.sync_symbols(names)
    for s in names:
        p=stream._pairs[stream._index(s)]
        assert s in p._primary._subscriptions and s in p._recovery._subscriptions
        assert frame(p._primary,s,now[0],markPrice='100')
        assert frame(p._recovery,s,now[0],markPrice='100')
    sizes=[(len(a.sent),len(b.sent)) for a,b in sockets]
    for cs in range(2,26):
        now[0]+=.25
        for s in names:
            p=stream._pairs[stream._index(s)]
            # Either lane may supply the verified maintained price.
            child=p._primary if cs%2 else p._recovery
            assert frame(child,s,now[0],cs,kind='delta',indexPrice='101')
        stream.sync_symbols(names)
        assert stream.refresh_stale_marks(names)==0
        assert stream.transport_health()['mark_union']['fresh']==50
    assert sizes==[(len(a.sent),len(b.sent)) for a,b in sockets]
    assert stream.transport_health()['redundancy']['both_fresh']==50
    failed=stream._pairs[0]
    failed._primary.disconnect()
    assert stream.transport_health()['mark_union']['fresh']==50
    assert stream.transport_health()['redundancy']['primary_fresh']<50
    now[0]+=4
    assert all(stream.independent_observations_for_hub(s)[0] is None for s in names)
    assert stream.refresh_stale_marks(names)==0
    now[0]+=2.1
    assert stream.refresh_stale_marks(names)<=16
    assert stream.refresh_stale_marks(names)==0


def test_real_hub_derives_demand_and_publishes_reserve_mark(fleet,tmp_path):
    now,stream,_=fleet
    source=PublicData(quote_stream=stream)
    hub=MarketDataHub(tmp_path/'cache.sqlite',source=source)
    try:
        source.shared.bybit_ws_source_deadlines=True
        assert hub.acquire_leadership()
        p=dict(category='linear',symbol='BTCUSDT',interval='1',limit=10)
        hub.registry.subscribe(KLINE,p,priority=True,lifetime=90)
        hub.registry.subscribe(KLINE,dict(p,end=59999),priority=True,lifetime=90)
        assert hub._renew_ticker_demand_from_history()==1
        assert hub._renew_ticker_demand_from_history()==0
        active,priority=hub.registry.stream_demand()
        assert ('tickers','BTCUSDT') in active and ('tickers','BTCUSDT') in priority
        hub.set_ws_dispatcher(MarketEventQueue())
        hub.subscribe_active_websockets()
        pair=stream._pairs[stream._index('BTCUSDT')]
        assert frame(pair._recovery,'BTCUSDT',now[0],markPrice='101')
        assert hub.publish_ws_event('tickers','BTCUSDT')
        params=dict(category='linear',symbol='BTCUSDT')
        row=source.shared.read_fresh(TICKER,params,'api.bybit.com')
        assert row['_bcc_ws_mark_at']==round(now[0]*1000)
        assert PublicData._decode_ws_position_mark('BTCUSDT',row)['bid']==101.
        pair._recovery.disconnect()
        assert hub.publish_ws_event('tickers','BTCUSDT')
        assert source.shared.read_fresh(TICKER,params,'api.bybit.com') is None
        source.shared.record_http_denial('a'*64,'fixture','api.bybit.com',cooldown=90,denial_status=403)
        hub.registry.subscribe(KLINE,dict(p,symbol='ETHUSDT'),lifetime=90)
        assert hub._renew_ticker_demand_from_history()==0
        assert 'ETHUSDT' not in hub.registry.active_bybit_symbols()[0]
    finally:
        hub.close()
