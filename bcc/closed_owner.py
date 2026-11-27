"""Bounded evidence of finalized history actually consumed by the account owner.

A receipt is observational only. It cannot authorize an order, refresh a price,
clear diagnostics or replace a missing/denied candle.
"""
import math

MINUTE = 60000
WINDOWS = 4


class ClosedOwnerReceipts:
    def __init__(self):
        self._rows = {}
        self._boundary = None

    def advance(self, boundary):
        if type(boundary) is not int or boundary <= 0 or boundary % MINUTE:
            return
        if self._boundary is not None and boundary < self._boundary:
            self._rows.clear()
        self._boundary = boundary
        for old in tuple(self._rows):
            if old < boundary - (WINDOWS - 1) * MINUTE or old > boundary:
                del self._rows[old]

    def acknowledged(self, symbol, boundary):
        row = self._rows.get(boundary, {}).get(symbol)
        return row is not None and row['commit_upper_ms'] < boundary + MINUTE

    def record(self, tick, bounds, required):
        """Called only AFTER successful account transaction and strategy evaluation."""
        if (not getattr(tick, 'closed_history', False) or not tick.live_history
                or tick.risk_only or tick.history_denied or tick.history_error
                or tick.book is None or len(tick.bars) < required):
            return False
        stamp = tick.bars[-1].get('t')
        decision = getattr(tick, 'closed_decision_at', None)
        low, high = bounds
        if (type(stamp) is not int or stamp < 0 or stamp % MINUTE
                or any(type(v) not in (int, float) or not math.isfinite(v)
                       for v in (decision, low, high)) or not decision <= low <= high):
            return False
        boundary = stamp + MINUTE
        decision_ms = math.floor(decision * 1000)
        commit_upper_ms = math.ceil(high * 1000)
        if not boundary <= decision_ms < boundary + MINUTE:
            return False
        self.advance(math.floor(low / 60) * MINUTE)
        if not self._boundary - (WINDOWS - 1) * MINUTE <= boundary <= self._boundary:
            return False
        rows = self._rows.setdefault(boundary, {})
        if tick.symbol in rows:
            return False
        rows[tick.symbol] = dict(decision_ms=decision_ms, commit_upper_ms=commit_upper_ms)
        return True

    def snapshot(self):
        return dict(boundary=self._boundary,
                    receipts={str(b): {s: dict(row) for s, row in rows.items()}
                              for b, rows in self._rows.items()})


def pending_ready(*, symbols, eligible, ready, boundary, sizes, histories,
                  diagnostics, admissions, receipts=None):
    """Rank real current history: existing errors first, then unconsumed minutes.

    No priority for a denied/short/old window. Preserve FIFO-like rotation
    within each class, including symbols that have no open positions.
    """
    candidates = []
    for symbol in symbols:
        bars = ready.get(symbol)
        history = histories.get(symbol)
        if (symbol not in eligible or not bars or bars[-1]['t'] != boundary - MINUTE
                or len(bars) < sizes.get(symbol, 100)
                or (history is not None and history.history_denied)):
            continue
        failed = any(isinstance(row, dict) and row.get('section') == 'Свечи'
                     and row.get('reason') for row in diagnostics.get(symbol, []))
        if failed or (receipts is not None and not receipts.acknowledged(symbol, boundary)):
            candidates.append((not failed, admissions.get(symbol, 0), symbol))
    # Stable inventory order for equal ranks (not symbol lexical order).
    candidates.sort(key=lambda row: row[:2])
    return [row[2] for row in candidates]
