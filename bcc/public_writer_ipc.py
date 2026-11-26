"""Cooperative interprocess admission for a single public SQLite writer.

The OS releases the byte lock when its handle/process exits. Never unlink the
sidecar: two different inodes could then authorize competing writers. SQLite
still fences actual transactions, including clients without this protocol.
"""
import errno
import os
import time
from pathlib import Path


class ProcessWriterLock:
    def __init__(self, database):
        self.path = str(Path(database)) + '.writer.lock'
        self.fd = None

    def acquire(self, deadline, blocking):
        if self.fd is not None:
            raise RuntimeError('process writer already held')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_BINARY', 0), 0o600)
        try:
            while True:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.fd = fd
                    return True
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                remaining = None if deadline is None else deadline - time.monotonic()
                if not blocking or (remaining is not None and remaining <= 0):
                    return False
                time.sleep(.005 if remaining is None else min(.005, remaining))
        finally:
            if self.fd is None:
                os.close(fd)

    def release(self):
        if self.fd is None:
            raise RuntimeError('process writer not held')
        fd, self.fd = self.fd, None
        os.close(fd)
