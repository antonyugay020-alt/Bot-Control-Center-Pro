"""Offline WS -> PublicData -> shared-cache producer integration.

No network or trading account required. Existing REST paths are unchanged
unless the Market Hub is explicitly started with a quote stream.
"""
import os
import unittest
from unittest.mock import patch

from bcc.market import PublicData, DataUnavailable
from bcc.ws_quotes import StreamUnavailable


URL = "https://api.bybit.com/v5/market/tickers"
PARAMS = {"category": "linear", "symbol": "BTCUSDT"}


class FakeQuote:
    def __init__(self, *, fail=False):
        self.symbols = []
        self.fail = fail

    def subscribe(self, symbol):
        self.symbols.append(symbol)

    def quote(self, symbol):
        if self.fail:
            raise StreamUnavailable("stream disconnected")
        return (100., 101., 1763000000.)


class FakeCache:
    def __init__(self, *, cached=None):
        self.cached = cached
        self.finishes = []

    def begin(self, url, params, host, *, priority=False):
        assert (url, params, host) == (URL, PARAMS, "api.bybit.com")
        if self.cached is not None:
            return ("key", None, self.cached)
        return ("key", "owner", None)

    def finish(self, *args, **kwargs):
        self.finishes.append((args, kwargs))
        return True

    def close(self):
        pass


class HubWsQuoteTests(unittest.TestCase):
    def make_source(self, *, quote=None, cache=None):
        env = dict(os.environ)
        env.pop("BCC_MARKET_HUB_MODE", None)
        env.pop("BCC_PUBLIC_CACHE", None)
        with patch.dict(os.environ, env, clear=True):
            source = PublicData(quote_stream=quote or FakeQuote())
        source.shared = cache or FakeCache()
        return source

    def test_public_get_publishes_ws_without_http(self):
        quote = FakeQuote()
        cache = FakeCache()
        source = self.make_source(quote=quote, cache=cache)
        try:
            with patch("bcc.market.market_now", return_value=1763000000.), patch(
                "requests.sessions.Session.get", side_effect=AssertionError("REST called")
            ):
                data = source.get(URL, PARAMS)
            self.assertEqual(data["result"]["list"][0]["bid1Price"], "100.0")
            self.assertIs(data["_bcc_ws_quote"], True)
            self.assertEqual(quote.symbols, ["BTCUSDT"])
            self.assertEqual(len(cache.finishes), 1)
            self.assertEqual(cache.finishes[0][0][-1], .35)
        finally:
            source.close()

    def test_ws_absent_never_falls_back_to_rest(self):
        cache = FakeCache()
        source = self.make_source(quote=FakeQuote(fail=True), cache=cache)
        try:
            with self.assertRaises(DataUnavailable):
                source.get(URL, PARAMS)
            self.assertEqual(len(cache.finishes), 1)
            self.assertEqual(cache.finishes[0][1]["cooldown"], .25)
        finally:
            source.close()

    def test_historic_rest_cache_does_not_count_as_ws(self):
        cache = FakeCache(cached={"retCode": 0, "result": {"list": []}})
        source = self.make_source(cache=cache)
        try:
            with self.assertRaises(DataUnavailable):
                source.get(URL, PARAMS)
            self.assertEqual(cache.finishes, [])
        finally:
            source.close()

    def test_non_ticker_route_not_intercepted(self):
        source = self.make_source()
        try:
            self.assertFalse(
                "https://api.bybit.com/v5/market/kline" == URL
            )
        finally:
            source.close()
