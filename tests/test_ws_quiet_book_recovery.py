"""Quiet executable books need real snapshots before cache/age gaps grow."""
import json

import pytest

from bcc.market import PublicData
from bcc.market_hub import MarketDataHub
from bcc.ws_orderbooks import BybitOrderbookWS
from bcc.ws_quotes import StreamUnavailable


class Socket:
    def __init__(self):
        self.sent=[]
        self.closed=False
    def send(self, raw):
        self.sent.append(json.loads(raw))
    def close(self):
        self.closed=True


def setup():
    clock=[10000.,100.]
    stream=BybitOrderbookWS(wall=lambda:clock[0],monotonic=lambda:clock[1])
    stream.subscribe('AUSDT')
    socket=Socket()
    generation=stream.connect(socket)
    return clock,stream,socket,generation


def feed(stream,clock,generation):
    return stream.ingest({'type':'snapshot','topic':'orderbook.50.AUSDT',
                         'ts':int(clock[0]*1000),
                         'data':{'s':'AUSDT','u':1,'seq':1,'b':[['.4','100']], 'a':[['.41','100']]}},
                        generation=generation)


def advance(clock, seconds):
    clock[0]+=seconds
    clock[1]+=seconds


def test_quiet_book_recovery_never_restamps_old_depth():
    c,stream,socket,gen=setup()
    assert feed(stream,c,gen)
    old=stream.get_orderbook('AUSDT')['ts']
    advance(c,1.1)
    assert stream.refresh_stale_books(['AUSDT'])==1
    assert socket.sent==[{'op':'unsubscribe','args':['orderbook.50.AUSDT']},
                         {'op':'subscribe','args':['orderbook.50.AUSDT']}]
    assert stream.get_orderbook('AUSDT')['ts']==old
    assert stream.refresh_stale_books(['AUSDT'])==0
    advance(c,2.1)
    with pytest.raises(StreamUnavailable):
        stream.get_orderbook('AUSDT')
    assert feed(stream,c,gen)
    assert stream.get_orderbook('AUSDT')['ts']==c[0]
    stream.disconnect()
    assert not feed(stream,c,gen)


def test_missing_book_recovery_batches_and_failed_send_closes_owned_socket():
    c,stream,socket,gen=setup()
    symbols=[f'COIN{i}USDT' for i in range(120)]
    stream.subscribe(symbols)
    socket.sent.clear()
    assert stream.refresh_stale_books(symbols)==0
    advance(c,3)
    assert stream.refresh_stale_books(symbols)==120
    assert len(socket.sent)==24
    assert all(len(row['args'])==10 for row in socket.sent)
    assert stream.refresh_stale_books(symbols)==0
    advance(c,1)
    def broken(_):raise OSError('socket lost')
    socket.send=broken
    with pytest.raises(StreamUnavailable,match='refresh'):
        stream.refresh_stale_books(symbols)
    assert socket.closed
    assert not feed(stream,c,gen)


def test_hub_refreshes_quiet_book_between_discovery_ticks(tmp_path,monkeypatch):
    c,stream,socket,gen=setup()
    source=PublicData(orderbook_stream=stream)
    hub=MarketDataHub(tmp_path/'cache.sqlite',source=source)
    calls=[]
    original=hub.registry.active_bybit_symbols
    def scan():
        calls.append(1)
        return original()
    monkeypatch.setattr(hub.registry,'active_bybit_symbols',scan)
    try:
        hub.registry.subscribe('https://api.bybit.com/v5/market/orderbook',
                               {'category':'linear','symbol':'AUSDT','limit':50})
        assert hub.acquire_leadership()
        monkeypatch.setattr('bcc.market_hub.time.monotonic',lambda:c[1])
        assert feed(stream,c,gen)
        hub.subscribe_active_websockets()
        advance(c,.8)
        assert feed(stream,c,gen)
        advance(c,.2)
        hub.subscribe_active_websockets()
        before=len(socket.sent)
        advance(c,.81)
        hub.subscribe_active_websockets()
        assert len(socket.sent)==before+2
        assert len(calls)==2
        assert stream.get_orderbook('AUSDT')['ts']==pytest.approx(c[0]-1.01)
    finally:
        hub.close()
        source.close()
