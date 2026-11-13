"""Bounded fair event handoff for the shared market-data WebSocket producer.

Receive callbacks never touch SQLite or wait on it. Critical CLOB and Bybit
executable prices are prioritized, but low-priority Binance spot/trades and
minute candles are guaranteed a scheduling turn after eight urgent events.
Duplicate notifications collapse to one latest-stream read per asset.
"""
from __future__ import annotations

import collections
import threading
import time

# CLOB ASK/BID and executable depth must not queue behind ticker floods.
URGENT = frozenset({"polybook", "depth50"})
REGULAR = frozenset({"tickers", "binance_trade", "kline1"})


class ReconnectBackoff:
    """Shared capped retry policy, independent of provider or credentials."""

    def __init__(self, base=1.0, maximum=30.0, healthy_after=30.0):
        if (type(base) not in (int, float) or not 0.1 <= base <= maximum
                or type(maximum) not in (int, float) or not base <= maximum <= 120
                or type(healthy_after) not in (int, float)
                or not 5 <= healthy_after <= 600):
            raise ValueError("invalid reconnect policy")
        self.base = float(base)
        self.maximum = float(maximum)
        self.healthy_after = float(healthy_after)
        self._next = self.base

    def after_session(self, uptime):
        if type(uptime) not in (int, float) or uptime < 0:
            raise ValueError("invalid session uptime")
        if uptime >= self.healthy_after:
            self._next = self.base
        result = self._next
        self._next = min(self.maximum, max(self.base, self._next * 2))
        return result


class MarketEventQueue:
    def __init__(self, *, capacity=8192, urgent_burst=8):
        if (type(capacity) is not int or not 16 <= capacity <= 100000
                or type(urgent_burst) is not int or not 1 <= urgent_burst <= 64):
            raise ValueError("invalid market-event queue limits")
        self.capacity = capacity
        self.urgent_burst = urgent_burst
        self._cv = threading.Condition()
        self._urgent = collections.deque()
        self._regular = collections.deque()
        self._pending = set()
        self._urgent_streak = 0
        self._dropped = 0

    def notify(self, kind, symbol):
        if (type(kind) is not str or kind not in URGENT | REGULAR
                or type(symbol) is not str or not symbol):
            return False
        item = (kind, symbol)
        with self._cv:
            if item in self._pending:
                return True
            if len(self._pending) >= self.capacity:
                # Fail closed on dropped notifications: old cache rows still
                # have short TTL and cannot be used indefinitely.
                self._dropped += 1
                return False
            self._pending.add(item)
            (self._urgent if kind in URGENT else self._regular).append(item)
            self._cv.notify()
            return True

    def take(self, *, timeout=.1):
        if (type(timeout) not in (int, float) or not 0 <= timeout <= 5):
            raise ValueError("invalid market queue timeout")
        with self._cv:
            if not self._pending:
                self._cv.wait(timeout=timeout)
            if not self._pending:
                return None
            if self._urgent and (
                    self._urgent_streak < self.urgent_burst
                    or not self._regular):
                item = self._urgent.popleft()
                self._urgent_streak += 1
            else:
                item = self._regular.popleft()
                self._urgent_streak = 0
            self._pending.discard(item)
            return item

    def stats(self):
        with self._cv:
            return {
                "pending": len(self._pending),
                "urgent": len(self._urgent),
                "regular": len(self._regular),
                "dropped_notifications": self._dropped,
            }
