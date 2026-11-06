"""Per-bot cross-process ordering of control revisions and PAPER commits.

The control registry and account ledger deliberately live in separate SQLite
files. A cross-process gate orders a STOP/retirement revision against the entire
financial transaction. A writer that acquires this gate before STOP may finish;
once STOP is committed, no transaction from the old launch revision can commit.
This is a narrow per-owner gate, not a global fleet lock or a trading lease.
The ledger's own SQLite execution generation remains the financial fence.
"""
import hashlib
import os
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def command_gate(root, key, *, timeout=15.0, clock=time.monotonic):
    if not isinstance(key, str) or not key:
        raise ValueError('nonempty owner key required')
    if timeout <= 0:
        raise ValueError('positive gate timeout required')
    # Hashed names avoid platform-specific path characters, traversal and
    # excessively long filenames. Do not place locks inside deletable bot dirs.
    token = hashlib.sha256(key.encode('utf-8')).hexdigest()
    folder = Path(root) / '.control_gates'
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder / (token + '.lock')).open('a+b')
    held = False
    try:
        deadline = clock() + timeout
        while not held:
            try:
                if os.name == 'nt':
                    import msvcrt
                    if handle.seek(0, 2) == 0:
                        handle.write(b'0')
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
            except OSError as exc:
                if clock() >= deadline:
                    raise TimeoutError('Control/ledger commit gate is busy') from exc
                time.sleep(min(0.025, max(0, deadline - clock())))
        yield
    finally:
        if held:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        else:
            handle.close()
