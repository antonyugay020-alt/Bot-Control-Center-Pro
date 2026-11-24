"""Opt-in Bybit ticker recovery isolated from the authoritative primary socket.

Each child retains its own exchange timestamps and connection generation. Never
combine fields across children. No REST fallback, orders or age relaxation.
"""
from __future__ import annotations

import threading
import time
from .ws_dispatch import MAX_REFRESH_TOPICS_PER_SCAN
from .ws_quotes import BybitQuoteStream, StreamUnavailable
from .ws_recovery_quotes import RecoveryQuoteStream
from .ws_symbols import USDT_SYMBOL


class DualBybitQuoteStream:
    """Two persistent sockets; only the secondary may request refresh snapshots."""

    def __init__(self, *, wall=time.time, monotonic=time.monotonic, on_update=None):
        self._wall = wall
        self._mono = monotonic
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._primary = BybitQuoteStream(wall=wall, monotonic=monotonic, on_update=on_update)
        self._recovery = RecoveryQuoteStream(wall=wall, monotonic=monotonic, on_update=on_update, mark_only_recovery=True)
        self._demand = set()
        self._primary_added = {}
        self._recovery_demand = set()
        self._priority = frozenset()
        self._deferred = 0
        self._new_topics = 0

    @staticmethod
    def _validate(symbols):
        if not isinstance(symbols, (list, tuple, set)) or any(
                type(s) is not str or not USDT_SYMBOL.fullmatch(s) for s in symbols):
            raise ValueError('invalid linear USDT symbols')
        return set(symbols)

    def subscribe(self, symbol):
        self.subscribe_many([symbol])

    def subscribe_many(self, symbols):
        symbols = self._validate(symbols)
        with self._lock:
            for symbol in symbols - self._demand:
                self._primary_added[symbol] = self._mono()
            self._demand.update(symbols)
            self._primary.subscribe_many(sorted(symbols))

    def sync_symbols(self, symbols):
        selected = self._validate(symbols)
        with self._lock:
            removed = len(self._demand - selected)
            now = self._mono()
            self._primary_added = {s:self._primary_added.get(s, now) for s in selected}
            self._demand = selected
            self._recovery_demand.intersection_update(selected)
            # Remove inactive recovery demand even if the primary send fails.
            # Each child independently revokes its own failed socket.
            try:
                self._recovery.sync_symbols(sorted(self._recovery_demand))
            except StreamUnavailable:
                pass
            self._primary.sync_symbols(sorted(selected))
            return removed

    def set_priority_symbols(self, symbols):
        selected = self._validate(symbols)
        with self._lock:
            self._priority = frozenset(selected)
            self._primary.set_priority_symbols(sorted(selected & self._demand))
            self._recovery.set_priority_symbols(sorted(selected & self._recovery_demand))

    def snapshot_for_hub(self, symbol):
        """Choose ONE complete source incarnation, never merge children."""
        with self._lock:
            if symbol not in self._demand:
                return None, None, None
            candidates = [s.snapshot_for_hub(symbol) for s in (self._primary, self._recovery)]
            valid = [c for c in candidates if c[2] and (c[0] is not None or c[1] is not None)]
            if not valid:
                return None, None, None
            # Prefer genuine mark evidence, then its original exchange time.
            # Quote-only deltas cannot erase a fresh mark from the other lane.
            return max(valid, key=lambda c: (c[0] is not None,
                c[0][1] if c[0] is not None else -1.,
                c[1][2] if c[1] is not None else -1.))

    def independent_observations_for_hub(self, symbol):
        """Read independent live fields, retaining the accepted mark's 3s bound.

        The existing consumer mark contract is three seconds; a quote stays
        subject to its own two-second source limit. No cached value from a
        retired generation is carried into a new publication.
        """
        from .ws_split_ticker import remaining
        with self._lock:
            if symbol not in self._demand:
                return None, None
            marks, quotes = [], []
            for child in (self._primary, self._recovery):
                with child._lock:
                    if not child._connected or symbol not in child._subscriptions:
                        continue
                    fence = child._connection_fence
                    row = child._marks.get(symbol)
                    if (row is not None and row[3] == child._generation
                            and 0 <= child._mono() - row[2] < 3.
                            and remaining(row[1], 3.) > 0):
                        marks.append((row[0], row[1] / 1000, fence))
                    try:
                        bid, ask, at = child.quote(symbol)
                    except StreamUnavailable:
                        continue
                    if remaining(round(at * 1000), 2.) > 0:
                        quotes.append((bid, ask, at, fence))
            return (max(marks, key=lambda m: m[1], default=None),
                    max(quotes, key=lambda q: q[2], default=None))

    def mark(self, symbol):
        mark, _, _ = self.snapshot_for_hub(symbol)
        if mark is None:
            raise StreamUnavailable('Bybit dual WS mark unavailable')
        return mark

    def quote(self, symbol):
        with self._lock:
            if symbol not in self._demand:
                raise StreamUnavailable('Bybit dual WS quote not subscribed')
            quotes = [child.snapshot_for_hub(symbol)[1] for child in (self._primary, self._recovery)]
            valid = [q for q in quotes if q is not None]
            if not valid:
                raise StreamUnavailable('Bybit dual WS quote unavailable')
            return max(valid, key=lambda q:q[2])

    def refresh_stale_marks(self, symbols):
        """Keep primary subscriptions stable; add and refresh only recovery demand.

        A per-scan topic budget limits combined new shadow subscriptions and
        refresh requests, not total demand. Previously selected recovery topics
        persist until the registry removes them. New shadow demand is lazy.
        """
        symbols = self._validate(symbols)
        with self._lock:
            wanted = []
            wall, now = self._wall(), self._mono()
            # Compare explicit marks from both lanes. Executable quote age
            # is not a reason to recover the independently timed collateral.
            for symbol in symbols & self._demand:
                primary_mark, _, fence = self._primary.snapshot_for_hub(symbol)
                marks = [primary_mark]
                if symbol in self._recovery_demand:
                    marks.append(self._recovery.snapshot_for_hub(symbol)[0])
                stamps = [m[1] for m in marks if m is not None]
                newest = max(stamps, default=-1.)
                if fence and not stamps and now-self._primary_added[symbol] < .75:
                    continue
                lead = 1.0 if symbol in self._priority else 1.5
                if not stamps or wall-newest >= lead:
                    wanted.append((symbol, newest))
            wanted = [s for s, _ in sorted(wanted,
                key=lambda item: (item[0] not in self._priority, item[1], item[0]))]
            new = [s for s in wanted if s not in self._recovery_demand]
            old = [s for s in wanted if s in self._recovery_demand]
            # Existing recovery must get a turn even under a burst of new demand.
            new_budget = MAX_REFRESH_TOPICS_PER_SCAN // 2 if old else MAX_REFRESH_TOPICS_PER_SCAN
            new_priority = [s for s in new if s in self._priority]
            new_background = [s for s in new if s not in self._priority][:20]
            add = (new_priority + new_background)[:new_budget]
            if add:
                self._recovery_demand.update(add)
                self._recovery.set_priority_symbols(sorted(self._priority & self._recovery_demand))
                self._recovery.subscribe_many(add)
                self._new_topics += len(add)
            budget = MAX_REFRESH_TOPICS_PER_SCAN - len(add)
            # Child scheduling considers ALL old demand and skips already
            # healthy/pending rows before charging the remaining topic budget.
            refreshed = self._recovery.refresh_stale_marks(old, max_topics=budget) if budget else 0
            debt = self._recovery.refresh_pressure()['deferred_symbols'] if budget else len(old)
            self._deferred = max(0,len(new)-len(add)) + debt
            return len(add) + refreshed

    def refresh_pressure(self):
        with self._lock:
            return dict(deferred_symbols=self._deferred,
                        per_scan_budget=MAX_REFRESH_TOPICS_PER_SCAN,
                        shadow_new_topics=self._new_topics,
                        primary=self._primary.refresh_pressure(),
                        recovery=self._recovery.refresh_pressure())

    def transport_health(self):
        with self._lock:
            def lane(child):
                with child._lock:
                    now = self._wall()
                    ages = [now - row[1] / 1000 for row in child._marks.values()
                            if child._connected and row[3] == child._generation]
                    return dict(connection='connected' if child._connected else 'reconnecting',
                                subscribed=len(child._subscriptions), received_symbols=len(child._quotes),
                                explicit_mark_symbols=len(ages),
                                explicit_mark_age_over_3s=sum(age > 3. for age in ages),
                                explicit_mark_max_age_seconds=round(max(ages, default=0.), 6))
            return dict(mode='stable_primary_independent_recovery',
                        primary=lane(self._primary), recovery=lane(self._recovery),
                        refresh=self.refresh_pressure())

    def run(self, *, websocket_app_factory=None):
        """Own both child lifecycles. Failure of either loop ends this candidate."""
        errors = []
        def worker(child):
            try:
                child.run(websocket_app_factory=websocket_app_factory, reset_stop=False)
            except BaseException as exc:
                errors.append(exc)
            finally:
                self._stop.set()
        threads = [threading.Thread(target=worker, args=(child,), daemon=True,
                                    name='bybit-ticker-'+name)
                   for child,name in ((self._primary,'primary'),(self._recovery,'recovery'))]
        if self._stop.is_set():
            return
        try:
            for t in threads:
                t.start()
            self._stop.wait()
        finally:
            self.stop()
            for t in threads:
                if t.ident is not None:
                    t.join(timeout=5)
        if any(t.is_alive() for t in threads):
            raise RuntimeError('Bybit dual WS child failed to stop')
        if errors:
            raise RuntimeError('Bybit dual WS child failed') from errors[0]

    def stop(self):
        self._stop.set()
        self._primary.stop()
        self._recovery.stop()
