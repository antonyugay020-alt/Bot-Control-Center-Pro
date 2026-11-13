"""Offline lifecycle acceptance for a fake Bybit socket; never contacts exchange."""
import json
import unittest

from bcc.ws_quotes import BybitQuoteStream, StreamUnavailable


class SocketLifecycleTests(unittest.TestCase):
    def test_subscription_and_close_clears_quotes(self):
        stream = BybitQuoteStream(wall=lambda: 1763000000.0)
        stream.subscribe("BTCUSDT")

        class FakeApp:
            def __init__(self, endpoint, *, on_open, on_message, on_close):
                self.endpoint = endpoint
                self.on_open, self.on_message, self.on_close = on_open, on_message, on_close
                self.sent = []

            def send(self, raw):
                self.sent.append(json.loads(raw))

            def close(self):
                pass

            def run_forever(self, **kwargs):
                self.on_open(self)
                assert self.sent == [{"op": "subscribe", "args": ["tickers.BTCUSDT"]}]
                self.on_message(self, json.dumps({
                    "type": "snapshot", "topic": "tickers.BTCUSDT",
                    "ts": 1763000000000,
                    "data": {"symbol": "BTCUSDT", "bid1Price": "100", "ask1Price": "101"}
                }))
                assert stream.quote("BTCUSDT")[0] == 100
                stream.stop()
                self.on_close(self, 1000, "shutdown")

        stream.run(websocket_app_factory=FakeApp, retry_delay=.1)
        with self.assertRaises(StreamUnavailable):
            stream.quote("BTCUSDT")
