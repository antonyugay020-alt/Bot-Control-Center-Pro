"""Bounded WS recovery bursts: exposure-first, fair deferred demand, fail-closed prices.

No Bybit requests, credentials, files from a live bot, or real trading.
"""
import json
import pytest

from bcc.ws_quotes import BybitQuoteStream, StreamUnavailable
from bcc.ws_orderbooks import BybitOrderbookWS
from bcc.ws_dispatch import MAX_REFRESH_TOPICS_PER_SCAN


class Socket:
    def __init__(self):
        self.messages=[]
        self.closed=False
        self.fail=False

    def send(self, raw):
        if self.fail:
            raise OSError('socket write failed')
        self.messages.append(json.loads(raw))

    def close(self):
        self.closed=True


def symbols(prefix, count):
    return [f'{prefix}{i}USDT' for i in range(count)]


def refreshed_topics(socket, topic):
    return {item.removeprefix(topic + '.')
            for msg in socket.messages if msg['op'] == 'unsubscribe'
            for item in msg['args']}


def test_thousand_held_mark_recoveries_are_fair_and_cannot_burst_all_at_once():
    moment=[100000.]
    stream=BybitQuoteStream(monotonic=lambda:moment[0],wall=lambda:moment[0])
    names=symbols('Q', 1000)
    stream.subscribe_many(names)
    stream.set_priority_symbols(names)
    socket=Socket()
    stream.connect(socket)
    moment[0]+=3.01
    try:
        assert stream.refresh_stale_marks(names)==MAX_REFRESH_TOPICS_PER_SCAN
        assert stream.refresh_pressure()=={
            'deferred_symbols':len(names)-MAX_REFRESH_TOPICS_PER_SCAN,
            'per_scan_budget':MAX_REFRESH_TOPICS_PER_SCAN,
        }
        assert len(socket.messages)==2*((MAX_REFRESH_TOPICS_PER_SCAN+9)//10)
        assert all(len(x['args']) <= 10 for x in socket.messages)
        first=refreshed_topics(socket,'tickers')
        assert len(first)==MAX_REFRESH_TOPICS_PER_SCAN
        with pytest.raises(StreamUnavailable):
            stream.mark(names[0])  # Control requests are never mark evidence.
        moment[0]+=.25
        assert stream.refresh_stale_marks(names)==MAX_REFRESH_TOPICS_PER_SCAN
        assert len(refreshed_topics(socket,'tickers'))==2*MAX_REFRESH_TOPICS_PER_SCAN
        for _ in range(6):
            moment[0]+=.25
            stream.refresh_stale_marks(names)
        assert refreshed_topics(socket,'tickers') == set(names), 'Oldest due must eventually be selected'
    finally:
        stream.disconnect()


def test_exposure_mark_demand_outranks_scan_and_debt_is_visible():
    clock=[100000.]
    stream=BybitQuoteStream(monotonic=lambda:clock[0],wall=lambda:clock[0])
    held=symbols('P',65);cold=symbols('Z',400)
    stream.subscribe_many(cold+held)
    stream.set_priority_symbols(held)
    socket=Socket();stream.connect(socket)
    clock[0]+=3.1
    try:
        assert stream.refresh_stale_marks(cold+held)==85
        chosen=refreshed_topics(socket,'tickers')
        assert set(held) <= chosen
        assert len(chosen-set(held))==20
        assert stream.refresh_pressure()['deferred_symbols']==len(cold)-20
        stream.set_priority_symbols([])
        assert stream._priority_symbols==frozenset()
    finally:
        stream.disconnect()


def test_thousand_quiet_books_cannot_starve_held_depth_or_flood_commands():
    clock=[100000.]
    stream=BybitOrderbookWS(monotonic=lambda:clock[0],wall=lambda:clock[0])
    held=symbols('P',50);cold=symbols('A',900)
    stream.subscribe(cold+held)
    stream.set_priority_symbols(held)
    socket=Socket();stream.connect(socket)
    clock[0]+=3.01
    try:
        assert stream.refresh_stale_books(cold+held)==MAX_REFRESH_TOPICS_PER_SCAN
        chosen=refreshed_topics(socket,'orderbook.50')
        assert set(held) <= chosen, 'Held depth must outrank lexically earlier discovery'
        assert len(socket.messages)==2*((MAX_REFRESH_TOPICS_PER_SCAN+9)//10)
        assert stream.refresh_pressure()['deferred_symbols']==len(cold+held)-MAX_REFRESH_TOPICS_PER_SCAN
        clock[0]+=.25
        assert stream.refresh_stale_books(cold+held)==MAX_REFRESH_TOPICS_PER_SCAN
        assert len(refreshed_topics(socket,'orderbook.50'))==2*MAX_REFRESH_TOPICS_PER_SCAN
        # No synthetic recovery: missing actual WS snapshots cannot be used.
        with pytest.raises(StreamUnavailable):
            stream.get_orderbook(held[0])
    finally:
        stream.disconnect()


def test_bounded_books_preserve_existing_120_topic_recovery_contract():
    clock=[1000.]
    stream=BybitOrderbookWS(monotonic=lambda:clock[0],wall=lambda:clock[0])
    names=symbols('B',120)
    stream.subscribe(names);socket=Socket();stream.connect(socket)
    clock[0]+=3.01
    assert stream.refresh_stale_books(names)==120
    assert len(socket.messages)==24
    assert stream.refresh_pressure()['deferred_symbols']==0
    assert stream.refresh_stale_books(names)==0


@pytest.mark.parametrize('factory,subscribe,refresh',[
    (BybitQuoteStream,'subscribe_many','refresh_stale_marks'),
    (BybitOrderbookWS,'subscribe','refresh_stale_books'),
])
def test_failed_recovery_send_revokes_source_and_closes_owned_socket(factory,subscribe,refresh):
    clock=[1000.]
    stream=factory(monotonic=lambda:clock[0],wall=lambda:clock[0])
    getattr(stream,subscribe)(['BTCUSDT'])
    socket=Socket();stream.connect(socket)
    clock[0]+=3.1;socket.fail=True
    with pytest.raises(StreamUnavailable):
        getattr(stream,refresh)(['BTCUSDT'])
    assert socket.closed and not stream._connected
    assert stream.refresh_pressure()['deferred_symbols']==0


def test_hub_propagates_authenticated_priority_to_both_ws_feeds(tmp_path):
    from bcc.market_hub import MarketDataHub
    from bcc.market import PublicData
    quote=BybitQuoteStream()
    book=BybitOrderbookWS()
    quote.connect(Socket());book.connect(Socket())
    source=PublicData(quote_stream=quote,orderbook_stream=book)
    hub=MarketDataHub(tmp_path/'isolated_hub.sqlite',source=source)
    root='https://api.bybit.com/v5/market/'
    try:
        assert hub.acquire_leadership()
        for name in ('AUSDT','BTCUSDT'):
            hub.registry.subscribe(root+'tickers',{'category':'linear','symbol':name},
                                   priority=name=='BTCUSDT')
            hub.registry.subscribe(root+'orderbook',{'category':'linear','symbol':name,'limit':50},
                                   priority=name=='BTCUSDT')
        hub.subscribe_active_websockets()
        assert quote._priority_symbols=={'BTCUSDT'}
        assert book._priority_symbols=={'BTCUSDT'}
        with hub.registry._connect() as db:
            db.execute('UPDATE hub_subscriptions SET priority_until=0')
        hub._ws_subscription_scan_due=0
        hub.subscribe_active_websockets()
        assert book._priority_symbols==frozenset()
        assert quote._priority_symbols==frozenset()
    finally:
        hub.close()
        source.close()
