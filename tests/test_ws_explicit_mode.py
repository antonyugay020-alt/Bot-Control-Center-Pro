"""Opt-in WS paths must fail closed if a producer is absent.

Historical candles and broad risk marks remain REST to avoid disabling
protective liquidation/risk calculations on partially migrated providers.
"""
import os
import unittest
from unittest.mock import patch

from bcc.market import DataUnavailable, PublicData


class ExplicitModeTests(unittest.TestCase):
    def setUp(self):
        env = dict(os.environ)
        for key in ("BCC_PUBLIC_CACHE", "BCC_MARKET_HUB_MODE",
                    "BCC_BYBIT_WS_QUOTES", "BCC_BYBIT_WS_BOOKS",
                    "BCC_BYBIT_WS_KLINES"):
            env.pop(key, None)
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.source = PublicData()

    def tearDown(self):
        self.source.close()
        self.env.stop()

    def test_ws_quote_no_rest_without_hub(self):
        os.environ["BCC_BYBIT_WS_QUOTES"] = "1"
        with patch("requests.sessions.Session.get",
                   side_effect=AssertionError("HTTP should be unreachable")):
            with self.assertRaises(DataUnavailable):
                self.source.get("https://api.bybit.com/v5/market/tickers",
                                {"category": "linear", "symbol": "BTCUSDT"})

    def test_ws_depth50_no_rest_without_hub(self):
        os.environ["BCC_BYBIT_WS_BOOKS"] = "1"
        with patch("requests.sessions.Session.get",
                   side_effect=AssertionError("HTTP should be unreachable")):
            with self.assertRaises(DataUnavailable):
                self.source.get("https://api.bybit.com/v5/market/orderbook",
                                {"category": "linear", "symbol": "BTCUSDT", "limit": 50})

    def test_ws_minute_tail_no_rest_without_hub(self):
        os.environ["BCC_BYBIT_WS_KLINES"] = "1"
        with patch("requests.sessions.Session.get",
                   side_effect=AssertionError("HTTP should be unreachable")):
            with self.assertRaises(DataUnavailable):
                self.source.get("https://api.bybit.com/v5/market/kline",
                                {"category": "linear", "symbol": "BTCUSDT",
                                 "interval": "1", "limit": 10})

    def test_historical_kline_still_routes_to_rest(self):
        os.environ["BCC_BYBIT_WS_KLINES"] = "1"
        with patch("requests.sessions.Session.get",
                   side_effect=AssertionError("history route remains REST")):
            with self.assertRaisesRegex(AssertionError, "history route remains REST"):
                self.source.get("https://api.bybit.com/v5/market/kline",
                                {"category": "linear", "symbol": "BTCUSDT",
                                 "interval": "1", "limit": 500})


if __name__ == "__main__":
    unittest.main()
