"""Bybit public ticker WS: partial-delta and epoch safety regressions."""
import unittest

from bcc.ws_quotes import BybitQuoteStream, StreamUnavailable


class Clock:
    wall = 1763000000.0
    mono = 20.0


class PartialDeltaTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.stream = BybitQuoteStream(
            max_age=2.0,
            wall=lambda: self.clock.wall,
            monotonic=lambda: self.clock.mono,
        )
        self.stream.subscribe("BTCUSDT")
        self.generation = self.stream.connect()

    def push(self, kind, data, *, generation=None, timestamp=None):
        return self.stream.ingest({
            "topic": "tickers.BTCUSDT",
            "type": kind,
            "ts": int(self.clock.wall * 1000) if timestamp is None else timestamp,
            "data": data,
        }, generation=self.generation if generation is None else generation)

    def snapshot(self):
        return self.push("snapshot", {
            "symbol": "BTCUSDT",
            "bid1Price": "100", "ask1Price": "101",
        })

    def test_delta_missing_symbol_and_ask_uses_verified_snapshot(self):
        self.assertTrue(self.snapshot())
        self.clock.mono += .1
        self.clock.wall += .1
        self.assertTrue(self.push("delta", {"bid1Price": "100.5"}))
        self.assertEqual(self.stream.quote("BTCUSDT")[:2], (100.5, 101.0))

    def test_delta_without_initial_snapshot_fails_closed(self):
        self.assertFalse(self.push("delta", {"bid1Price": "100.5"}))
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")

    def test_one_sided_deltas_do_not_refresh_other_side(self):
        self.assertTrue(self.snapshot())
        self.clock.wall += 1.9
        self.clock.mono += 1.9
        self.assertTrue(self.push("delta", {"bid1Price": "100.5"}))
        self.clock.wall += .2
        self.clock.mono += .2
        self.assertFalse(self.push("delta", {"bid1Price": "100.6"}))
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")

    def test_crossed_book_delta_invalidates_previous_quote(self):
        self.snapshot()
        self.assertFalse(self.push("delta", {"bid1Price": "200"}))
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")

    def test_invalid_delta_value_invalidates_previous_quote(self):
        self.snapshot()
        self.assertFalse(self.push("delta", {"ask1Price": "NaN"}))
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")

    def test_old_socket_delta_cannot_enter_new_generation(self):
        self.snapshot()
        old = self.generation
        self.stream.disconnect()
        self.generation = self.stream.connect()
        self.assertFalse(self.push("delta", {"bid1Price": "100.5"},
                                   generation=old))
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")
        self.assertTrue(self.snapshot())

    def test_out_of_order_delta_does_not_overwrite_latest(self):
        self.snapshot()
        self.clock.wall += .1
        self.clock.mono += .1
        self.assertTrue(self.push("delta", {"bid1Price": "100.5"}))
        self.assertFalse(self.push("delta", {"bid1Price": "99"},
                                   timestamp=int((self.clock.wall - .2) * 1000)))
        self.assertEqual(self.stream.quote("BTCUSDT")[0], 100.5)


if __name__ == "__main__":
    unittest.main()
