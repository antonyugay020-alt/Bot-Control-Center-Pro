"""FIFO admission using permanent reusable slots, never unlinking live paths.

OS byte locks witness liveness; the registration lock serializes publication.
A slot's sequence is stored beyond the locked byte and is meaningful only
while that slot is locked. SQLite remains the final transaction authority.
"""
import errno
import os
import time
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

    def read_sequence(self):
        os.lseek(self.fd, 1, os.SEEK_SET)
        data = os.read(self.fd, 20)
        if len(data) != 20 or not data.isdigit():
            raise RuntimeError('Invalid live writer sequence')
        return int(data)

    def write_sequence(self, seq):
        if not self.held or not 0 < seq < 10**20:
            raise RuntimeError('Invalid writer sequence publication')
        data = f'{seq:020d}'.encode('ascii')
        os.lseek(self.fd, 1, os.SEEK_SET)
        if os.write(self.fd, data) != len(data):
            raise OSError('Incomplete writer sequence publication')

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


class ProcessWriterLock:
    def __init__(self, database):
        self.path = str(Path(database)) + '.writer.lock'
        # Separate from the retired per-request V29 ticket namespace.
        self.queue = Path(self.path + '.slots')
        self.slot = None
        self.ticket_lock = None
        self.sequence = None
        self.writer = None

    def _register(self, deadline, blocking):
        self.queue.mkdir(exist_ok=True)
        mutex = _ByteLock(self.queue / 'register.lock')
        spare = None
        try:
            while not mutex.try_lock():
                if not _pause(deadline, blocking):
                    return False
            highest = 0
            entries = sorted(self.queue.glob('*.slot'))
            for path in entries:
                probe = _ByteLock(path, create=False)
                try:
                    if probe.try_lock():
                        if spare is None:
                            spare, self.slot = probe, path
                            probe = None
                    else:
                        highest = max(highest, probe.read_sequence())
                finally:
                    if probe is not None:
                        probe.close()
            if spare is None:
                # Files only grow to the peak number of concurrent waiters.
                # Slots are never removed, renamed, or truncated.
                index = int(entries[-1].stem) + 1 if entries else 0
                self.slot = self.queue / f'{index:020d}.slot'
                spare = _ByteLock(self.slot)
                if not spare.try_lock():
                    raise RuntimeError('New writer slot unexpectedly locked')
            self.sequence = highest + 1
            spare.write_sequence(self.sequence)
            self.ticket_lock, spare = spare, None
            return True
        finally:
            # On a failed publication, release the slot before the mutex.
            try:
                if spare is not None:
                    spare.close()
            finally:
                mutex.close()

    def _head(self):
        mutex = _ByteLock(self.queue / 'register.lock')
        try:
            if not mutex.try_lock():
                return False
            for path in self.queue.glob('*.slot'):
                if path == self.slot:
                    continue
                probe = _ByteLock(path, create=False)
                try:
                    if not probe.try_lock() and probe.read_sequence() < self.sequence:
                        return False
                finally:
                    probe.close()
            return True
        finally:
            mutex.close()

    def _drop_ticket(self):
        lock, self.ticket_lock = self.ticket_lock, None
        self.slot = self.sequence = None
        if lock is not None:
            lock.close()

    def acquire(self, deadline, blocking):
        if self.ticket_lock is not None or self.writer is not None:
            raise RuntimeError('process writer already held')
        granted = False
        try:
            if not self._register(deadline, blocking):
                return False
            while True:
                if self._head():
                    # Retain the actual V28/V29 writer lock for compatibility.
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
