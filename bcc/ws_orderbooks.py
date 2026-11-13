"""Bybit V5 public orderbook. Adapted from user-supplied bootstrap_stage2.py.

One socket per Hub producer; subscription batches, snapshot + delta merge.
Unlike the bootstrap prototype, a delta never creates a book and no book
survives reconnect, disconnect, out-of-order updates or failed subscription.
No secrets, trade endpoints or automatic connection on import.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass

from .ws_quotes import StreamUnavailable

_VALID = re.compile(r"[A-Z0-9]{2,24}USDT\Z")


@dataclass(frozen=True)
class OrderBook:
    symbol: str
    bids: tuple
    asks: tuple
    ts: float
    update_id: int
    sequence: int
    generation: int
    received_mono: float


class BybitOrderbookWS:
    ENDPOINT = "wss://stream.bybit.com/v5/public/linear"
    MAX_SUB_PER_MSG = 10

    def __init__(self, *, monotonic=time.monotonic, wall=time.time, max_age=3.0, on_update=None):
        if (type(max_age) not in (float, int) or not math.isfinite(max_age)
                or not 0 < max_age <= 3):
            raise ValueError("orderbook max_age must be within 3 seconds")
        self._mono, self._wall, self._max_age = monotonic, wall, max_age
        self.on_update = on_update
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._socket = None
        self._generation = 0
        self._connected = False
        self._symbols = set()
        self._books = {}

    def subscribe(self, symbols):
        if type(symbols) is str:
            symbols = [symbols]
        if not isinstance(symbols, (tuple, list, set)):
            raise ValueError("invalid symbol collection")
        if any(type(s) is not str or not _VALID.fullmatch(s) for s in symbols):
            raise ValueError("invalid Bybit symbol")
        with self._lock:
            fresh = sorted(set(symbols) - self._symbols)
            self._symbols.update(fresh)
            socket = self._socket if self._connected else None
            if socket is None:
                return
            try:
                self._send_subscriptions(socket, fresh)
            except Exception as exc:
                self._invalidate()
                try:
                    socket.close()
                finally:
                    raise StreamUnavailable("orderbook subscription failed") from exc

    def _send_subscriptions(self, socket, symbols):
        for index in range(0, len(symbols), self.MAX_SUB_PER_MSG):
            batch = symbols[index:index + self.MAX_SUB_PER_MSG]
            socket.send(json.dumps({
                "op": "subscribe",
                "args": ["orderbook.50." + symbol for symbol in batch],
            }))

    def _invalidate(self):
        self._generation += 1
        self._connected = False
        self._socket = None
        self._books.clear()

    def disconnect(self):
        with self._lock:
            self._invalidate()

    def connect(self, socket=None):
        with self._lock:
            if self._stop.is_set():
                raise StreamUnavailable("orderbook transport stopped")
            self._invalidate()
            self._connected = True
            self._socket = socket
            return self._generation

    @staticmethod
    def _levels(rows):
        if type(rows) is not list:
            raise ValueError("malformed orderbook levels")
        result = {}
        for row in rows:
            if type(row) is not list or len(row) != 2:
                raise ValueError("malformed orderbook level")
            price, qty = float(row[0]), float(row[1])
            if (not math.isfinite(price) or not math.isfinite(qty)
                    or price <= 0 or qty < 0 or price in result):
                raise ValueError("invalid orderbook price or quantity")
            result[price] = qty
        return result

    @staticmethod
    def _assemble(symbol, bids, asks, stamp, update_id, sequence, generation, now):
        bid_levels = tuple(sorted(((p, q) for p, q in bids.items() if q > 0),
                                  reverse=True))
        ask_levels = tuple(sorted((p, q) for p, q in asks.items() if q > 0))
        if (not bid_levels or not ask_levels or len(bid_levels) > 50 or
                len(ask_levels) > 50 or bid_levels[0][0] >= ask_levels[0][0]):
            raise ValueError("unusable or crossed orderbook")
        return OrderBook(symbol, bid_levels, ask_levels, stamp,
                         update_id, sequence, generation, now)

    def ingest(self, message, *, generation):
        if type(message) is not dict:
            return False
        topic = message.get("topic")
        if type(topic) is not str or not topic.startswith("orderbook.50."):
            return False
        symbol = topic[len("orderbook.50."):]
        kind = message.get("type")
        data = message.get("data")
        if kind not in ("snapshot", "delta") or type(data) is not dict or data.get("s") != symbol:
            return False
        try:
            stamp = message["ts"]
            update_id, sequence = data["u"], data["seq"]
            if (type(stamp) is not int or type(update_id) is not int
                    or type(sequence) is not int or update_id < 0 or sequence < 0
                    or not -0.5 <= self._wall() - stamp / 1000 <= self._max_age):
                return False
            bids = self._levels(data["b"])
            asks = self._levels(data["a"])
        except (KeyError, ValueError, TypeError, OverflowError):
            # An invalid delta cannot be applied safely, and keeping its
            # predecessor after a missed change can fabricate executable depth.
            with self._lock:
                if self._connected and generation == self._generation:
                    self._books.pop(symbol, None)
            return False
        with self._lock:
            if (not self._connected or generation != self._generation
                    or symbol not in self._symbols):
                return False
            prior = self._books.get(symbol)
            if kind == "delta":
                if prior is None or (sequence <= prior.sequence or
                                     update_id <= prior.update_id or
                                     stamp < prior.ts * 1000):
                    return False
                bids_base = dict(prior.bids)
                asks_base = dict(prior.asks)
                for target, change in ((bids_base, bids), (asks_base, asks)):
                    for price, quantity in change.items():
                        if quantity == 0:
                            target.pop(price, None)
                        else:
                            target[price] = quantity
                bids, asks = bids_base, asks_base
            try:
                new = self._assemble(symbol, bids, asks, stamp / 1000,
                                     update_id, sequence, generation, self._mono())
            except ValueError:
                # A malformed delta invalidates the whole chain: no partial book.
                self._books.pop(symbol, None)
                return False
            self._books[symbol] = new
        if self.on_update is not None:
            try:
                self.on_update("depth50", symbol)
            except Exception:
                pass
        return True

    def get_orderbook(self, symbol):
        with self._lock:
            book = self._books.get(symbol)
            if (not self._connected or book is None or
                    book.generation != self._generation or
                    not 0 <= self._mono() - book.received_mono <= self._max_age or
                    not -0.5 <= self._wall() - book.ts <= self._max_age):
                raise StreamUnavailable("Bybit orderbook absent or stale")
            return {
                "bids": [list(level) for level in book.bids],
                "asks": [list(level) for level in book.asks],
                "ts": book.ts,
                "u": book.update_id,
                "seq": book.sequence,
            }

    def run(self, *, websocket_app_factory=None, retry_delay=1):
        if websocket_app_factory is None:
            try:
                from websocket import WebSocketApp
            except ImportError as exc:
                raise StreamUnavailable("websocket-client dependency unavailable") from exc
            websocket_app_factory = WebSocketApp
        if type(retry_delay) not in (int, float) or not 0.1 <= retry_delay <= 30:
            raise ValueError("invalid retry delay")
        self._stop.clear()
        while not self._stop.is_set():
            self.disconnect()
            state = {"generation": None}

            def opened(ws):
                generation = self.connect(ws)
                state["generation"] = generation
                with self._lock:
                    try:
                        self._send_subscriptions(ws, sorted(self._symbols))
                    except Exception:
                        self._invalidate()
                        ws.close()

            def received(ws, raw):
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    return
                if not isinstance(message, dict):
                    return
                if message.get("op") == "subscribe" and message.get("success") is False:
                    self.disconnect()
                    ws.close()
                    return
                self.ingest(message, generation=state["generation"])

            def closed(ws, code, reason):
                self.disconnect()

            socket = websocket_app_factory(
                self.ENDPOINT, on_open=opened, on_message=received,
                on_close=closed)
            try:
                socket.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                # Public transport errors are retryable, not trade permission.
                logging.warning("Bybit public WS reconnect: %s", type(exc).__name__)
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
