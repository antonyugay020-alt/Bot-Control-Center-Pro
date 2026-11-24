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
import threading
import time
from dataclasses import dataclass
from .ws_dispatch import ReconnectBackoff, request_snapshots, MAX_REFRESH_TOPICS_PER_SCAN
from .ws_recovery import SnapshotRecovery

from .ws_quotes import StreamUnavailable

from .ws_symbols import USDT_SYMBOL as _VALID


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
        self._refresh_due = {}
        self._snapshot_recovery = SnapshotRecovery()
        self._priority_symbols = frozenset()
        self._refresh_backlog = 0
        self._verified_book_events = 0
        self._verified_book_snapshots = 0
        self._refresh_topics_sent = 0
        self._refresh_batches_sent = 0
        self._refresh_send_failures = 0
        self._connections_opened = 0

    def sync_symbols(self, symbols):
        from .ws_dispatch import synchronize_bybit_symbols
        return synchronize_bybit_symbols(
            self, symbols, subscribed=self._symbols, topic="orderbook.50",
            caches=("_books", "_refresh_due", "_snapshot_recovery"), subscribe=self.subscribe, error_type=StreamUnavailable,
        )

    def set_priority_symbols(self, symbols):
        """Trust only active Hub-held exposure, never a provider frame."""
        if not isinstance(symbols, (list, tuple, set)) or any(
                type(symbol) is not str or not _VALID.fullmatch(symbol)
                for symbol in symbols):
            raise ValueError("invalid priority book symbols")
        with self._lock:
            selected = frozenset(symbols)
            now = self._mono()
            for symbol in selected - self._priority_symbols:
                if symbol in self._symbols:
                    self._refresh_due[symbol] = min(
                        self._refresh_due.get(symbol, now + .5), now + .5)
            self._priority_symbols = selected

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
            for symbol in fresh:
                self._refresh_due[symbol] = self._mono() + 3.0
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
        self._refresh_due.clear()
        self._snapshot_recovery.clear()
        self._refresh_backlog = 0

    def disconnect(self, *, generation=None):
        with self._lock:
            if generation is not None and generation != self._generation:
                return False
            self._invalidate()

    def connect(self, socket=None):
        with self._lock:
            if self._stop.is_set():
                raise StreamUnavailable("orderbook transport stopped")
            self._invalidate()
            self._connections_opened += 1
            self._connected = True
            self._socket = socket
            self._refresh_due = {symbol: self._mono() + 3.0 for symbol in self._symbols}
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
            if prior is None:
                self._refresh_due[symbol] = min(
                    self._refresh_due.get(symbol, new.received_mono + 1.0),
                    new.received_mono + 1.0,
                )
            self._books[symbol] = new
            self._verified_book_events += 1
            if kind == "snapshot":
                self._verified_book_snapshots += 1
                self._snapshot_recovery.observe_snapshot(symbol, (new.ts,))
        if self.on_update is not None:
            try:
                self.on_update("depth50", symbol)
            except Exception:
                pass
        return True

    def refresh_stale_books(self, symbols):
        """Recover quiet depth through new WS events, never retimestamp depth."""
        if (not isinstance(symbols, (tuple, list, set)) or
                any(type(s) is not str or not _VALID.fullmatch(s) for s in symbols)):
            raise ValueError("invalid book refresh symbols")
        with self._lock:
            if not self._connected or self._socket is None:
                return 0
            now, wall = self._mono(), self._wall()
            lead_age = min(1.0, self._max_age / 2)
            stale = []
            deferred = 0
            for symbol in sorted(
                set(symbols) & self._symbols,
                key=lambda s: (s not in self._priority_symbols,
                               self._refresh_due.get(s, 0), s),
            ):
                book = self._books.get(symbol)
                fresh = (book is not None and book.generation == self._generation
                         and 0 <= now - book.received_mono < lead_age
                         and -.5 <= wall - book.ts < lead_age)
                if self._snapshot_recovery.waiting(symbol, now):
                    continue
                if fresh or now < self._refresh_due.get(symbol, 0):
                    continue
                if len(stale) >= MAX_REFRESH_TOPICS_PER_SCAN:
                    deferred += 1
                    continue
                stale.append(symbol)
                self._snapshot_recovery.begin(symbol, now, (book.ts if book is not None else -1,))
                self._refresh_due[symbol] = now + 1.0
            self._refresh_backlog = deferred
            if not stale:
                return 0
            socket = self._socket
            try:
                request_snapshots(socket, "orderbook.50", stale,
                                  batch_size=self.MAX_SUB_PER_MSG)
                self._refresh_topics_sent += len(stale)
                self._refresh_batches_sent += (len(stale) + self.MAX_SUB_PER_MSG - 1) // self.MAX_SUB_PER_MSG
            except Exception as exc:
                self._refresh_send_failures += 1
                self._invalidate()
                try:
                    socket.close()
                finally:
                    raise StreamUnavailable("orderbook refresh failed") from exc
            return len(stale)

    def refresh_pressure(self):
        with self._lock:
            return dict(deferred_symbols=self._refresh_backlog,
                        per_scan_budget=MAX_REFRESH_TOPICS_PER_SCAN,
                        genuine_book_events=self._verified_book_events,
                        genuine_book_snapshots=self._verified_book_snapshots,
                        snapshot_topics_requested=self._refresh_topics_sent,
                        snapshot_batches_requested=self._refresh_batches_sent,
                        snapshot_send_failures=self._refresh_send_failures,
                        connections_opened=self._connections_opened,
                        **self._snapshot_recovery.stats())

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
        reconnect = ReconnectBackoff(base=retry_delay)
        self._stop.clear()
        while not self._stop.is_set():
            started = time.monotonic()
            self.disconnect()
            # Bind this session into callback defaults: loop-local variables
            # otherwise alias the NEXT socket after a reconnect.
            state = {"generation": None}

            def opened(ws, state=state):
                generation = self.connect(ws)
                state["generation"] = generation
                with self._lock:
                    try:
                        self._send_subscriptions(ws, sorted(self._symbols))
                    except Exception:
                        self._invalidate()
                        ws.close()

            def received(ws, raw, state=state):
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    return
                if not isinstance(message, dict):
                    return
                if message.get("op") in ("subscribe", "unsubscribe") and message.get("success") is False:
                    self.disconnect(generation=state["generation"])
                    ws.close()
                    return
                self.ingest(message, generation=state["generation"])

            def closed(ws, code, reason, state=state):
                self.disconnect(generation=state["generation"])

            socket = websocket_app_factory(
                self.ENDPOINT, on_open=opened, on_message=received,
                on_close=closed)
            try:
                socket.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                # Public transport errors are retryable, not trade permission.
                logging.warning("Bybit public WS reconnect: %s", type(exc).__name__)
            finally:
                self.disconnect(generation=state["generation"])
            self._stop.wait(reconnect.after_session(
                max(0.0, time.monotonic() - started)))

    def stop(self):
        self._stop.set()
        with self._lock:
            socket = self._socket
        self.disconnect()
        if socket is not None:
            socket.close()
