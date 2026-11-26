"""FIFO tickets for cooperating public WAL writers; SQLite is still authority.

Each published ticket is locked by its live owner. A crashed owner/waiter is
reaped only after the OS grants its ticket lock. Persistent admission locks
are never unlinked. No wall-clock/PID guessing or lease expiry steals a lock.
"""
import errno
import os
import time
import uuid
from pathlib import Path


class _ByteLock:
    def __init__(self, path, *, create=True):
        self.fd = os.open(path, os.O_RDWR | getattr(os, 'O_BINARY', 0)
                          | (os.O_CREAT if create else 0), 0o600)
        self.held = False

    def try_lock(self):
        try:
            if os.name == 'nt':
                import msvcrt
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            return False
        self.held = True
        return True

    def close(self):
        try:
            if self.held:
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.held = False


def _pause(deadline, blocking):
    remaining = None if deadline is None else deadline - time.monotonic()
    if not blocking or (remaining is not None and remaining <= 0):
        return False
    time.sleep(.005 if remaining is None else min(.005, remaining))
    return True


def _unlink_ticket(path):
    try:
        path.unlink()
    except (FileNotFoundError, PermissionError):
        # Windows may have a concurrent reaper's handle open. A later head
        # scan will remove this unlocked ticket. Never remove a live ticket.
        pass


class ProcessWriterLock:
    def __init__(self, database):
        self.path = str(Path(database)) + '.writer.lock'
        self.queue = Path(self.path + '.queue')
        self.ticket = None
        self.ticket_lock = None
        self.writer = None

    def _register(self, deadline, blocking):
        self.queue.mkdir(exist_ok=True)
        mutex = _ByteLock(self.queue / 'register.lock')
        try:
            while not mutex.try_lock():
                if not _pause(deadline, blocking):
                    return False
            # Serialize publication: an observer can never see an unlocked
            # half-created ticket. Sequence is relative to current queue.
            entries = sorted(self.queue.glob('*.ticket'))
            seq = int(entries[-1].name.split('-', 1)[0]) + 1 if entries else 0
            self.ticket = self.queue / f'{seq:020d}-{uuid.uuid4().hex}.ticket'
            self.ticket_lock = _ByteLock(self.ticket)
            if not self.ticket_lock.try_lock():
                raise RuntimeError('new writer ticket unexpectedly locked')
            return True
        finally:
            mutex.close()

    def _head(self):
        # Registration mutex is needed ONLY while publishing/reaping, not
        # while waiting on SQLite or the actual writer. Keep this section short.
        mutex = _ByteLock(self.queue / 'register.lock')
        try:
            if not mutex.try_lock():
                return False
            for path in sorted(self.queue.glob('*.ticket')):
                if path == self.ticket:
                    return True
                try:
                    probe = _ByteLock(path, create=False)
                except FileNotFoundError:
                    continue
                try:
                    dead = probe.try_lock()
                finally:
                    probe.close()
                if not dead:
                    return False
                _unlink_ticket(path)
            return False
        finally:
            mutex.close()

    def _drop_ticket(self):
        lock, path = self.ticket_lock, self.ticket
        self.ticket_lock = self.ticket = None
        try:
            if lock is not None:
                lock.close()
        finally:
            if path is not None:
                _unlink_ticket(path)

    def acquire(self, deadline, blocking):
        if self.ticket is not None or self.writer is not None:
            raise RuntimeError('process writer already held')
        granted = False
        try:
            if not self._register(deadline, blocking):
                return False
            while True:
                if self._head():
                    # Preserve interoperability with the existing byte lock.
                    # Do not admit if a V28/non-queue writer still owns it.
                    candidate = _ByteLock(self.path)
                    try:
                        if candidate.try_lock():
                            self.writer = candidate
                            granted = True
                            return True
                    finally:
                        if not granted:
                            candidate.close()
                if not _pause(deadline, blocking):
                    return False
        finally:
            if not granted:
                self._drop_ticket()

    def release(self):
        if self.writer is None:
            raise RuntimeError('process writer not held')
        writer, self.writer = self.writer, None
        try:
            writer.close()
        finally:
            self._drop_ticket()
