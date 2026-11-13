"""Fail-closed public Bybit linear ticker WebSocket reader.

No credentials, orders, REST fallback, or autonomous activation. One instance
may be shared among subscribing clients of a single market-data producer.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass


class StreamUnavailable(RuntimeError):
    pass


_SYMBOL = re.compile(r"[A-Z0-9]{2,24}USDT\Z")


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    exchange_ms: int
    received_monotonic: float
    generation: int


class BybitQuoteStream:
    """Caches fully observed bid/ask pairs per connection generation.

    Bybit ticker deltas may omit one side; never mix sides across messages.
    All values are dropped upon disconnect, reconnect, subscription failure,
    or a superseded generation. No live network starts without run().
    """

    ENDPOINT = "wss://stream.bybit.com/v5/public/linear"
    MAX_SUB_PER_MSG = 10

    def __init__(self, *, max_age=2.0, monotonic=time.monotonic, wall=time.time, on_update=None):
        if type(max_age) not in (int, float) or not math.isfinite(max_age) or not 0 < max_age <= 15:
            raise ValueError("invalid quote max_age")
        self._max_age = max_age
        self._mono, self._wall = monotonic, wall
        self.on_update = on_update
        self._lock = threading.RLock()
        self._generation = 0
        self._connected = False
        self._subscriptions = set()
        self._quotes = {}
        self._socket = None
        self._stop = threading.Event()

    def subscribe(self, symbol):
        self.subscribe_many([symbol])

    def subscribe_many(self, symbols):
        """One validated batched subscription change for an entire bot fleet."""
        if not isinstance(symbols, (list, tuple, set)) or any(
                type(symbol) is not str or not _SYMBOL.fullmatch(symbol)
                for symbol in symbols):
            raise ValueError("invalid linear USDT symbols")
        with self._lock:
            fresh = sorted(set(symbols) - self._subscriptions)
            if not fresh:
                return
            self._subscriptions.update(fresh)
            socket = self._socket if self._connected else None
            if socket is None:
                return
            try:
                for index in range(0, len(fresh), self.MAX_SUB_PER_MSG):
                    socket.send(json.dumps({
                        "op": "subscribe",
                        "args": ["tickers." + symbol
                                 for symbol in fresh[index:index + self.MAX_SUB_PER_MSG]],
                    }))
            except Exception as exc:
                self.disconnect()
                try:
                    socket.close()
                finally:
                    raise StreamUnavailable("Bybit subscription send failed") from exc

    def disconnect(self):
        with self._lock:
            self._generation += 1
            self._connected = False
            self._socket = None
            self._quotes.clear()

    def connect(self, socket=None):
        with self._lock:
            if self._stop.is_set():
                raise StreamUnavailable("Bybit WebSocket stopped")
            self._generation += 1
            self._connected = True
            self._socket = socket
            self._quotes.clear()
            return self._generation

    def ingest(self, payload, *, generation):
        """Accept complete Bybit v5 ticker messages only; ignore unrelated data."""
        if type(payload) is not dict or payload.get("type") not in ("snapshot", "delta"):
            return False
        topic = payload.get("topic")
        if type(topic) is not str or not topic.startswith("tickers."):
            return False
        symbol = topic[len("tickers."):]
        row = payload.get("data")
        if type(row) is not dict or row.get("symbol") != symbol:
            return False
        if "bid1Price" not in row or "ask1Price" not in row:
            return False
        try:
            bid = float(row["bid1Price"])
            ask = float(row["ask1Price"])
            ts = payload["ts"]
        except (ValueError, TypeError, KeyError, OverflowError):
            return False
        if (type(ts) is not int or not math.isfinite(bid) or
                not math.isfinite(ask) or bid <= 0 or ask <= 0 or bid > ask):
            return False
        now_wall = self._wall()
        if not -5 <= now_wall - ts / 1000 <= 2:
            return False
        with self._lock:
            if (not self._connected or generation != self._generation or
                    symbol not in self._subscriptions):
                return False
            previous = self._quotes.get(symbol)
            if previous is not None and ts < previous.exchange_ms:
                return False
            self._quotes[symbol] = Quote(symbol, bid, ask, ts, self._mono(), generation)
        if self.on_update is not None:
            try:
                self.on_update("tickers", symbol)
            except Exception:
                # Consumer failures never kill the independent receive loop.
                pass
        return True

    def quote(self, symbol):
        with self._lock:
            q = self._quotes.get(symbol)
            if (not self._connected or q is None or
                    q.generation != self._generation or
                    not 0 <= self._mono() - q.received_monotonic <= self._max_age or
                    not -5 <= self._wall() - q.exchange_ms / 1000 <= 2):
                raise StreamUnavailable("Bybit WebSocket quote absent or stale")
            return q.bid, q.ask, q.exchange_ms / 1000

    def run(self, *, websocket_app_factory=None, retry_delay=1.0):
        """Blocking producer loop; caller owns its thread and shutdown lifecycle.

        An injected factory allows offline tests without a network connection.
        """
        if websocket_app_factory is None:
            try:
                from websocket import WebSocketApp
            except ImportError as exc:
                raise StreamUnavailable("websocket-client dependency unavailable") from exc
            websocket_app_factory = WebSocketApp
        if type(retry_delay) not in (int, float) or not math.isfinite(retry_delay) or not 0.1 <= retry_delay <= 30:
            raise ValueError("invalid retry delay")
        self._stop.clear()
        while not self._stop.is_set():
            self.disconnect()
            state = {"generation": None}

            def on_open(ws):
                if self._stop.is_set():
                    ws.close()
                    return
                gen = self.connect(ws)
                state["generation"] = gen
                with self._lock:
                    topics = ["tickers." + s for s in sorted(self._subscriptions)]
                for index in range(0, len(topics), self.MAX_SUB_PER_MSG):
                    ws.send(json.dumps({
                        "op": "subscribe",
                        "args": topics[index:index + self.MAX_SUB_PER_MSG],
                    }))

            def on_message(ws, raw):
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    return
                if not isinstance(payload, dict):
                    return
                if payload.get("op") == "subscribe" and payload.get("success") is False:
                    self.disconnect()
                    ws.close()
                    return
                self.ingest(payload, generation=state["generation"])

            def on_close(ws, code, reason):
                self.disconnect()

            ws = websocket_app_factory(self.ENDPOINT, on_open=on_open,
                                       on_message=on_message, on_close=on_close)
            try:
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                logging.warning("Bybit public quote WS reconnect: %s", type(exc).__name__)
            finally:
                self.disconnect()
            self._stop.wait(retry_delay)

    def stop(self):
        self._stop.set()
        with self._lock:
            socket = self._socket
        self.disconnect()
        if socket is not None:
            socket.close()
