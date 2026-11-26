"""Public writer interprocess exclusion, bounded timeout and crash recovery."""
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from contextlib import contextmanager

import pytest

from bcc.public_writer import writer_gate, writer_transaction

ROOT = Path(__file__).resolve().parents[1]


def child(code, *args):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.Popen([sys.executable, '-c', code, *map(str, args)], cwd=ROOT,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def wait_for(path, proc):
    deadline = time.monotonic() + 10
    while not path.exists():
        assert proc.poll() is None, proc.communicate()
        assert time.monotonic() < deadline
        time.sleep(.01)


def stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.communicate(timeout=10)


def test_process_timeout_and_death_release(tmp_path):
    path = tmp_path / 'cache.sqlite'
    ready = tmp_path / 'ready'
    proc = child('''import sys,time
from pathlib import Path
from bcc.public_writer import writer_gate
g=writer_gate(sys.argv[1])
with g:
    Path(sys.argv[2]).touch()
    time.sleep(60)
''', path, ready)
    gate = writer_gate(path)
    try:
        wait_for(ready, proc)
        started = time.monotonic()
        assert not gate.acquire(timeout=.1)
        assert time.monotonic() - started < 2
        assert gate.stats()['process_timeouts'] == 1
        assert not gate.stats()['held']
        assert not gate.acquire(False)
    finally:
        stop(proc)
    assert gate.acquire(timeout=2)
    gate.release()
    assert Path(str(path)+'.writer.lock').is_file()


def test_mixed_process_wal_writers_no_lost_commits(tmp_path):
    path = tmp_path / 'cache.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE rows(k TEXT PRIMARY KEY)')
    code = '''import sys,sqlite3
from contextlib import contextmanager
from bcc.public_writer import writer_gate,writer_transaction
path,worker=sys.argv[1:]
g=writer_gate(path)
@contextmanager
def connect():
    db=sqlite3.connect(path,isolation_level=None)
    try:
        with db:yield db
    finally:db.close()
for i in range(60):
    with writer_transaction(g,connect) as db:
        db.execute('INSERT INTO rows VALUES(?)',(worker+':'+str(i),))
print('PASS')
'''
    procs = [child(code,path,i) for i in range(4)]
    try:
        for proc in procs:
            out,err=proc.communicate(timeout=30)
            assert proc.returncode==0,(out,err)
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT count(*) FROM rows').fetchone()[0]==240
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    finally:
        for proc in procs:stop(proc)


def test_local_reentry_does_not_release_process_lock(tmp_path):
    path = tmp_path/'cache.sqlite'
    gate=writer_gate(path)
    code='''import sys
from bcc.public_writer import writer_gate
g=writer_gate(sys.argv[1]);ok=g.acquire(timeout=.05)
if ok:g.release()
sys.exit(0 if ok else 1)
'''
    with gate:
        with gate:
            pass
        proc=child(code,path)
        try:
            proc.communicate(timeout=10)
            assert proc.returncode==1
        finally:stop(proc)
    proc=child(code,path)
    try:
        proc.communicate(timeout=10)
        assert proc.returncode==0
    finally:stop(proc)


def test_sqlite_remains_authority_for_noncooperating_writer(tmp_path):
    path=tmp_path/'cache.sqlite'
    blocker=sqlite3.connect(path,isolation_level=None)
    blocker.execute('BEGIN IMMEDIATE')
    @contextmanager
    def connect():
        db=sqlite3.connect(path,isolation_level=None)
        try:
            with db:yield db
        finally:db.close()
    gate=writer_gate(path)
    try:
        with pytest.raises(sqlite3.OperationalError,match='writer admission'):
            with writer_transaction(gate,connect):
                pytest.fail('SQLite transaction was not admitted')
        assert not gate.stats()['held']
    finally:
        blocker.close()
    with writer_transaction(gate,connect) as db:
        assert db.in_transaction
