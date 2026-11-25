"""Durable exposure demand outranks scan traffic before cache publication."""
import json
import time

import pytest

from bcc.market_hub import HubRegistry,MarketDataHub
from bcc.market import PublicData
from bcc.ws_dispatch import MarketEventQueue
from bcc.ws_quotes import BybitQuoteStream,StreamUnavailable
from bcc.ws_orderbooks import BybitOrderbookWS
from bcc.ws_klines import BybitKlineWS


BASE='https://api.bybit.com/v5/market/'


def test_twenty_exposure_updates_outrank_two_thousand_scan_events_with_fairness():
    queue=MarketEventQueue(capacity=4096)
    background={('depth50',f'S{i}USDT') for i in range(1000)}|{('tickers',f'S{i}USDT') for i in range(1000)}
    critical={('depth50',f'P{i}USDT') for i in range(10)}|{('tickers',f'P{i}USDT') for i in range(10)}
    queue.configure(background|critical,critical)
    for item in sorted(background):queue.notify(*item)
    for item in sorted(critical):queue.notify(*item)
    received=set();turns=0;background_turns=0
    while received!=critical:
        item=queue.take(timeout=0);turns+=1
        if item in critical:received.add(item)
        else:background_turns+=1
        assert turns<=22
    assert background_turns==2
    # At an injected 20 ms/commit, exposure service stays within cache TTL.
    assert turns*.020 < .75


def test_pending_promotion_and_inactive_filter_happen_before_another_frame():
    q=MarketEventQueue()
    q.notify('depth50','OLDUSDT');q.notify('tickers','HOTUSDT');q.notify('depth50','SCANUSDT')
    q.configure({('tickers','HOTUSDT'),('depth50','SCANUSDT')},{('tickers','HOTUSDT')})
    assert q.take(timeout=0)==('tickers','HOTUSDT')
    assert q.take(timeout=0)==('depth50','SCANUSDT')
    assert q.take(timeout=0) is None
    assert not q.notify('depth50','OLDUSDT')
    assert q.stats()['filtered_inactive']==2


def test_full_scan_queue_cannot_drop_a_new_held_position_update():
    q=MarketEventQueue(capacity=16)
    scans={('depth50',f'S{i}USDT') for i in range(16)}
    hot=('tickers','HOTUSDT')
    q.configure(scans|{hot},{hot})
    for item in scans:assert q.notify(*item)
    assert q.notify(*hot)
    assert q.stats()['pending']==16
    assert q.stats()['evicted_background']==1
    assert q.take(timeout=0)==hot


def test_background_quotes_and_depth_survive_permanent_exposure_traffic():
    q=MarketEventQueue(urgent_burst=3)
    hot=('tickers','HOTUSDT');book=('depth50','SCANUSDT');quote=('tickers','SCANUSDT')
    q.configure({hot,book,quote},{hot})
    for item in (hot,book,quote):q.notify(*item)
    seen=set()
    for _ in range(8):
        item=q.take(timeout=0)
        if item==hot:q.notify(*hot)
        else:seen.add(item)
    assert seen=={book,quote}


def test_registry_stream_scope_and_short_urgency_are_validated(tmp_path):
    r=HubRegistry(tmp_path/'cache.sqlite')
    r.subscribe(BASE+'tickers',{'category':'linear','symbol':'AUSDT'},priority=True)
    r.subscribe(BASE+'orderbook',{'category':'linear','symbol':'4USDT','limit':50})
    r.subscribe(BASE+'orderbook',{'category':'linear','symbol':'4USDT','limit':200},priority=True)
    r.subscribe(BASE+'tickers',{'category':'linear'})
    r.subscribe(BASE+'tickers',None)
    r.subscribe(BASE+'kline',{'category':'linear','symbol':'AUSDT','interval':'1','limit':10})
    r.subscribe(BASE+'kline',{'category':'linear','symbol':'AUSDT','interval':'1','limit':1000})
    active,priority=r.stream_demand()
    assert active=={('tickers','AUSDT'),('depth50','4USDT'),('kline1','AUSDT')}
    assert priority=={('tickers','AUSDT')}
    with r._connect() as db:db.execute('UPDATE hub_subscriptions SET priority_until=0')
    assert r.stream_demand()[1]==set()


class Socket:
    def __init__(self):self.sent=[];self.closed=False
    def send(self,raw):self.sent.append(json.loads(raw))
    def close(self):self.closed=True


