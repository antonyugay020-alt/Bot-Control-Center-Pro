"""Offline producer-subscription lifecycle regression; no live market connections."""
import json
import unittest

from bcc.ws_quotes import BybitQuoteStream, StreamUnavailable


class SocketTransportTests(unittest.TestCase):
    def test_running_socket_receives_new_subscription_and_stops(self):
        stream = BybitQuoteStream(wall=lambda: 1763000000.0)
        stream.subscribe("BTCUSDT")

        class FakeSocket:
            def __init__(self, url, **callbacks):
                self.callbacks = callbacks
                self.sent = []
                self.closed = False

            def send(self, raw):
                self.sent.append(json.loads(raw))

            def close(self):
                self.closed = True

            def run_forever(self, **kwargs):
                self.callbacks["on_open"](self)
                assert self.sent[0]["args"] == ["tickers.BTCUSDT"]
                stream.subscribe("ETHUSDT")
                assert self.sent[-1]["args"] == ["tickers.ETHUSDT"]
                stream.stop()
                assert self.closed

        stream.run(websocket_app_factory=FakeSocket, retry_delay=0.1)
        with self.assertRaises(StreamUnavailable):
            stream.quote("BTCUSDT")

    def test_subscription_send_failure_invalidates_connection(self):
        stream = BybitQuoteStream(wall=lambda: 1763000000.0)
        stream.subscribe("BTCUSDT")
        generation = stream.connect()
        assert stream.ingest({
            "type": "snapshot", "topic": "tickers.BTCUSDT",
            "ts": 1763000000000,
            "data": {"symbol": "BTCUSDT", "bid1Price": "100", "ask1Price": "101"},
        }, generation=generation)

        class FailingSocket:
            def send(self, raw):
                raise OSError("broken socket")

            def close(self):
                pass

        stream._socket = FailingSocket()
        with self.assertRaises(StreamUnavailable):
            stream.subscribe("ETHUSDT")
        with self.assertRaises(StreamUnavailable):
            stream.quote("BTCUSDT")

    def test_non_finite_limits_rejected(self):
        for value in (float("nan"), float("inf"), -1, 0):
            with self.assertRaises(ValueError):
                BybitQuoteStream(max_age=value)
