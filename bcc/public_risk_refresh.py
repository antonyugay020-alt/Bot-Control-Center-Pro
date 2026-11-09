"""Bounded periodic risk tier refresh independent of position depth and RSI.

The active position quote lane never waits for these slow public requests.
Trading continues to fail closed if risk tiers actually expire or are missing.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from .portfolio_marks import commit_public_risk_tiers
from .market import DataUnavailable
from .data_cache import CacheBusy


class PublicRiskRefresher:
    def __init__(self, source, store, *, workers=2, refresh_after=180, retry_after=30):
        self.source = source
        self.store = store
        self.workers = workers
        self.refresh_after = refresh_after
        self.retry_after = retry_after
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='public-risk-tiers')
        self.running = {}
        self.retry_at = {}
        self.stats = {'completed': 0, 'errors': 0, 'in_flight': 0}

    def poll(self, state):
        now = time.monotonic()
        # Process all completed futures, including those for positions that
        # closed in the meantime. The writer validates current ownership.
        for symbol, fut in list(self.running.items()):
            if not fut.done():
                continue
            del self.running[symbol]
            try:
                risk = fut.result()
                if commit_public_risk_tiers(self.store, symbol, risk):
                    self.stats['completed'] += 1
                self.retry_at[symbol] = now + self.refresh_after
            except (DataUnavailable, CacheBusy, OSError, ValueError, TypeError, KeyError) as exc:
                self.stats['errors'] += 1
                self.retry_at[symbol] = now + self.retry_after
                logging.warning('PUBLIC_RISK_TIERS %s: %s', symbol, exc)
        active = {p['symbol'] for p in list(state['positions'].values()) +
                  list(state.get('pending', {}).values())
                  if p.get('kind') != 'POLYMARKET'}
        # Prioritize the soonest expirations. No fixed position ceiling.
        def age(symbol):
            data = state.get('margin_data', {}).get(symbol)
            if not isinstance(data, dict):
                return float('inf')
            return max(0.0, time.time() - data.get('risk_at', 0))
        for symbol in sorted(active, key=lambda sym: (-age(sym), sym)):
            if len(self.running) >= self.workers:
                break
            if (age(symbol) < self.refresh_after or symbol in self.running
                    or self.retry_at.get(symbol, 0) > now):
                continue
            self.running[symbol] = self.executor.submit(self.source.risk_tiers, symbol)
        self.stats['in_flight'] = len(self.running)
        self.stats['active'] = len(active)
        self.stats['tiers_near_expiry'] = sum(age(s) >= 240 for s in active)
        return dict(self.stats)

    def close(self):
        for fut in self.running.values():
            fut.cancel()
        self.executor.shutdown(wait=True, cancel_futures=True)
