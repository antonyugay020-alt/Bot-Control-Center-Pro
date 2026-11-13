"""Minute-candle feed: fresh current bar; REST history remains independent."""
import unittest

from bcc.ws_klines import BybitKlineWS
from bcc.ws_quotes import StreamUnavailable


class Clock:
    wall = 1763000000.0
    mono = 100.0


class KlineTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.client = BybitKlineWS(
            wall=lambda: self.clock.wall,
            monotonic=lambda: self.clock.mono,
        )
        self.client.subscribe("BTCUSDT")
        self.gen = self.client.connect()

    def send(self, *, start=None, ts=None, volume="2", close="100.5",
             confirmed=False, generation=None):
        stamp = int(self.clock.wall * 1000) if ts is None else ts
        start = stamp // 60000 * 60000 if start is None else start
        return self.client.ingest({
            "type": "snapshot", "topic": "kline.1.BTCUSDT", "ts": stamp,
            "data": [{
                "start": start, "end": start + 59999, "interval": "1",
                "open": "100", "high": "101", "low": "99",
                "close": close, "volume": volume, "confirm": confirmed,
            }],
        }, generation=self.gen if generation is None else generation)

    def test_receives_current_candle(self):
        self.assertTrue(self.send())
        candle = self.client.get_candle("BTCUSDT")
        self.assertEqual(candle.close, 100.5)
        self.assertEqual(candle.end_ms, candle.start_ms + 59999)

    def test_rejects_bad_price_and_gap(self):
        self.assertFalse(self.send(close="NaN"))
        with self.assertRaises(StreamUnavailable):
            self.client.get_candle("BTCUSDT")

    def test_rejects_stale(self):
        self.assertTrue(self.send())
        self.clock.mono += 10.1
        with self.assertRaises(StreamUnavailable):
            self.client.get_candle("BTCUSDT")

    def test_disconnect_invalidates(self):
        self.assertTrue(self.send())
        self.client.disconnect()
        with self.assertRaises(StreamUnavailable):
            self.client.get_candle("BTCUSDT")
        new = self.client.connect()
        self.assertFalse(self.send(generation=self.gen))
        self.gen = new
        self.assertTrue(self.send())

    def test_confirmation_never_rolls_back(self):
        self.assertTrue(self.send(confirmed=True))
        self.assertFalse(self.send(confirmed=False))
        self.assertTrue(self.client.get_candle("BTCUSDT").confirmed)

    def test_wrong_symbol_rejected(self):
        self.assertFalse(self.client.ingest({
            "type": "snapshot", "topic": "kline.1.ETHUSDT", "ts": int(self.clock.wall*1000),
            "data": [{
                "start": int(self.clock.wall*1000)//60000*60000,
                "end": int(self.clock.wall*1000)//60000*60000 + 59999,
                "interval": "1", "open": "100", "high": "101",
                "low": "99", "close": "100", "volume": "1", "confirm": False,
            }],
        }, generation=self.gen))


if __name__ == "__main__":
    unittest.main()
