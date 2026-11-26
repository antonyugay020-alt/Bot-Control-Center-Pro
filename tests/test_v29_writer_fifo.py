"""Queued writer admission: FIFO, crashed waiter, slow Windows-like polling."""
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bcc.public_writer import writer_gate
from bcc.public_writer_ipc import _ByteLock

ROOT = Path(__file__).resolve().parents[1]


def child(code, *args):
    return subprocess.Popen([sys.executable, '-c', code, *map(str, args)], cwd=ROOT,
                            env=dict(os.environ, PYTHONPATH=str(ROOT)),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.communicate(timeout=10)


def active_count(queue):
    mutex = _ByteLock(queue / 'register.lock')
    try:
        if not mutex.try_lock():
            return None
        count = 0
        for path in queue.glob('*.slot'):
            probe = _ByteLock(path, create=False)
            try:
                count += not probe.try_lock()
            finally:
                probe.close()
        return count
    finally:
        mutex.close()


def wait_count(queue, count, proc):
    deadline = time.monotonic() + 5
    while active_count(queue) != count:
        assert proc.poll() is None, proc.communicate()
        assert time.monotonic() < deadline
        time.sleep(.005)


WAITER = '''import sys
from bcc.public_writer import writer_gate
path, label, output=sys.argv[1:]
with writer_gate(path):
    with open(output, 'a', encoding='ascii') as f:
        f.write(label+'\\n')
'''


def test_fifo_no_overtake_and_crashed_waiter_reaped(tmp_path):
    path = tmp_path/'db.sqlite'
    queue = Path(str(path)+'.writer.lock.slots')
    output = tmp_path/'order.txt'
    gate = writer_gate(path)
    procs = []
    try:
        with gate:
            for i in range(3):
                p = child(WAITER, path, i, output)
                procs.append(p)
                wait_count(queue, i+2, p)
            # Kill the first queued waiter while the owner still holds the
            # writer. Its slot becomes available only after OS releases the lock.
            stop(procs[0])
            assert not output.exists()
        # The previous owner must not jump in front of either queued waiter.
        with gate:
            with output.open('a', encoding='ascii') as f:
                f.write('owner\n')
        for p in procs[1:]:
            out, err = p.communicate(timeout=10)
            if p.returncode != 0:
                pytest.fail(err.decode('utf-8', errors='replace'), pytrace=False)
        assert output.read_text().splitlines() == ['1', '2', 'owner']
        assert active_count(queue) == 0
    finally:
        for p in procs:
            stop(p)


def test_timed_out_slot_is_released_without_skipping_live_owner(tmp_path):
    path = tmp_path/'db.sqlite'
    queue = Path(str(path)+'.writer.lock.slots')
    code = '''import sys
from bcc.public_writer import writer_gate
g=writer_gate(sys.argv[1]); assert not g.acquire(timeout=.15)
'''
    gate = writer_gate(path)
    with gate:
        p = child(code, path)
        try:
            out, err = p.communicate(timeout=10)
            if p.returncode != 0:
                pytest.fail(err.decode('utf-8', errors='replace'), pytrace=False)
            assert active_count(queue) == 1
        finally:
            stop(p)
    assert active_count(queue) == 0


def test_slow_polling_writers_keep_original_admission_budget(tmp_path):
    path = tmp_path/'db.sqlite'
    db = sqlite3.connect(path)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE rows(k TEXT PRIMARY KEY)')
        db.commit()
    finally:
        db.close()
    code = '''import sys, sqlite3, time
from contextlib import contextmanager
from bcc import public_writer_ipc as ipc
from bcc.public_writer import writer_gate,writer_transaction
path, worker=sys.argv[1:]
# Reproduce coarse scheduling without altering the 1-second admission budget.
original_sleep=time.sleep
ipc.time.sleep=lambda seconds:original_sleep(max(.016,seconds))
@contextmanager
def connect():
    db=sqlite3.connect(path,isolation_level=None)
    try:
        with db:yield db
    finally:db.close()
g=writer_gate(path)
for i in range(60):
    with writer_transaction(g,connect) as db:
        db.execute('INSERT INTO rows VALUES(?)',(worker+':'+str(i),))
        original_sleep(.004)
'''
    procs = [child(code, path, i) for i in range(4)]
    try:
        for p in procs:
            out, err = p.communicate(timeout=40)
            if p.returncode != 0:
                pytest.fail(err.decode('utf-8', errors='replace'), pytrace=False)
        db = sqlite3.connect(path)
        try:
            assert db.execute('SELECT count(*) FROM rows').fetchone()[0] == 240
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        finally:
            db.close()
        queue = Path(str(path)+'.writer.lock.slots')
        assert active_count(queue) == 0
        assert len(list(queue.glob('*.slot'))) <= len(procs)
    finally:
        for p in procs:
            stop(p)


def test_slots_are_reused_without_unlink_or_rename(tmp_path, monkeypatch):
    path = tmp_path/'db.sqlite'
    queue = Path(str(path)+'.writer.lock.slots')
    def forbidden(*args, **kwargs):
        raise AssertionError('writer slots must not be removed or renamed')
    monkeypatch.setattr(Path, 'unlink', forbidden)
    monkeypatch.setattr(Path, 'rename', forbidden)
    monkeypatch.setattr(Path, 'replace', forbidden)
    gate = writer_gate(path)
    for _ in range(100):
        with gate:
            assert active_count(queue) == 1
        assert active_count(queue) == 0
    assert len(list(queue.glob('*.slot'))) == 1
