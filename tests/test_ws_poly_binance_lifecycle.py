"""Synthetic CLOB/Binance public socket startup and shutdown acceptance."""
import json
import time

from bcc.ws_polymarket import PolymarketMarketWS
from bcc.ws_binance import BinanceTradeWS


def test_clob_initial_channel_and_first_book():
    stream = PolymarketMarketWS()
    stream.subscribe(["123", "456"])

    class Socket:
        def __init__(self, url, **callbacks):
            self.callbacks = callbacks
            self.messages = []
            self.closed = False
            assert url == PolymarketMarketWS.ENDPOINT

        def send(self, value):
            self.messages.append(value)

        def close(self):
            self.closed = True

        def run_forever(self, **kwargs):
            self.callbacks["on_open"](self)
            assert json.loads(self.messages[0]) == {
                "assets_ids": ["123", "456"], "type": "market"}
            self.callbacks["on_message"](self, json.dumps([{
                "event_type": "book", "asset_id": "123",
                "timestamp": str(int(time.time() * 1000)),
                "bids": [{"price": "0.4", "size": "9"}],
                "asks": [{"price": "0.5", "size": "9"}],
            }]))
            assert stream.get_book("123")["asks"][0]["price"] == "0.5"
            stream.stop()
            assert self.closed

    stream.run(websocket_app_factory=Socket)


def test_binance_first_trade_then_disconnect():
    stream = BinanceTradeWS()
    stream.subscribe("BTCUSDT")

    class Socket:
        def __init__(self, url, **callbacks):
            self.callbacks = callbacks
            self.sent = []
            self.closed = False
            assert url == BinanceTradeWS.ENDPOINT

        def send(self, value):
            self.sent.append(value)

        def close(self):
            self.closed = True

        def run_forever(self, **kwargs):
            self.callbacks["on_open"](self)
            assert json.loads(self.sent[0])["params"] == ["btcusdt@trade"]
            self.callbacks["on_message"](self, json.dumps({
                "e": "trade", "s": "BTCUSDT", "p": "100",
                "T": int(time.time() * 1000),
            }))
            assert stream.quote("BTCUSDT")[0] == 100
            stream.stop()
            assert self.closed

    stream.run(websocket_app_factory=Socket)
