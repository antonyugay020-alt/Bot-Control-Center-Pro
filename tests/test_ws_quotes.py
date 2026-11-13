"""Offline safety regressions for the opt-in public quote WebSocket reader."""
import unittest

from bcc.ws_quotes import BybitQuoteStream, StreamUnavailable


class Clock:
    def __init__(self):
        self.wall = 1763000000.0
        self.mono = 20.0


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.stream = BybitQuoteStream(max_age=2, monotonic=lambda: self.clock.mono,
                                      wall=lambda: self.clock.wall)
        self.stream.subscribe("BTCUSDT")
        self.generation = self.stream.connect()

    def feed(self, data=None, ts=None, topic="tickers.BTCUSDT", generation=None):
        return self.stream.ingest({
            "type": "snapshot", "topic": topic,
            "ts": int(self.clock.wall * 1000) if ts is None else ts,
            "data": data or {
                "symbol": "BTCUSDT", "bid1Price": "100", "ask1Price": "101"
            }
        }, generation=self.generation if generation is None else generation)

    def test_fresh(self):
        self.assertTrue(self.feed())
        self.assertEqual(self.stream.quote("BTCUSDT")[:2], (100.0, 101.0))

    def test_partial_delta_keeps_independent_other_side_age(self):
        self.assertTrue(self.feed())
        self.clock.mono += 1
        self.clock.wall += 1
        self.assertTrue(self.stream.ingest({
            "type": "delta", "topic": "tickers.BTCUSDT",
            "ts": int(self.clock.wall * 1000),
            "data": {"symbol": "BTCUSDT", "bid1Price": "100.5"},
        }, generation=self.generation))
        self.assertEqual(self.stream.quote("BTCUSDT")[:2], (100.5, 101.))
        self.clock.mono += 1.1
        self.clock.wall += 1.1
        # A new bid never renews the untouched ask that is now >2s old.
        self.assertFalse(self.stream.ingest({
            "type": "delta", "topic": "tickers.BTCUSDT",
            "ts": int(self.clock.wall * 1000),
            "data": {"symbol": "BTCUSDT", "bid1Price": "100.6"},
        }, generation=self.generation))
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")

    def test_partial_delta_before_snapshot_denied(self):
        self.assertFalse(self.stream.ingest({
            "type": "delta", "topic": "tickers.BTCUSDT",
            "ts": int(self.clock.wall * 1000),
            "data": {"symbol": "BTCUSDT", "bid1Price": "100"},
        }, generation=self.generation))
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")

    def test_stale(self):
        self.feed()
        self.clock.mono += 2.01
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")

    def test_disconnected_and_old_epoch(self):
        self.feed()
        self.stream.disconnect()
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")
        new_generation = self.stream.connect()
        self.assertFalse(self.feed(generation=self.generation))
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")
        self.assertTrue(self.feed(generation=new_generation))

    def test_reject_bad_prices_and_crossed_book(self):
        for bid, ask in [("nan", "1"), ("2", "1"), ("0", "1"), ("inf", "2")]:
            self.assertFalse(self.feed({
                "symbol": "BTCUSDT", "bid1Price": bid, "ask1Price": ask
            }))
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")

    def test_no_other_symbol_or_topic(self):
        self.assertFalse(self.feed(topic="tickers.ETHUSDT"))
        self.assertFalse(self.feed(topic="kline.1.BTCUSDT"))
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")

    def test_exchange_timestamp_regression(self):
        self.feed()
        self.assertFalse(self.feed(ts=int(self.clock.wall * 1000) - 1))
        self.assertEqual(self.stream.quote("BTCUSDT")[0], 100)

    def test_wall_freshness(self):
        self.feed()
        self.clock.wall += 3
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")

    def test_input_validation(self):
        for symbol in ("btcUSDT", "BTC", "BTC/USDT", "BTCUSDT\\n"):
            with self.assertRaises(ValueError):
                self.stream.subscribe(symbol)


if __name__ == "__main__":
    unittest.main()
