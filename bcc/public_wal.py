"""Move public WAL checkpoints off the market writer's critical path.

Only the elected central Hub starts maintenance. All writer connections retain
SQLite's normal automatic checkpoint whenever the maintenance lane is absent,
stale, overloaded or failed. Policy and market data remain in the same WAL.
"""
import os
import sqlite3
import threading
import time
from pathlib import Path


DEFAULT_PAGES = 1000
MAX_BACKLOG_PAGES = 16384
_ACTIVE = {}
_LOCK = threading.Lock()


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def configure_writer(db, path):
    """Call outside transactions. Never disable checkpoint without a live lane."""
    with _LOCK:
        manager = _ACTIVE.get(_key(path))
    pages = 0 if manager is not None and manager.healthy() else DEFAULT_PAGES
    db.execute('PRAGMA wal_autocheckpoint=' + str(pages))
    return pages


class WalMaintenance:
    def __init__(self, path, *, period=.25):
        self.path = Path(path).resolve()
        self.period = period
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._guard = threading.Lock()
        self._thread = None
        self._last = None
        self._backlog = 0
        self._stats = dict(checkpoints=0, busy=0, errors=0, last_log_pages=0,
                           last_checkpointed_pages=0, max_backlog_pages=0,
                           max_checkpoint_seconds=0., fallback_checks=0)

    def healthy(self):
        with self._guard:
            return (not self._stop.is_set() and self._last is not None
                    and time.monotonic()-self._last <= 2.
                    and self._backlog <= MAX_BACKLOG_PAGES)

    def stats(self):
        with self._guard:
            result = dict(self._stats)
        result['healthy'] = self.healthy()
        return result

    def start(self):
        with _LOCK:
            if self._thread is not None or _key(self.path) in _ACTIVE:
                raise RuntimeError('Public WAL maintenance already started')
            _ACTIVE[_key(self.path)] = self
            self._thread = threading.Thread(target=self._run, name='hub-wal-checkpoint', daemon=True)
            self._thread.start()
        if not self._ready.wait(3.) or not self.healthy():
            self.close()
            raise RuntimeError('Public WAL maintenance did not initialize')

    def _run(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=0., isolation_level=None)
            db.execute('PRAGMA busy_timeout=0')
            # NORMAL matches the existing public-cache writer. Registry
            # connections retain their original synchronization policy.
            db.execute('PRAGMA synchronous=NORMAL')
            while not self._stop.is_set():
                began = time.monotonic()
                try:
                    busy, pages, done = db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
                    if busy and (pages < 0 or done < 0):
                        # Another checkpoint can own the checkpoint lock.
                        # No usable frame counts: restore automatic handling
                        # until a later successful passive observation.
                        with self._guard:
                            self._last = None
                            self._stats['busy'] += 1
                    else:
                        if pages < 0 or done < 0:
                            raise RuntimeError('Public database is not in WAL mode')
                        with self._guard:
                            self._last = time.monotonic()
                            self._backlog = max(0, pages-done)
                            s = self._stats
                            s['checkpoints'] += 1
                            s['busy'] += int(bool(busy))
                            s['last_log_pages'], s['last_checkpointed_pages'] = pages, done
                            s['max_backlog_pages'] = max(s['max_backlog_pages'], self._backlog)
                            s['max_checkpoint_seconds'] = max(s['max_checkpoint_seconds'], self._last-began)
                            s['fallback_checks'] += int(self._backlog > MAX_BACKLOG_PAGES)
                        self._ready.set()
                except sqlite3.Error:
                    # An I/O or lock error cannot silently leave auto-checkpoint
                    # disabled. Retry on the next maintenance interval.
                    with self._guard:
                        self._last = None
                        self._stats['errors'] += 1
                self._stop.wait(self.period)
        except Exception:
            with self._guard:
                self._last = None
                self._stats['errors'] += 1
            self._ready.set()
        finally:
            if db is not None:
                db.close()

    def close(self):
        self._stop.set()
        with _LOCK:
            if _ACTIVE.get(_key(self.path)) is self:
                del _ACTIVE[_key(self.path)]
        if self._thread is not None:
            self._thread.join(5.)
            if self._thread.is_alive():
                raise RuntimeError('Public WAL maintenance did not stop')


class WriterTiming:
    """Bounded counters; no payloads or unbounded per-request history."""
    def __init__(self):
        self._guard = threading.Lock()
        self._data = {}

    def record(self, lane, wait, held):
        with self._guard:
            row = self._data.setdefault(lane, dict(count=0, max_wait_seconds=0.,
                                                  max_hold_seconds=0., holds_over_200ms=0))
            row['count'] += 1
            row['max_wait_seconds'] = max(row['max_wait_seconds'], wait)
            row['max_hold_seconds'] = max(row['max_hold_seconds'], held)
            row['holds_over_200ms'] += int(held > .2)

    def stats(self):
        with self._guard:
            return {key: dict(value) for key, value in self._data.items()}
