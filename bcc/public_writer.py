"""Process-local FIFO admission for all writers of one public WAL database.

SQLite remains the cross-process authority. This gate never grants permission
for an HTTP request, a market observation, or an account transaction.
"""
import collections
import os
import sqlite3
import threading
from time import monotonic as _monotonic, sleep as _sleep
import weakref
from pathlib import Path
from contextlib import contextmanager


class WriterGate:
    def __init__(self):
        self._cv = threading.Condition()
        self._owner = None
        self._depth = 0
        self._waiters = collections.deque()
        self._admitted = self._timeouts = 0
        self._max_wait = 0.

    def acquire(self, blocking=True, timeout=-1):
        if not blocking and timeout != -1:
            raise ValueError('timeout requires blocking acquisition')
        if timeout < 0 and timeout != -1:
            raise ValueError('invalid writer timeout')
        ident = threading.get_ident()
        began = _monotonic()
        deadline = None if timeout == -1 else began + timeout
        with self._cv:
            if self._owner == ident:
                self._depth += 1
                return True
            if not blocking:
                if self._owner is not None or self._waiters:
                    self._timeouts += 1
                    return False
            else:
                ticket = object()
                self._waiters.append(ticket)
                try:
                    while self._owner is not None or self._waiters[0] is not ticket:
                        remaining = None if deadline is None else deadline - _monotonic()
                        if remaining is not None and remaining <= 0:
                            self._timeouts += 1
                            return False
                        self._cv.wait(remaining)
                finally:
                    self._waiters.remove(ticket)
                    self._cv.notify_all()
            self._owner = ident
            self._depth = 1
            self._admitted += 1
            self._max_wait = max(self._max_wait, _monotonic() - began)
            return True

    def release(self):
        with self._cv:
            if self._owner != threading.get_ident():
                raise RuntimeError('writer gate is not owned by this thread')
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
                self._cv.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()

    def stats(self):
        with self._cv:
            return dict(admitted=self._admitted, timeouts=self._timeouts,
                        waiting=len(self._waiters), held=self._owner is not None,
                        max_wait_seconds=round(self._max_wait, 6))


_gates = weakref.WeakValueDictionary()
_guard = threading.Lock()


def writer_gate(path):
    key = os.path.normcase(str(Path(path).resolve()))
    with _guard:
        gate = _gates.get(key)
        if gate is None:
            gate = WriterGate()
            _gates[key] = gate
        return gate


@contextmanager
def writer_transaction(gate, connect, *, migration=False):
    """Acquire a real SQLite writer transaction before yielding to the caller.

    A remote-process lock may delay admission. Release the local FIFO ticket
    between bounded SQLite attempts, so unrelated callers can also progress.
    Never retry caller statements, commits, or an already executed request.
    Migration retains its original WAL setup and transaction boundaries.
    """
    if migration:
        if not gate.acquire(timeout=1.):
            raise sqlite3.OperationalError('database is locked (local writer queue)')
        try:
            with connect() as db:
                yield db
        finally:
            gate.release()
        return
    deadline = _monotonic() + 1.
    last = None
    while True:
        remaining = deadline - _monotonic()
        if remaining <= 0:
            raise sqlite3.OperationalError('database is locked (writer admission)') from last
        if gate.acquire(timeout=min(.2, remaining)):
            try:
                with connect() as db:
                    db.execute('PRAGMA busy_timeout=100')
                    try:
                        db.execute('BEGIN IMMEDIATE')
                    except sqlite3.OperationalError as exc:
                        code = getattr(exc, 'sqlite_errorcode', None)
                        if code is None or (code & 0xff) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                            raise
                        last = exc
                    else:
                        yield db
                        return
            finally:
                gate.release()
        _sleep(min(.01, max(0., deadline - _monotonic())))
