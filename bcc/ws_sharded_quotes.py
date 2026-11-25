"""Bounded persistent Bybit ticker shards, with independent redundant chains.

All active ticker demand is subscribed on both lanes. A quiet topic remains
unavailable after its original freshness deadline. Only sustained missing
collateral on BOTH lanes can trigger a bounded secondary snapshot request.
"""
from __future__ import annotations

import threading
import time
import zlib

from .ws_dual_quotes import DualBybitQuoteStream
from .ws_quotes import StreamUnavailable

SHARDS = 4
RECOVERY_TOPICS_PER_SHARD = 4
MISSING_GRACE = 2.0
RETRY_INTERVAL = 12.0


class StablePair(DualBybitQuoteStream):
    def __init__(self, **kwargs):
        super().__init__(maintained=True, maintained_recovery=True, **kwargs)
        self._missing_since = {}
        self._retry_at = {}
        self._recovery_attempts = 0

    def sync_symbols(self, symbols):
        selected = self._validate(symbols)
        with self._lock:
            with self._recovery._lock:
                pending = set(self._recovery._wire_symbols)
            # Do not re-admit a removed topic's unfinished wire operation.
            self._recovery_demand = ((self._recovery_demand & selected)
                                     | (selected - pending))
            self._missing_since = {s:t for s,t in self._missing_since.items() if s in selected}
            self._retry_at = {s:t for s,t in self._retry_at.items() if s in selected}
            removed = super().sync_symbols(sorted(selected))
            self.set_priority_symbols(sorted(self._priority))
            return removed

    def subscribe_many(self, symbols):
        selected = self._validate(symbols)
        with self._lock:
            if not selected <= self._demand:
                self.sync_symbols(sorted(self._demand | selected))

    def refresh_stale_marks(self, symbols):
        selected = self._validate(symbols)
        with self._lock:
            now = self._mono()
            eligible = []
            with self._recovery._lock:
                pending = set(self._recovery._wire_symbols)
            for symbol in sorted(selected & self._demand):
                mark, _ = self.independent_observations_for_hub(symbol)
                if mark is not None:
                    self._missing_since.pop(symbol, None)
                    continue
                since = self._missing_since.setdefault(symbol, now)
                if (symbol in self._recovery_demand and symbol not in pending
                        and now-since >= MISSING_GRACE
                        and now >= self._retry_at.get(symbol, 0.)):
                    eligible.append(symbol)
            eligible.sort(key=lambda s:(s not in self._priority, self._missing_since[s], s))
            selected = eligible[:RECOVERY_TOPICS_PER_SHARD]
            # A retry interval is a command budget, not a price validity TTL.
            # Charge selection even on wire failure; avoid retry storms.
            for symbol in selected:
                self._retry_at[symbol] = now + RETRY_INTERVAL
            self._recovery_attempts += len(selected)
            self._deferred = len(eligible)-len(selected)
            # Also check outstanding control deadlines when no new work is due.
            return self._recovery.refresh_stale_marks(selected,
                        max_topics=RECOVERY_TOPICS_PER_SHARD)

    def transport_health(self):
        with self._lock:
            result = super().transport_health()
            primary = reserve = both = 0
            for s in self._demand:
                a = self._primary.maintained_mark(s) is not None
                b = s in self._recovery_demand and self._recovery.maintained_mark(s) is not None
                primary += int(a)
                reserve += int(b)
                both += int(a and b)
            result['redundancy'] = dict(primary_fresh=primary, reserve_fresh=reserve,
                both_fresh=both, neither=len(self._demand)-primary-reserve+both,
                missing_tracked=len(self._missing_since), recovery_selections=self._recovery_attempts)
            return result


def _sum_counts(rows):
    return {k:sum(r.get(k,0) for r in rows if type(r.get(k,0)) is int)
            for k in {k for r in rows for k,v in r.items() if type(v) is int}}