@pytest.mark.parametrize('factory,attribute,topic',[
    (BybitQuoteStream,'_subscriptions','tickers'),
    (BybitOrderbookWS,'_symbols','orderbook.50'),
    (BybitKlineWS,'_symbols','kline.1'),
])
def test_producer_demand_converges_without_revoking_the_healthy_socket(factory,attribute,topic):
    stream=factory();socket=Socket()
    (stream.subscribe_many if hasattr(stream,'subscribe_many') else stream.subscribe)(['AUSDT','4USDT'])
    generation=stream.connect(socket)
    assert stream.sync_symbols(['AUSDT','NEWUSDT'])==1
    assert getattr(stream,attribute)=={'AUSDT','NEWUSDT'}
    assert socket.sent==[{'op':'unsubscribe','args':[topic+'.4USDT']},
                         {'op':'subscribe','args':[topic+'.NEWUSDT']}]
    assert stream._connected and stream._generation==generation
    assert not socket.closed
    with pytest.raises(ValueError):stream.sync_symbols(['A/USDT'])
    assert getattr(stream,attribute)=={'AUSDT','NEWUSDT'}


def test_hub_wires_demand_priority_expiry_and_stream_cleanup(tmp_path):
    q=MarketEventQueue();stream=BybitQuoteStream(on_update=q.notify)
    stream.connect(Socket())
    source=PublicData(quote_stream=stream)
    hub=MarketDataHub(tmp_path/'cache.sqlite',source=source)
    hub.set_ws_dispatcher(q)
    try:
        assert hub.acquire_leadership()
        hot=hub.registry.subscribe(BASE+'tickers',{'category':'linear','symbol':'AUSDT'},priority=True)
        cold=hub.registry.subscribe(BASE+'tickers',{'category':'linear','symbol':'4USDT'})
        hub.subscribe_active_websockets()
        assert q.stats()['pending']==0
        assert stream._priority_symbols=={'AUSDT'}
        for symbol in ('4USDT','AUSDT'):
            assert stream.ingest({'type':'snapshot','topic':'tickers.'+symbol,'ts':int(time.time()*1000),
                                  'data':{'symbol':symbol,'bid1Price':'.4','ask1Price':'.41','markPrice':'.405'}},
                                 generation=stream._generation)
        assert q.take(timeout=0)==('tickers','AUSDT')
        assert hub.publish_ws_event('tickers','AUSDT')
        with hub.registry._connect() as db:db.execute('UPDATE hub_subscriptions SET active_until=0 WHERE k=?',(cold,))
        hub._ws_subscription_scan_due=0
        hub.subscribe_active_websockets()
        assert q.take(timeout=0) is None
        assert stream._subscriptions=={'AUSDT'}
        with pytest.raises(StreamUnavailable):stream.mark('4USDT')
        assert stream.mark('AUSDT')[0]==.405
    finally:
        hub.close();source.close()


@pytest.mark.parametrize('factory',[BybitQuoteStream,BybitOrderbookWS,BybitKlineWS])
def test_failed_unsubscribe_closes_only_owned_stream_and_revokes_generation(factory):
    stream=factory()
    (stream.subscribe_many if hasattr(stream,'subscribe_many') else stream.subscribe)(['AUSDT','4USDT'])
    socket=Socket();old=stream.connect(socket)
    def broken(raw):raise OSError('closed socket')
    socket.send=broken
    with pytest.raises(StreamUnavailable,match='unsubscribe'):
        stream.sync_symbols(['AUSDT'])
    assert socket.closed and not stream._connected and stream._generation!=old


def test_deferred_stream_demand_is_preserved_without_contacting_venues(tmp_path):
    registry=HubRegistry(tmp_path/'cache.sqlite')
    registry.subscribe('https://clob.polymarket.com/book',{'token_id':'123'},priority=True)
    registry.subscribe('https://api.binance.com/api/v3/ticker/price',{'symbol':'BTCUSDT'})
    registry.subscribe('https://gamma-api.polymarket.com/markets',None)
    active,priority=registry.stream_demand()
    assert active=={('polybook','123'),('binance_trade','BTCUSDT')}
    assert priority=={('polybook','123')}


@pytest.mark.parametrize('factory',[BybitQuoteStream,BybitOrderbookWS,BybitKlineWS])
def test_rejected_unsubscribe_ack_revokes_stream_instead_of_leaking_old_topics(factory):
    stream=factory();stream.subscribe('AUSDT')
    checked=[]
    class App:
        def __init__(self,endpoint,*,on_open,on_message,on_close):
            self.opened,self.received=on_open,on_message
            self.closed=False
        def send(self,raw):pass
        def close(self):self.closed=True
        def run_forever(self,**kwargs):
            self.opened(self)
            assert stream._connected
            self.received(self,json.dumps({'op':'unsubscribe','success':False}))
            checked.append(self.closed and not stream._connected)
            stream.stop()
    stream.run(websocket_app_factory=App,retry_delay=.1)
    assert checked==[True]


