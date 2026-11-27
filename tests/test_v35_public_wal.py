"""Actual WAL files, independent checkpoint and safe fallback under load."""
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from bcc.public_wal import WalMaintenance, WriterTiming, configure_writer, DEFAULT_PAGES


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'public.sqlite'
    db = sqlite3.connect(path, isolation_level=None)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE market(k INTEGER PRIMARY KEY, value TEXT)')
    try:
        yield path, db
    finally:
        db.close()


def wait_for(predicate, timeout=5.):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.01)


def test_checkpoint_ownership_and_shutdown_restore_default(database):
    path, db = database
    assert configure_writer(db, path) == DEFAULT_PAGES
    lane = WalMaintenance(path)
    lane.start()
    try:
        assert configure_writer(db, path) == 0
        assert db.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == 0
        assert lane.stats()['checkpoints'] > 0
        with pytest.raises(RuntimeError, match='already started'):
            WalMaintenance(path).start()
    finally:
        lane.close()
    assert configure_writer(db, path) == DEFAULT_PAGES
    assert db.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == DEFAULT_PAGES


def test_checkpoint_stale_or_backlogged_restores_auto(database):
    path, db = database
    lane = WalMaintenance(path, period=10.)
    lane.start()
    try:
        with lane._guard:
            lane._last = time.monotonic() - 3.
        assert configure_writer(db, path) == DEFAULT_PAGES
        with lane._guard:
            lane._last = time.monotonic()
            lane._backlog = 20000
        assert configure_writer(db, path) == DEFAULT_PAGES
        with lane._guard:
            lane._backlog = 0
        assert configure_writer(db, path) == 0
    finally:
        lane.close()


def test_passive_checkpoint_never_requires_reader_to_end(database):
    path, db = database
    lane = WalMaintenance(path, period=.02)
    reader = sqlite3.connect(path, isolation_level=None)
    lane.start()
    try:
        configure_writer(db, path)
        db.execute('INSERT INTO market VALUES(1,?)', ('before',))
        reader.execute('BEGIN')
        assert reader.execute('SELECT value FROM market').fetchone()[0] == 'before'
        before = lane.stats()['checkpoints']
        for i in range(2, 50):
            db.execute('INSERT INTO market VALUES(?,?)', (i, 'x'*4096))
        wait_for(lambda: lane.stats()['checkpoints'] > before and lane.stats()['max_backlog_pages'] > 0)
        assert reader.execute('SELECT count(*) FROM market').fetchone()[0] == 1
        assert lane.stats()['max_backlog_pages'] > 0
        reader.rollback()
        wait_for(lambda: lane.stats()['last_log_pages'] == lane.stats()['last_checkpointed_pages'])
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        reader.close()
        lane.close()


def test_concurrent_market_writer_reader_and_maintenance(database):
    path, db = database
    lane = WalMaintenance(path, period=.02)
    lane.start()
    started = threading.Event()
    try:
        configure_writer(db, path)
        def publish():
            writer = sqlite3.connect(path, isolation_level=None, timeout=1.)
            try:
                for i in range(240):
                    configure_writer(writer, path)
                    writer.execute('BEGIN IMMEDIATE')
                    writer.executemany('INSERT OR REPLACE INTO market VALUES(?,?)',
                                       [(j, str(i)+'x'*1024) for j in range(59)])
                    writer.commit()
                    started.set()
                return 240*59
            finally:
                writer.close()
        def read():
            assert started.wait(3.)
            reader = sqlite3.connect(path, isolation_level=None, timeout=1.)
            try:
                for _ in range(400):
                    reader.execute('BEGIN')
                    rows = reader.execute('SELECT value FROM market').fetchall()
                    assert len(rows) == 59 and len(set(rows)) == 1
                    reader.rollback()
                return 400
            finally:
                reader.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            p = pool.submit(publish)
            readers = [pool.submit(read) for _ in range(3)]
            assert p.result(20) == 14160
            assert sum(r.result(20) for r in readers) == 1200
        assert db.execute('SELECT count(*) FROM market').fetchone()[0] == 59
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert lane.stats()['errors'] == 0
    finally:
        lane.close()


def test_writer_timing_keeps_separate_wait_and_hold():
    timing = WriterTiming()
    timing.record('market', .1, .3)
    timing.record('market', .2, .1)
    timing.record('registry', .4, .05)
    stats = timing.stats()
    assert stats['market'] == dict(count=2,max_wait_seconds=.2,max_hold_seconds=.3,holds_over_200ms=1)
    assert stats['registry']['max_wait_seconds'] == .4
    stats['market']['count'] = 0
    assert timing.stats()['market']['count'] == 2


def test_busy_checkpoint_with_unknown_frame_counts_retries_startup(database, monkeypatch):
    path, db = database
    original = sqlite3.connect
    attempts = []
    class BusyResult:
        def fetchone(self):
            return (1, -1, -1)
    class Connection:
        def __init__(self, real):
            self.real = real
        def execute(self, sql):
            if sql == 'PRAGMA wal_checkpoint(PASSIVE)':
                attempts.append(1)
                if len(attempts) == 1:
                    return BusyResult()
            return self.real.execute(sql)
        def close(self):
            self.real.close()
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: Connection(original(*a, **kw)))
    lane = WalMaintenance(path, period=.02)
    lane.start()
    try:
        assert len(attempts) >= 2 and lane.stats()['busy'] >= 1
        assert lane.healthy() and configure_writer(db, path) == 0
    finally:
        lane.close()