class ShardedBybitQuoteStream:
    """Stable symbol hashing; exactly four pairs, not a cap on symbols."""
    maintained_enabled = True
    maintained_recovery_enabled = True

    def __init__(self, *, wall=time.time, monotonic=time.monotonic, on_update=None,
                 pair_factory=StablePair):
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._demand = set()
        self._priority = set()
        self._pairs = tuple(pair_factory(wall=wall, monotonic=monotonic,
                            on_update=on_update) for _ in range(SHARDS))

    @staticmethod
    def _index(symbol):
        return zlib.crc32(symbol.encode('ascii')) % SHARDS

    def _groups(self, symbols):
        groups = [[] for _ in self._pairs]
        for symbol in sorted(symbols):
            groups[self._index(symbol)].append(symbol)
        return groups

    def sync_symbols(self, symbols):
        selected = DualBybitQuoteStream._validate(symbols)
        with self._lock:
            removed = len(self._demand-selected)
            self._demand = selected
            for pair, names in zip(self._pairs,self._groups(selected)):
                try:
                    pair.sync_symbols(names)
                except StreamUnavailable:
                    # The affected socket owns its reconnect; do not lose
                    # subscriptions or maintenance on the other shards.
                    continue
            return removed

    def subscribe_many(self, symbols):
        selected = DualBybitQuoteStream._validate(symbols)
        with self._lock:
            if not selected <= self._demand:
                self.sync_symbols(sorted(self._demand|selected))

    def subscribe(self, symbol):
        self.subscribe_many([symbol])

    def set_priority_symbols(self, symbols):
        selected = DualBybitQuoteStream._validate(symbols)
        with self._lock:
            self._priority = selected
            for pair,names in zip(self._pairs,self._groups(selected)):
                pair.set_priority_symbols(names)

    def independent_observations_for_hub(self, symbol):
        with self._lock:
            if symbol not in self._demand:
                return None,None
            return self._pairs[self._index(symbol)].independent_observations_for_hub(symbol)

    def snapshot_for_hub(self, symbol):
        with self._lock:
            if symbol not in self._demand:
                return None,None,None
            return self._pairs[self._index(symbol)].snapshot_for_hub(symbol)

    def mark(self, symbol):
        mark,_ = self.independent_observations_for_hub(symbol)
        if mark is None:
            raise StreamUnavailable('Sharded Bybit mark unavailable')
        return mark[:2]

    def quote(self, symbol):
        _,quote = self.independent_observations_for_hub(symbol)
        if quote is None:
            raise StreamUnavailable('Sharded Bybit quote unavailable')
        return quote[:3]

    def refresh_stale_marks(self, symbols):
        selected = DualBybitQuoteStream._validate(symbols)
        with self._lock:
            count = 0
            for pair,names in zip(self._pairs,self._groups(selected & self._demand)):
                try:
                    count += pair.refresh_stale_marks(names)
                except StreamUnavailable:
                    continue
            return count

    def refresh_pressure(self):
        with self._lock:
            rows = [p.refresh_pressure() for p in self._pairs]
            return dict(per_scan_budget=SHARDS*RECOVERY_TOPICS_PER_SHARD,
                deferred_symbols=sum(r['deferred_symbols'] for r in rows),
                shadow_new_topics=sum(r['shadow_new_topics'] for r in rows),
                primary=_sum_counts([r['primary'] for r in rows]),
                recovery=_sum_counts([r['recovery'] for r in rows]))

    def transport_health(self):
        with self._lock:
            rows = [p.transport_health() for p in self._pairs]
            result = dict(mode='persistent_sharded_redundancy', shard_count=SHARDS,
                mark_union=_sum_counts([r['mark_union'] for r in rows]),
                maintained=_sum_counts([r['maintained'] for r in rows]),
                maintained_recovery=_sum_counts([r['maintained_recovery'] for r in rows]),
                redundancy=_sum_counts([r['redundancy'] for r in rows]),
                refresh=self.refresh_pressure(), shards=[dict(index=i,
                    union=r['mark_union'], redundancy=r['redundancy'],
                    primary=r['primary'], recovery=r['recovery']) for i,r in enumerate(rows)])
            for lane in ('primary','recovery'):
                stats = [r[lane] for r in rows]
                result[lane] = dict(_sum_counts(stats),
                    connection='connected' if all(r['connection']=='connected' for r in stats) else 'partial',
                    explicit_mark_max_age_seconds=max(r['explicit_mark_max_age_seconds'] for r in stats))
            return result

    def run(self, *, websocket_app_factory=None):
        if self._stop.is_set():
            return
        errors = []
        def worker(pair):
            try:
                pair.run(websocket_app_factory=websocket_app_factory)
            except BaseException as exc:
                errors.append(exc)
            finally:
                self._stop.set()
        threads = [threading.Thread(target=worker,args=(p,),daemon=True,
                                    name=f'bybit-ticker-shard-{i}') for i,p in enumerate(self._pairs)]
        try:
            for t in threads:
                t.start()
            self._stop.wait()
        finally:
            self.stop()
            for t in threads:
                if t.ident is not None:
                    t.join(timeout=12)
        if any(t.is_alive() for t in threads):
            raise RuntimeError('Bybit ticker shard failed to stop')
        if errors:
            raise RuntimeError('Bybit ticker shard failed') from errors[0]

    def stop(self):
        self._stop.set()
        for pair in self._pairs:
            pair.stop()
