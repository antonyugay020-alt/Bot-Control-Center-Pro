"""Fail-closed public Bybit linear ticker WebSocket reader.

No credentials, orders, REST fallback, or autonomous activation. One instance
may be shared among subscribing clients of a single market-data producer.
"""
from __future__ import annotations

import json
import logging
import math
import uuid
import threading
import time
from dataclasses import dataclass
from .ws_dispatch import ReconnectBackoff, request_snapshots, MAX_REFRESH_TOPICS_PER_SCAN
from .ws_recovery import SnapshotRecovery, SNAPSHOT_RESPONSE_TIMEOUT


class StreamUnavailable(RuntimeError):
    pass


from .ws_symbols import USDT_SYMBOL as _SYMBOL


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    exchange_ms: int
    received_monotonic: float
    generation: int
    bid_exchange_ms: int
    ask_exchange_ms: int
    bid_received_monotonic: float
    ask_received_monotonic: float


class BybitQuoteStream:
    """Caches quote sides with independent freshness within one WS generation.

    A delta may omit unchanged fields. The missing side is retained only
    while its OWN exchange/monotonic timestamp remains fresh; a one-sided
    update never renews its freshness. No mixing across reconnects.
    """

    ENDPOINT = "wss://stream.bybit.com/v5/public/linear"
    MAX_SUB_PER_MSG = 10

    def __init__(self, *, max_age=2.0, monotonic=time.monotonic, wall=time.time, on_update=None, mark_only_recovery=False):
        if type(max_age) not in (int, float) or not math.isfinite(max_age) or not 0 < max_age <= 15:
            raise ValueError("invalid quote max_age")
        if type(mark_only_recovery) is not bool:
            raise ValueError("invalid recovery policy")
        self._mark_only_recovery = mark_only_recovery
        self._max_age = max_age
        self._mono, self._wall = monotonic, wall
        self.on_update = on_update
        self._lock = threading.RLock()
        self._generation = 0
        # Unforgeable-in-process incarnation fence: monotonic integers restart
        # at zero on every producer process and cannot fence cache reuse alone.
        self._connection_fence = None
        self._connected = False
        self._subscriptions = set()
        self._priority_symbols = frozenset()
        self._quotes = {}
        # Exchange mark-price has its own event clock and must never be
        # fabricated from best bid/ask or renewed by quote-only deltas.
        self._marks = {}
        self._mark_refresh_due = {}
        self._snapshot_recovery = SnapshotRecovery()
        # Expose WS recovery debt; never pretend an unrefreshed mark is fresh.
        self._refresh_backlog = 0
        # Aggregate event provenance only, no prices/symbols/account identifiers.
        # Counters stay monotonic across reconnects for operational diagnosis.
        self._verified_mark_events = 0
        self._verified_quote_events = 0
        self._refresh_topics_sent = 0
        self._refresh_batches_sent = 0
        self._refresh_send_failures = 0
        self._connections_opened = 0
        self._socket = None
        self._stop = threading.Event()

    def sync_symbols(self, symbols):
        from .ws_dispatch import synchronize_bybit_symbols
        return synchronize_bybit_symbols(
            self, symbols, subscribed=self._subscriptions, topic="tickers",
            caches=("_quotes", "_marks", "_mark_refresh_due", "_snapshot_recovery"), subscribe=self.subscribe_many, error_type=StreamUnavailable,
        )

    def subscribe(self, symbol):
        self.subscribe_many([symbol])

    def set_priority_symbols(self, symbols):
        """Trusted Hub exposure demand, never a priority supplied by a frame."""
        if not isinstance(symbols, (list, tuple, set)) or any(
                type(symbol) is not str or not _SYMBOL.fullmatch(symbol) for symbol in symbols):
            raise ValueError("invalid priority symbols")
        with self._lock:
            selected = frozenset(symbols)
            for symbol in selected - self._priority_symbols:
                self._mark_refresh_due[symbol] = min(
                    self._mark_refresh_due.get(symbol, self._mono() + .5), self._mono() + .5)
            self._priority_symbols = selected

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
            for symbol in fresh:
                # Allow the initial exchange subscription snapshot to arrive
                # before requesting a recovery snapshot.
                self._mark_refresh_due[symbol] = self._mono() + (SNAPSHOT_RESPONSE_TIMEOUT if self._mark_only_recovery else 3.0)
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

    def disconnect(self, *, generation=None):
        with self._lock:
            if generation is not None and generation != self._generation:
                return False
            self._generation += 1
            self._connected = False
            self._connection_fence = None
            self._socket = None
            self._quotes.clear()
            self._marks.clear()
            self._mark_refresh_due.clear()
            self._snapshot_recovery.clear()
            self._refresh_backlog = 0

    def connect(self, socket=None):
        with self._lock:
            if self._stop.is_set():
                raise StreamUnavailable("Bybit WebSocket stopped")
            self._generation += 1
            self._connections_opened += 1
            self._connected = True
            self._connection_fence = uuid.uuid4().hex
            self._socket = socket
            self._quotes.clear()
            self._marks.clear()
            self._mark_refresh_due.clear()
            self._snapshot_recovery.clear()
            self._refresh_backlog = 0
            for symbol in self._subscriptions:
                self._mark_refresh_due[symbol] = self._mono() + (SNAPSHOT_RESPONSE_TIMEOUT if self._mark_only_recovery else 3.0)
            return self._generation

    def ingest(self, payload, *, generation):
        """Merge valid partial deltas without renewing the untouched quote side."""
        if type(payload) is not dict or payload.get("type") not in ("snapshot", "delta"):
            return False
        kind = payload["type"]
        topic = payload.get("topic")
        if type(topic) is not str or not topic.startswith("tickers."):
            return False
        symbol = topic[len("tickers."):]
        row = payload.get("data")
        if type(row) is not dict or row.get("symbol", symbol) != symbol:
            return False
        try:
            ts = payload["ts"]
            if type(ts) is not int or not -5 <= self._wall() - ts / 1000 <= 2:
                return False
            incoming = {}
            for name in ("bid1Price", "ask1Price"):
                if name in row:
                    value = float(row[name])
                    if not math.isfinite(value) or value <= 0:
                        raise ValueError("invalid ticker price")
                    incoming[name] = value
        except (ValueError, TypeError, KeyError, OverflowError):
            with self._lock:
                if self._connected and generation == self._generation:
                    self._quotes.pop(symbol, None)
            return False
        # Mark-price can arrive alone in a delta. Track it independently of
        # bid and ask, which may be absent or have their own older timestamps.
        mark = None
        if "markPrice" in row:
            try:
                mark = float(row["markPrice"])
                if not math.isfinite(mark) or mark <= 0:
                    raise ValueError("invalid Bybit mark-price")
            except (ValueError, TypeError, OverflowError):
                with self._lock:
                    if self._connected and generation == self._generation:
                        self._marks.pop(symbol, None)
                return False
        if not incoming and mark is None:
            return False
        with self._lock:
            if (not self._connected or generation != self._generation or
                    symbol not in self._subscriptions):
                return False
            previous = self._quotes.get(symbol)
            changed = False
            if mark is not None:
                old_mark = self._marks.get(symbol)
                if old_mark is None or ts > old_mark[1]:
                    received = self._mono()
                    self._marks[symbol] = (mark, ts, received, generation)
                    self._verified_mark_events += 1
                    if self._mark_only_recovery:
                        # Only a newer explicit mark completes collateral recovery.
                        self._snapshot_recovery.observe_snapshot(symbol, (ts,))
                    if old_mark is None:
                        # Handshake grace ends once the first genuine mark
                        # arrives. Keeping the initial three-second deadline
                        # would otherwise outlive its two-second evidence TTL.
                        grace = .5 if symbol in self._priority_symbols else 1.
                        self._mark_refresh_due[symbol] = min(
                            self._mark_refresh_due.get(symbol, received + grace), received + grace)
                    changed = True
            # Quote and mark ordering are independent. A newer executable
            # quote cannot suppress an otherwise newer mark observation.
            if incoming and (previous is None or ts >= previous.exchange_ms):
                if kind == "snapshot":
                    previous = None
                complete = set(incoming) == {"bid1Price", "ask1Price"}
                if previous is None and not complete:
                    self._quotes.pop(symbol, None)
                else:
                    now = self._mono()
                    bid = incoming.get("bid1Price", previous.bid if previous else None)
                    ask = incoming.get("ask1Price", previous.ask if previous else None)
                    if bid > ask:
                        self._quotes.pop(symbol, None)
                    else:
                        bid_ms = ts if "bid1Price" in incoming else previous.bid_exchange_ms
                        ask_ms = ts if "ask1Price" in incoming else previous.ask_exchange_ms
                        bid_mono = now if "bid1Price" in incoming else previous.bid_received_monotonic
                        ask_mono = now if "ask1Price" in incoming else previous.ask_received_monotonic
                        # Keep independently timestamped sides even during a
                        # stale interval. quote() still denies them; subsequent
                        # partial deltas can recover each side without waiting
                        # forever for an unsolicited full exchange snapshot.
                        self._quotes[symbol] = Quote(
                            symbol, bid, ask, ts, now, generation,
                            bid_ms, ask_ms, bid_mono, ask_mono,
                        )
                        self._verified_quote_events += 1
                        changed = changed or (
                            0 <= now - bid_mono <= self._max_age
                            and 0 <= now - ask_mono <= self._max_age
                            and -5 <= self._wall() - bid_ms / 1000 <= self._max_age
                            and -5 <= self._wall() - ask_ms / 1000 <= self._max_age
                        )
            if (not self._mark_only_recovery and kind == "snapshot"
                    and mark is not None and set(incoming) == {"bid1Price", "ask1Price"}):
                actual_mark = self._marks.get(symbol)
                actual_quote = self._quotes.get(symbol)
                if (actual_mark is not None and actual_quote is not None
                        and actual_mark[1] == ts
                        and actual_quote.bid_exchange_ms == ts
                        and actual_quote.ask_exchange_ms == ts):
                    self._snapshot_recovery.observe_snapshot(symbol, (ts, ts, ts))
            if not changed:
                return False
        if self.on_update is not None:
            try:
                self.on_update("tickers", symbol)
            except Exception:
                pass
        return True

    def _wire_refresh_pending(self, symbol):
        return False

    def _send_recovery_requests(self, symbols):
        request_snapshots(self._socket, "tickers", symbols,
                          batch_size=self.MAX_SUB_PER_MSG)

    def _handle_control_message(self, socket, payload, generation):
        return False

    def refresh_stale_marks(self, symbols, *, max_topics=MAX_REFRESH_TOPICS_PER_SCAN):
        """Request a NEW exchange ticker snapshot for quiet symbols.

        Never reuse or restamp old observations. A bounded unsubscribe/
        resubscribe on the established public socket asks Bybit to send a
        fresh snapshot before independent mark or quote sides expire. Background
        symbols are admitted in oldest-due batches of at most 20 per turn.
        Exposure keeps its half-second proactive lead, but an unresolved
        recovery is not repeated before its response timeout. Refresh begins before expiry,
        allowing a quiet symbol to retain genuine independent observations.
        The subsequent genuine WebSocket event is still required before
        margin/risk becomes available again.
        """
        if type(max_topics) is not int or not 1 <= max_topics <= MAX_REFRESH_TOPICS_PER_SCAN:
            raise ValueError("invalid mark refresh topic budget")
        if not isinstance(symbols, (list, tuple, set)):
            raise ValueError("invalid mark refresh symbols")
        if any(type(s) is not str or not _SYMBOL.fullmatch(s) for s in symbols):
            raise ValueError("invalid mark refresh symbol")
        with self._lock:
            if not self._connected or self._socket is None:
                return 0
            now = self._mono()
            wall = self._wall()
            stale = []
            # Held positions remain first, oldest due first. The burst
            # budget limits WS CONTROL traffic only, not portfolio size.
            # Deferred positions keep their original deadline and are served
            # on subsequent producer scans; collateral freshness is unchanged.
            background_selected = 0
            deferred = 0
            for symbol in sorted(
                set(symbols) & self._subscriptions,
                key=lambda symbol: (
                    symbol not in self._priority_symbols,
                    self._mark_refresh_due.get(symbol, 0),
                    symbol,
                ),
            ):
                priority = symbol in self._priority_symbols
                lead_age = self._max_age / (4 if priority else 2)
                row = self._marks.get(symbol)
                fresh = (row is not None and row[3] == self._generation
                         and 0 <= now - row[2] < lead_age
                         and -5 <= wall - row[1] / 1000 < lead_age)
                quote = self._quotes.get(symbol)
                quote_fresh = (
                    quote is not None and quote.generation == self._generation
                    and 0 <= now - quote.bid_received_monotonic < lead_age
                    and 0 <= now - quote.ask_received_monotonic < lead_age
                    and -5 <= wall - quote.bid_exchange_ms / 1000 < lead_age
                    and -5 <= wall - quote.ask_exchange_ms / 1000 < lead_age
                )
                if self._wire_refresh_pending(symbol) or self._snapshot_recovery.waiting(symbol, now):
                    continue
                if (fresh and (self._mark_only_recovery or quote_fresh)) or now < self._mark_refresh_due.get(symbol, 0):
                    continue
                if len(stale) >= max_topics or (not priority and background_selected >= 20):
                    deferred += 1
                    continue
                stale.append(symbol)
                stamps = (row[1] if row is not None else -1,)
                if not self._mark_only_recovery:
                    stamps += (quote.bid_exchange_ms if quote is not None else -1,
                               quote.ask_exchange_ms if quote is not None else -1)
                self._snapshot_recovery.begin(symbol, now, stamps)
                background_selected += int(not priority)
                self._mark_refresh_due[symbol] = now + (.5 if priority else 1.)
            self._refresh_backlog = deferred
            if not stale:
                return 0
            try:
                self._send_recovery_requests(stale)
                self._refresh_topics_sent += len(stale)
                self._refresh_batches_sent += (len(stale) + self.MAX_SUB_PER_MSG - 1) // self.MAX_SUB_PER_MSG
            except Exception as exc:
                self._refresh_send_failures += 1
                socket = self._socket
                self.disconnect()
                try:
                    socket.close()
                finally:
                    raise StreamUnavailable("Bybit mark refresh subscription failed") from exc
            return len(stale)

    def refresh_pressure(self):
        """Observable WS command debt, independent of verified data age."""
        with self._lock:
            return dict(deferred_symbols=self._refresh_backlog,
                        per_scan_budget=MAX_REFRESH_TOPICS_PER_SCAN,
                        genuine_mark_events=self._verified_mark_events,
                        genuine_quote_events=self._verified_quote_events,
                        snapshot_topics_requested=self._refresh_topics_sent,
                        snapshot_batches_requested=self._refresh_batches_sent,
                        snapshot_send_failures=self._refresh_send_failures,
                        connections_opened=self._connections_opened,
                        **self._snapshot_recovery.stats())

    def snapshot_for_hub(self, symbol):
        """Return quote/mark from ONE websocket incarnation, without REST.

        Missing mark/quote is reported independently. A reconnect while the
        Hub prepares a publication can never combine two generations.
        """
        with self._lock:
            if not self._connected:
                return None, None, None
            fence = self._connection_fence
            try:
                mark = self.mark(symbol)
            except StreamUnavailable:
                mark = None
            try:
                quote = self.quote(symbol)
            except StreamUnavailable:
                quote = None
            return mark, quote, fence

    def mark(self, symbol):
        """Independent real Bybit WS mark-price with its original event time.

        Quote-only updates never refresh this observation. A reconnect or a
        delta lacking markPrice does not create fresh collateral evidence.
        """
        with self._lock:
            row = self._marks.get(symbol)
            if not self._connected or row is None or row[3] != self._generation:
                raise StreamUnavailable("Bybit WebSocket mark-price absent")
            price, exchange_ms, received_mono, _ = row
            now = self._mono()
            if (not 0 <= now - received_mono <= self._max_age
                    or not -5 <= self._wall() - exchange_ms / 1000 <= self._max_age):
                raise StreamUnavailable("Bybit WebSocket mark-price stale")
            return price, exchange_ms / 1000

    def quote(self, symbol):
        with self._lock:
            q = self._quotes.get(symbol)
            now = self._mono()
            wall = self._wall()
            if (not self._connected or q is None or
                    q.generation != self._generation or
                    not 0 <= now - q.bid_received_monotonic <= self._max_age or
                    not 0 <= now - q.ask_received_monotonic <= self._max_age or
                    not -5 <= wall - q.bid_exchange_ms / 1000 <= self._max_age or
                    not -5 <= wall - q.ask_exchange_ms / 1000 <= self._max_age):
                raise StreamUnavailable("Bybit WebSocket quote absent or stale")
            # Conservative exchange time: one-sided delta does not refresh
            # the timestamp of the unmodified executable quote side.
            return q.bid, q.ask, min(q.bid_exchange_ms, q.ask_exchange_ms) / 1000

    def run(self, *, websocket_app_factory=None, retry_delay=1.0, reset_stop=True):
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
        reconnect = ReconnectBackoff(base=retry_delay)
        if reset_stop:
            self._stop.clear()
        while not self._stop.is_set():
            started = time.monotonic()
            self.disconnect()
            # Bind this session into callback defaults: loop-local variables
            # otherwise alias the NEXT socket after a reconnect.
            state = {"generation": None}

            def on_open(ws, state=state):
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

            def on_message(ws, raw, state=state):
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    return
                if not isinstance(payload, dict):
                    return
                if self._handle_control_message(ws, payload, state["generation"]):
                    return
                if payload.get("op") in ("subscribe", "unsubscribe") and payload.get("success") is False:
                    self.disconnect(generation=state["generation"])
                    ws.close()
                    return
                self.ingest(payload, generation=state["generation"])

            def on_close(ws, code, reason, state=state):
                self.disconnect(generation=state["generation"])

            ws = websocket_app_factory(self.ENDPOINT, on_open=on_open,
                                       on_message=on_message, on_close=on_close)
            try:
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                logging.warning("Bybit public quote WS reconnect: %s", type(exc).__name__)
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


