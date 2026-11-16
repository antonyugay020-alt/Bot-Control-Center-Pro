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


def request_snapshots(socket, topic, symbols, *, batch_size=10):
    """Ask the established public socket for new exchange snapshots in batches."""
    import json
    for index in range(0, len(symbols), batch_size):
        topics = [topic + "." + symbol for symbol in symbols[index:index + batch_size]]
        socket.send(json.dumps({"op": "unsubscribe", "args": topics}))
        socket.send(json.dumps({"op": "subscribe", "args": topics}))


def synchronize_bybit_symbols(stream, symbols, *, subscribed, topic, caches, subscribe, error_type):
    """Converge producer demand; removed topics cannot retain cached evidence."""
    import json
    from .ws_symbols import USDT_SYMBOL
    if (not isinstance(symbols, (list, tuple, set)) or any(
            type(symbol) is not str or not USDT_SYMBOL.fullmatch(symbol) for symbol in symbols)):
        raise ValueError("invalid stream symbols")
    with stream._lock:
        selected = set(symbols)
        removed = sorted(subscribed - selected)
        subscribed.difference_update(removed)
        for symbol in removed:
            for cache in caches:
                getattr(stream, cache).pop(symbol, None)
        socket = stream._socket if stream._connected else None
        if socket is not None:
            try:
                for index in range(0, len(removed), stream.MAX_SUB_PER_MSG):
                    socket.send(json.dumps({"op": "unsubscribe", "args": [
                        topic + "." + symbol for symbol in removed[index:index + stream.MAX_SUB_PER_MSG]
                    ]}))
            except Exception as exc:
                stream.disconnect()
                try:
                    socket.close()
                finally:
                    raise error_type("Bybit demand unsubscribe failed") from exc
        subscribe(sorted(selected))
        return len(removed)


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
        self._critical = collections.deque()
        self._active = None
        self._priority = frozenset()
        self._critical_streak = 0
        self._enqueued = {}
        self._filtered = 0
        self._evicted = 0
        self._max_wait = 0.
        self._max_critical_wait = 0.
        self._urgent_streak = 0
        self._dropped = 0

    def configure(self, active, priority=()):
        """Apply validated live demand; risk priority cannot come from a frame."""
        active, priority = frozenset(active), frozenset(priority)
        if (not priority <= active or any(
                type(item) is not tuple or len(item) != 2
                or item[0] not in URGENT | REGULAR or type(item[1]) is not str
                or not item[1] for item in active)):
            raise ValueError("invalid stream demand")
        with self._cv:
            self._active, self._priority = active, priority
            retained = self._pending & active
            self._filtered += len(self._pending - retained)
            ordered = sorted(retained, key=self._enqueued.__getitem__)
            self._pending = retained
            self._enqueued = {item: self._enqueued[item] for item in retained}
            self._critical = collections.deque(item for item in ordered if item in priority)
            self._urgent = collections.deque(item for item in ordered if item not in priority and item[0] in URGENT)
            self._regular = collections.deque(item for item in ordered if item not in priority and item[0] in REGULAR)
            self._cv.notify_all()

    def notify(self, kind, symbol):
        if (type(kind) is not str or kind not in URGENT | REGULAR
                or type(symbol) is not str or not symbol):
            return False
        item = (kind, symbol)
        with self._cv:
            if self._active is not None and item not in self._active:
                self._filtered += 1
                return False
            if item in self._pending:
                return True
            if len(self._pending) >= self.capacity:
                background = self._regular if self._regular else self._urgent
                if item in self._priority and background:
                    removed = background.pop()
                    self._pending.discard(removed)
                    self._enqueued.pop(removed, None)
                    self._evicted += 1
                else:
                    self._dropped += 1
                    return False
            self._pending.add(item)
            self._enqueued[item] = time.monotonic()
            target = (self._critical if item in self._priority else
                      self._urgent if kind in URGENT else self._regular)
            target.append(item)
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
            background = bool(self._urgent or self._regular)
            if self._critical and (self._critical_streak < self.urgent_burst or not background):
                item = self._critical.popleft()
                self._critical_streak += 1
            else:
                self._critical_streak = 0
                if self._urgent and (self._urgent_streak < self.urgent_burst or not self._regular):
                    item = self._urgent.popleft()
                    self._urgent_streak += 1
                else:
                    item = self._regular.popleft()
                    self._urgent_streak = 0
            self._pending.discard(item)
            wait = time.monotonic() - self._enqueued.pop(item)
            self._max_wait = max(self._max_wait, wait)
            if item in self._priority:
                self._max_critical_wait = max(self._max_critical_wait, wait)
            return item

    def take_batch(self, *, limit=64, timeout=.1):
        """Drain available events fairly without waiting to fill a batch."""
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError("invalid market batch limit")
        first = self.take(timeout=timeout)
        if first is None:
            return []
        result = [first]
        for _ in range(limit - 1):
            item = self.take(timeout=0)
            if item is None:
                break
            result.append(item)
        return result

    def stats(self):
        with self._cv:
            fronts = [queue[0] for queue in (self._critical, self._urgent, self._regular) if queue]
            oldest = min((self._enqueued[item] for item in fronts), default=time.monotonic())
            return {
                "active_keys": len(self._active) if self._active is not None else None,
                "priority_keys": len(self._priority),
                "pending": len(self._pending), "critical": len(self._critical),
                "urgent": len(self._urgent), "regular": len(self._regular),
                "dropped_notifications": self._dropped,
                "filtered_inactive": self._filtered, "evicted_background": self._evicted,
                "oldest_pending_seconds": round(max(0., time.monotonic()-oldest), 3),
                "max_dispatch_wait_seconds": round(self._max_wait, 3),
                "oldest_critical_seconds": round(max(0., time.monotonic()-self._enqueued[self._critical[0]]), 3) if self._critical else 0.,
                "max_critical_dispatch_wait_seconds": round(self._max_critical_wait, 3),
            }
