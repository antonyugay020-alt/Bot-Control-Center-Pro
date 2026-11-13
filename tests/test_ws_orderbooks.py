"""Offline Bybit V5 depth50 snapshot/delta integration.

Uses a synthetic producer clock. Never opens a socket or account connection.
"""
import unittest

from bcc.ws_orderbooks import BybitOrderbookWS
from bcc.ws_quotes import StreamUnavailable


class Depth50Tests(unittest.TestCase):
    def setUp(self):
        self.wall = 1763000000.
        self.mono = 100.
        self.book = BybitOrderbookWS(
            wall=lambda: self.wall, monotonic=lambda: self.mono)
        self.book.subscribe(["BTCUSDT", "ETHUSDT"])
        self.gen = self.book.connect()

    def publish(self, kind="snapshot", *, symbol="BTCUSDT", bids=None,
                asks=None, seq=1, update=1, stamp=None, generation=None):
        return self.book.ingest({
            "topic": "orderbook.50." + symbol,
            "type": kind,
            "ts": int(self.wall * 1000) if stamp is None else stamp,
            "data": {
                "s": symbol, "u": update, "seq": seq,
                "b": bids if bids is not None else [["100", "2"], ["99", "1"]],
                "a": asks if asks is not None else [["101", "3"], ["102", "1"]],
            },
        }, generation=self.gen if generation is None else generation)

    def test_snapshot(self):
        self.assertTrue(self.publish())
        data = self.book.get_orderbook("BTCUSDT")
        self.assertEqual(data["bids"], [[100., 2.], [99., 1.]])
        self.assertEqual(data["asks"], [[101., 3.], [102., 1.]])

    def test_delta_add_remove_and_sort(self):
        self.publish()
        self.assertTrue(self.publish("delta", bids=[["100", "0"], ["99.5", "4"]],
                                     asks=[["101", "1"]], seq=2, update=2))
        value = self.book.get_orderbook("BTCUSDT")
        self.assertEqual(value["bids"], [[99.5, 4.], [99., 1.]])
        self.assertEqual(value["asks"][0], [101., 1.])

    def test_delta_cannot_create_book(self):
        self.assertFalse(self.publish("delta", seq=2, update=2))
        with self.assertRaises(StreamUnavailable):
            self.book.get_orderbook("BTCUSDT")

    def test_bad_price_and_crossed_book_rejected(self):
        self.assertFalse(self.publish(bids=[["nan", "1"]]))
        self.assertFalse(self.publish(bids=[["105", "1"]]))
        with self.assertRaises(StreamUnavailable):
            self.book.get_orderbook("BTCUSDT")

    def test_disconnected_invalidates(self):
        self.publish()
        self.book.disconnect()
        with self.assertRaises(StreamUnavailable):
            self.book.get_orderbook("BTCUSDT")
        new = self.book.connect()
        self.assertFalse(self.publish(generation=self.gen))
        self.assertTrue(self.publish(generation=new))

    def test_old_message_rejected(self):
        self.publish()
        self.assertFalse(self.publish("delta", seq=1, update=1))

    def test_age_expires(self):
        self.publish()
        self.mono += 3.01
        with self.assertRaises(StreamUnavailable):
            self.book.get_orderbook("BTCUSDT")

    def test_snapshot_refresh_replaces_levels(self):
        self.publish()
        self.assertTrue(self.publish(bids=[["95", "3"]],
                                     asks=[["96", "2"]], seq=3, update=1))
        self.assertEqual(self.book.get_orderbook("BTCUSDT")["bids"], [[95., 3.]])

    def test_unsubscribed_symbol_rejected(self):
        self.assertFalse(self.publish(symbol="XRPUSDT"))
        with self.assertRaises(StreamUnavailable):
            self.book.get_orderbook("XRPUSDT")

    def test_stop_invalidates(self):
        self.publish()
        self.book.stop()
        with self.assertRaises(StreamUnavailable):
            self.book.get_orderbook("BTCUSDT")


if __name__ == "__main__":
    unittest.main()