class MaintainedQuoteStream(BybitQuoteStream):
    """Opt-in stable primary only; the reserve retains explicit-mark rules."""

    def __init__(self, *, on_update=None, **kwargs):
        from .market_clock import book_age_bounds, latest_now
        from .ws_maintained_marks import MaintainedMarks
        super().__init__(on_update=None, **kwargs)
        self._notify = on_update
        self._maintained = MaintainedMarks(age_bounds=book_age_bounds,
            monotonic=self._mono, utc_upper=latest_now)

    def _notify_symbols(self, symbols):
        if self._notify is not None:
            for symbol in symbols:
                try:
                    self._notify("tickers", symbol)
                except Exception:
                    pass

    def connect(self, socket=None):
        with self._lock:
            generation = super().connect(socket)
            self._maintained.reset(self._connection_fence, self._subscriptions)
            return generation

    def disconnect(self, *, generation=None):
        with self._lock:
            if generation is not None and generation != self._generation:
                return False
            affected = tuple(self._subscriptions) if self._connected else ()
            self._maintained.reset(None)
            result = super().disconnect(generation=generation)
        self._notify_symbols(affected)
        return result

    def subscribe_many(self, symbols):
        with self._lock:
            super().subscribe_many(symbols)
            self._maintained.sync(self._subscriptions)

    def ingest(self, payload, *, generation):
        with self._lock:
            if not self._connected or generation != self._generation:
                return False
            changed = self._maintained.ingest(payload, self._connection_fence)
            raw = super().ingest(payload, generation=generation)
        if changed or raw:
            self._notify_symbols((payload["topic"][8:],))
        return changed or raw

    def maintained_mark(self, symbol):
        with self._lock:
            return self._maintained.mark(symbol)

    def maintained_health(self):
        with self._lock:
            return self._maintained.stats()