def test_held_books_and_marks_reach_sqlite_before_large_scanner_flood(tmp_path,monkeypatch):
    from bcc import market_clock
    reference=market_clock.MarketClock()
    monkeypatch.setattr(market_clock,'clock',reference)
    monkeypatch.setattr('bcc.market.clock',reference)
    events=MarketEventQueue(capacity=2048)
    quotes=BybitQuoteStream(on_update=events.notify)
    books=BybitOrderbookWS(on_update=events.notify)
    quotes.connect();books.connect()
    source=PublicData(quote_stream=quotes,orderbook_stream=books)
    path=tmp_path/'cache.sqlite';hub=MarketDataHub(path,source=source)
    hub.set_ws_dispatcher(events)
    reader=None
    scans=[f'S{i}USDT' for i in range(500)]
    held=[f'P{i}USDT' for i in range(5)]
    try:
        assert hub.acquire_leadership()
        for symbol in scans+held:
            params={'category':'linear','symbol':symbol}
            hub.registry.subscribe(BASE+'tickers',params,priority=symbol in held)
            hub.registry.subscribe(BASE+'orderbook',dict(params,limit=50),priority=symbol in held)
        hub.subscribe_active_websockets()
        stamp=int(time.time()*1000)
        for symbol in scans+held:
            assert quotes.ingest({'type':'snapshot','topic':'tickers.'+symbol,'ts':stamp,
                                  'data':{'symbol':symbol,'bid1Price':'100','ask1Price':'101','markPrice':'100.5'}},
                                 generation=quotes._generation)
            assert books.ingest({'type':'snapshot','topic':'orderbook.50.'+symbol,'ts':stamp,
                                 'data':{'s':symbol,'u':1,'seq':1,'b':[['100','100']], 'a':[['101','100']]}},
                                generation=books._generation)
        wanted={(kind,symbol) for symbol in held for kind in ('tickers','depth50')}
        completed=set();turns=0
        while completed!=wanted:
            item=events.take(timeout=0);assert item is not None
            time.sleep(.02)  # Model a slow cache writer without faking quote ages.
            assert hub.publish_ws_event(*item)
            turns+=1
            if item in wanted:completed.add(item)
            assert turns<=11
        monkeypatch.setenv('BCC_PUBLIC_CACHE',str(path))
        monkeypatch.setenv('BCC_MARKET_HUB_MODE','client')
        monkeypatch.setenv('BCC_BYBIT_WS_QUOTES','1')
        reader=PublicData();reader.set_position_symbols(held)
        marks,valuations=reader._ws_portfolio_observations()
        assert set(marks)==set(valuations)==set(held)
        for symbol in held:
            value=reader.shared.read_fresh(BASE+'orderbook',{'category':'linear','symbol':symbol,'limit':50},'api.bybit.com')
            assert value['_bcc_ws_book'] is True and value['result']['s']==symbol
    finally:
        if reader is not None:reader.close()
        hub.close();source.close()


def test_held_mark_recovers_ahead_of_background_without_relaxing_either_clock():
    timer=[10000.];socket=Socket()
    stream=BybitQuoteStream(monotonic=lambda:timer[0],wall=lambda:timer[0])
    stream.subscribe_many(['AAAUSDT','ZZZUSDT']);generation=stream.connect(socket)
    for symbol in ('AAAUSDT','ZZZUSDT'):
        assert stream.ingest({'type':'snapshot','topic':'tickers.'+symbol,'ts':10000000,
                              'data':{'symbol':symbol,'bid1Price':'1','ask1Price':'2','markPrice':'1.5'}},generation=generation)
    stream.set_priority_symbols(['ZZZUSDT'])
    timer[0]+=.6
    assert stream.refresh_stale_marks(['AAAUSDT','ZZZUSDT'])==1
    assert socket.sent[-1]['args']==['tickers.ZZZUSDT']
    assert stream.refresh_stale_marks(['AAAUSDT','ZZZUSDT'])==0
    timer[0]+=.6
    assert stream.refresh_stale_marks(['AAAUSDT','ZZZUSDT'])==1
    assert socket.sent[-1]['args']==['tickers.AAAUSDT']
    assert stream.refresh_pressure()['pending_snapshot_topics']==2
    timer[0]+=.9  # Recovery requests alone never make an old mark fresh.
    with pytest.raises(StreamUnavailable):stream.mark('ZZZUSDT')
    with pytest.raises(StreamUnavailable):stream.quote('ZZZUSDT')
    stream.set_priority_symbols([])
    assert not stream._priority_symbols
