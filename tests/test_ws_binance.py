"""Offline Binance last trade stream tests for Polymarket PAPER impulses."""
import json
import unittest
from bcc.ws_binance import BinanceTradeWS
from bcc.ws_quotes import StreamUnavailable


class Clock:
    wall = 1763000000.
    mono = 100.


class BinanceTradeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.stream = BinanceTradeWS(
            wall=lambda: self.clock.wall,
            monotonic=lambda: self.clock.mono,
        )
        self.stream.subscribe(["BTCUSDT", "ETHUSDT", "SOLUSDT",
                               "XRPUSDT", "DOGEUSDT", "HYPEUSDT", "ZECUSDT"])
        self.generation = self.stream.connect()

    def trade(self, *, symbol="BTCUSDT", price="100", stamp=None, generation=None):
        return self.stream.ingest({
            "e": "trade", "s": symbol, "p": price,
            "T": int(self.clock.wall * 1000) if stamp is None else stamp,
        }, generation=self.generation if generation is None else generation)

    def test_price_snapshot(self):
        self.assertTrue(self.trade())
        self.assertEqual(self.stream.quote("BTCUSDT")[0], 100.)

    def test_seven_independent_coins(self):
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
                       "DOGEUSDT", "HYPEUSDT", "ZECUSDT"):
            self.assertTrue(self.trade(symbol=symbol))
            self.assertEqual(self.stream.quote(symbol)[0], 100.)

    def test_bad_price_rejected(self):
        self.assertFalse(self.trade(price="NaN"))
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")

    def test_malformed_new_trade_revokes_prior_price(self):
        self.assertTrue(self.trade())
        self.assertFalse(self.trade(price="NaN"))
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")

    def test_stale_new_trade_revokes_prior_price(self):
        self.assertTrue(self.trade())
        self.clock.wall += 3
        self.assertFalse(self.trade(stamp=int((self.clock.wall - 6) * 1000)))
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")

    def test_absent_token_no_price(self):
        self.assertFalse(self.trade(symbol="UNKNOWNUSDT"))

    def test_disconnect_revokes_price(self):
        self.trade()
        old = self.generation
        self.stream.disconnect()
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")
        self.generation = self.stream.connect()
        self.assertFalse(self.trade(generation=old))
        self.assertTrue(self.trade())

    def test_out_of_order_price_ignored(self):
        self.trade()
        self.clock.wall += 0.1
        self.clock.mono += 0.1
        self.assertTrue(self.trade(price="101"))
        self.assertFalse(self.trade(price="50",
                                   stamp=int((self.clock.wall - 0.2) * 1000)))
        self.assertEqual(self.stream.quote("BTCUSDT")[0], 101.)

    def test_price_expires_without_trade(self):
        self.trade()
        self.clock.mono += 2.01
        with self.assertRaises(StreamUnavailable):
            self.stream.quote("BTCUSDT")

    def test_invalid_symbol_rejected(self):
        with self.assertRaises(ValueError):
            self.stream.subscribe(["BTC/USDT"])

    def test_subscription_batches(self):
        class Socket:
            def __init__(self):
                self.messages = []
            def send(self, value):
                self.messages.append(json.loads(value))
            def close(self):
                pass
        socket = Socket()
        stream = BinanceTradeWS()
        stream.connect(socket)
        stream.subscribe([f"COIN{i}USDT" for i in range(60)])
        self.assertEqual([len(msg["params"]) for msg in socket.messages],
                         [25, 25, 10])


if __name__ == "__main__":
    unittest.main()
