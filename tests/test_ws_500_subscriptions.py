"""500-market synthetic WebSocket subscription admission; no exchange calls."""
import json

from bcc.ws_quotes import BybitQuoteStream
from bcc.ws_orderbooks import BybitOrderbookWS


class Sink:
    def __init__(self):
        self.messages = []

    def send(self, payload):
        self.messages.append(json.loads(payload))

    def close(self):
        pass


def test_five_hundred_bybit_quotes_are_batched():
    ws = BybitQuoteStream()
    sink = Sink()
    ws.connect(sink)
    symbols = [f"COIN{i}USDT" for i in range(500)]
    ws.subscribe_many(symbols)
    assert len(sink.messages) == 50
    assert all(len(msg["args"]) == 10 for msg in sink.messages)
    topics = {topic for msg in sink.messages for topic in msg["args"]}
    assert topics == {"tickers." + symbol for symbol in symbols}
    ws.disconnect()


def test_five_hundred_bybit_books_are_batched():
    ws = BybitOrderbookWS()
    sink = Sink()
    ws.connect(sink)
    symbols = [f"COIN{i}USDT" for i in range(500)]
    ws.subscribe(symbols)
    assert len(sink.messages) == 50
    assert all(len(msg["args"]) == 10 for msg in sink.messages)
    topics = {topic for msg in sink.messages for topic in msg["args"]}
    assert topics == {"orderbook.50." + symbol for symbol in symbols}
    ws.disconnect()
