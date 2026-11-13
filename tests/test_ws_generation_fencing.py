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
