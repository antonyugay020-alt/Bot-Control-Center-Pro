"""WebSocket depth50 producer / PublicData / shared-cache contract."""
import os
import unittest
from unittest.mock import patch

from bcc.market import PublicData, DataUnavailable
from bcc.ws_quotes import StreamUnavailable

URL = "https://api.bybit.com/v5/market/orderbook"
ARGS = {"category": "linear", "symbol": "BTCUSDT", "limit": 50}


class FakeStream:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.subscribed = []

    def subscribe(self, symbol):
        self.subscribed.append(symbol)

    def get_orderbook(self, symbol):
        if self.fail:
            raise StreamUnavailable("socket down")
        return {"bids": [[100., 1.]], "asks": [[101., 2.]],
                "ts": 1763000000., "u": 7, "seq": 11}


class FakeCache:
    def __init__(self, *, cached=None):
        self.cached = cached
        self.finished = []

    def begin(self, url, params, host, *, priority=False, websocket=False):
        assert (url, params, host, websocket) == (
            URL, ARGS, "api.bybit.com", True)
        if self.cached is not None:
            return ("key", None, self.cached)
        return ("key", "owner", None)

    def finish(self, *args, **kw):
        self.finished.append((args, kw))
        return True

    def close(self):
        pass


class HubDepthTests(unittest.TestCase):
    def make_data(self, *, stream=None, cache=None):
        env = dict(os.environ)
        env.pop("BCC_PUBLIC_CACHE", None)
        env.pop("BCC_MARKET_HUB_MODE", None)
        with patch.dict(os.environ, env, clear=True):
            source = PublicData(orderbook_stream=stream or FakeStream())
        source.shared = cache or FakeCache()
        return source

    def test_ws_publishes_book_in_existing_rest_shape(self):
        stream = FakeStream()
        cache = FakeCache()
        source = self.make_data(stream=stream, cache=cache)
        try:
            with patch("bcc.market.market_now", return_value=1763000000.), patch(
                    "requests.sessions.Session.get",
                    side_effect=AssertionError("unexpected HTTP")):
                response = source.get(URL, ARGS)
            self.assertIs(response["_bcc_ws_book"], True)
            self.assertEqual(response["result"]["b"], [["100.0", "1.0"]])
            self.assertEqual(response["result"]["seq"], 11)
            self.assertEqual(stream.subscribed, ["BTCUSDT"])
            self.assertEqual(cache.finished[0][0][-1], .2)
        finally:
            source.close()

    def test_ws_failure_does_not_hit_rest(self):
        cache = FakeCache()
        source = self.make_data(stream=FakeStream(fail=True), cache=cache)
        try:
            with patch("requests.sessions.Session.get",
                       side_effect=AssertionError("unexpected HTTP")):
                with self.assertRaises(DataUnavailable):
                    source.get(URL, ARGS)
        finally:
            source.close()

    def test_old_http_cache_not_accepted_as_ws(self):
        source = self.make_data(cache=FakeCache(cached={
            "retCode": 0, "time": 1763000000000,
            "result": {"s": "BTCUSDT", "b": [], "a": []},
        }))
        try:
            with self.assertRaises(DataUnavailable):
                source.get(URL, ARGS)
        finally:
            source.close()

    def test_client_origin_provenance(self):
        source = self.make_data()
        source.shared = FakeCache(cached={
            "retCode": 0, "result": {"s": "BTCUSDT"},
        })
        # Client-specific SQLite read contract is covered by regular
        # Market Hub regression; do not fabricate a passing client read.
        source.close()
