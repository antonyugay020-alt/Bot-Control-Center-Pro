"""Cross-venue late-socket-close fencing after successful reconnect.

An old callback MUST NOT revoke the fresh generation of a new socket.
Never opens a network connection; each source is an isolated public stream.
"""
import unittest

from bcc.ws_quotes import BybitQuoteStream, StreamUnavailable
from bcc.ws_orderbooks import BybitOrderbookWS
from bcc.ws_klines import BybitKlineWS
from bcc.ws_polymarket import PolymarketMarketWS
from bcc.ws_binance import BinanceTradeWS


class StaleCloseTests(unittest.TestCase):
    def test_quote_old_close_preserves_new_connection(self):
        stream = BybitQuoteStream()
        stream.subscribe("BTCUSDT")
        old = stream.connect()
        current = stream.connect()
        self.assertFalse(stream.disconnect(generation=old))
        self.assertTrue(stream._connected)
        self.assertEqual(stream._generation, current)

    def test_depth_old_close_preserves_new_connection(self):
        stream = BybitOrderbookWS()
        stream.subscribe("BTCUSDT")
        old = stream.connect()
        current = stream.connect()
        self.assertFalse(stream.disconnect(generation=old))
        self.assertTrue(stream._connected)
        self.assertEqual(stream._generation, current)

    def test_kline_old_close_preserves_new_connection(self):
        stream = BybitKlineWS()
        stream.subscribe("BTCUSDT")
        old = stream.connect()
        current = stream.connect()
        self.assertFalse(stream.disconnect(generation=old))
        self.assertTrue(stream._connected)
        self.assertEqual(stream._generation, current)

    def test_clob_old_close_preserves_new_connection(self):
        stream = PolymarketMarketWS()
        stream.subscribe("123")
        old = stream.connect()
        current = stream.connect()
        self.assertFalse(stream.disconnect(generation=old))
        self.assertTrue(stream._connected)
        self.assertEqual(stream._generation, current)

    def test_binance_old_close_preserves_new_connection(self):
        stream = BinanceTradeWS()
        stream.subscribe("BTCUSDT")
        old = stream.connect()
        current = stream.connect()
        self.assertFalse(stream.disconnect(generation=old))
        self.assertTrue(stream._connected)
        self.assertEqual(stream._generation, current)


if __name__ == "__main__":
    unittest.main()


import json
import pytest


@pytest.mark.parametrize('kind', ['quote', 'depth', 'kline', 'clob', 'binance'])
def test_real_reconnect_callbacks_keep_their_original_socket_generation(kind):
    constructors = {
        'quote': BybitQuoteStream, 'depth': BybitOrderbookWS,
        'kline': BybitKlineWS, 'clob': PolymarketMarketWS,
        'binance': BinanceTradeWS,
    }
    stream = constructors[kind]()
    stream.subscribe('123' if kind == 'clob' else 'ADAUSDT')
    apps = []
    checked = []

    class App:
        def __init__(self, endpoint, *, on_open, on_message, on_close):
            self.opened, self.received, self.closed = on_open, on_message, on_close
            apps.append(self)

        def send(self, data):
            pass

        def close(self):
            pass

        def run_forever(self, **kwargs):
            self.opened(self)
            if len(apps) == 1:
                return  # Transport loss starts a genuinely new run-loop iteration.
            current = stream._generation
            old = apps[0]
            try:
                old.closed(old, 1006, 'delayed old close')
                assert stream._connected
                assert stream._generation == current
                old.received(old, json.dumps({
                    'op': 'subscribe', 'success': False,
                    'code': 1, 'msg': 'late error', 'event_type': 'error',
                }))
                assert stream._connected
                assert stream._generation == current
                checked.append(True)
            finally:
                stream.stop()

    stream.run(websocket_app_factory=App, retry_delay=.1)
    assert checked == [True]
    assert len(apps) == 2
