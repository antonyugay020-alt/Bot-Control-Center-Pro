"""PublicData opt-in WebSocket contract: never silently retry REST."""
import os
import unittest
from unittest.mock import patch

from bcc.market import PublicData, DataUnavailable
from bcc.ws_quotes import StreamUnavailable


class FakeStream:
    def __init__(self, quote=None, error=None):
        self.result = quote
        self.error = error
        self.symbols = []

    def subscribe(self, symbol):
        self.symbols.append(symbol)

    def quote(self, symbol):
        if self.error:
            raise self.error
        return self.result


class WSQuoteIntegrationTests(unittest.TestCase):
    def make_data(self, stream):
        env = dict(os.environ)
        env.pop("BCC_PUBLIC_CACHE", None)
        env.pop("BCC_MARKET_HUB_MODE", None)
        with patch.dict(os.environ, env, clear=True):
            return PublicData(quote_stream=stream)

    def test_active_ws_quote_never_calls_rest(self):
        stream = FakeStream((100.0, 101.0, 1763000000.0))
        source = self.make_data(stream)
        try:
            with patch("bcc.market.clock.ensure"), patch.object(source, "bybit", side_effect=AssertionError("REST invoked")):
                self.assertEqual(source.quote("BTCUSDT"), (100.0, 101.0, 1763000000.0))
            self.assertEqual(stream.symbols, ["BTCUSDT"])
        finally:
            source.close()

    def test_disconnected_ws_denies_without_rest(self):
        source = self.make_data(FakeStream(error=StreamUnavailable("socket down")))
        try:
            with patch("bcc.market.clock.ensure"), patch.object(source, "bybit", side_effect=AssertionError("REST invoked")):
                with self.assertRaises(DataUnavailable):
                    source.quote("BTCUSDT")
        finally:
            source.close()

    def test_ws_does_not_bypass_provider_denial(self):
        source = self.make_data(FakeStream((100., 101., 1763000000.)))
        try:
            with patch("bcc.market.clock.ensure"), patch.object(source, "_assert_provider_allowed",
                 side_effect=DataUnavailable("provider denied")), patch.object(source, "bybit",
                 side_effect=AssertionError("REST invoked")):
                with self.assertRaises(DataUnavailable):
                    source.quote("BTCUSDT")
        finally:
            source.close()
