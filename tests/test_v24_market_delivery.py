"""Bounded REST cadence, single compact writer and Windows heartbeat reads."""
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from bcc import storage
from bcc.rest_mark_fallback import HOST, KEY, RestMarkPump, build_snapshot, publish_compact


def snapshot(stamp):
    raw = dict(retCode=0, time=stamp, result=dict(category='linear', list=[
        dict(symbol='BTCUSDT', markPrice='100', bid1Price='99', ask1Price='101')]))
    return build_snapshot(raw, lambda t: True)


def test_compact_writer_does_not_replace_raw_bulk_and_keeps_original_time():
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE cache(k TEXT PRIMARY KEY,value TEXT,until REAL,host TEXT)')
        db.execute('INSERT INTO cache VALUES(?,?,?,?)', ('bulk', 'original raw', 123., HOST))
        first = snapshot(10000000)
        assert publish_compact(db, first, json.dumps(first), lambda t: True)
        saved = db.execute('SELECT value,until FROM cache WHERE k=?', (KEY,)).fetchone()
        assert json.loads(saved[0]) == first
        assert db.execute('SELECT value,until FROM cache WHERE k=?', ('bulk',)).fetchone() == ('original raw', 123.)
        for stamp in (10000000, 9999999):
            data = snapshot(stamp)
            assert not publish_compact(db, data, json.dumps(data), lambda t: True)
        assert db.execute('SELECT value,until FROM cache WHERE k=?', (KEY,)).fetchone() == saved
        newer = snapshot(10000100)
        assert not publish_compact(db, newer, json.dumps(newer), lambda t: False)
        assert publish_compact(db, newer, json.dumps(newer), lambda t: True)
        assert json.loads(db.execute('SELECT value FROM cache WHERE k=?', (KEY,)).fetchone()[0]) == newer
        assert db.execute('SELECT count(*) FROM cache').fetchone()[0] == 2


@pytest.mark.parametrize('period', [.49, 1.01, True, float('nan'), float('inf')])
def test_invalid_bulk_cadence(period):
    with pytest.raises(ValueError):
        RestMarkPump(lambda: True, period=period)


def test_fast_pump_is_singleflight_bounded_and_never_catches_up_in_bursts():
    now = [10.]
    entered, release = threading.Event(), threading.Event()
    starts = []
    def fetch():
        starts.append(now[0])
        entered.set()
        assert release.wait(3)
        return True
    pump = RestMarkPump(fetch, monotonic=lambda: now[0], period=.65)
    try:
        pump.poll(False)
        assert pump.job is None
        pump.poll(True)
        assert entered.wait(1)
        now[0] = 12.
        for _ in range(100):
            pump.poll(True)
        assert len(starts) == 1
        release.set()
        pump.job.result(2)
        pump.poll(True)
        pump.job.result(2)
        pump.poll(True)
        assert pump.job is None and len(starts) == 2
        now[0] += .64
        pump.poll(True)
        assert pump.job is None
        now[0] += .02
        pump.poll(True)
        pump.job.result(2)
        pump.poll(False)
        assert pump.completed == 3 and pump.job is None
        assert pump.stats()['period_seconds'] == .65
        assert pump.stats()['max_start_gap_seconds'] == 2.
    finally:
        release.set()
        pump.close()


def test_recoverable_error_classifies_cause_without_message():
    def fetch():
        try:
            raise PermissionError('private detail')
        except PermissionError as exc:
            raise ValueError('private detail') from exc
    pump = RestMarkPump(fetch, recoverable=(ValueError,), monotonic=lambda: 10., period=.65)
    try:
        pump.poll(True)
        with pytest.raises(ValueError):
            pump.job.result(2)
        pump.poll(False)
        assert pump.stats()['error_types'] == {'PermissionError': 1}
        assert 'private detail' not in str(pump.stats())
    finally:
        pump.close()


def _sharing_error():
    exc = PermissionError('simulated Windows sharing violation')
    exc.winerror = 32
    return exc


def test_heartbeat_read_retries_only_a_transient_sharing_violation(tmp_path, monkeypatch):
    path = tmp_path / 'process.json'
    path.write_text('{"status":"RUNNING"}', encoding='utf-8')
    original = Path.read_text
    attempts = []
    def read(self, *args, **kwargs):
        attempts.append(self)
        if len(attempts) <= 2:
            raise _sharing_error()
        return original(self, *args, **kwargs)
    now = [0.]
    monkeypatch.setattr(storage, 'time', SimpleNamespace(monotonic=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0]+s)))
    monkeypatch.setattr(Path, 'read_text', read)
    assert storage.load_json(path) == {'status':'RUNNING'}
    assert len(attempts) == 3


def test_persistent_sharing_violation_and_other_files_fail_closed(tmp_path, monkeypatch):
    now = [0.]
    attempts = []
    def read(self, *a, **kw):
        attempts.append(self)
        raise _sharing_error()
    monkeypatch.setattr(storage, 'time', SimpleNamespace(monotonic=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0]+s)))
    monkeypatch.setattr(Path, 'read_text', read)
    with pytest.raises(PermissionError):
        storage.load_json(tmp_path/'process.json')
    assert .25 <= now[0] < .28 and 2 <= len(attempts) <= 12
    attempts.clear()
    with pytest.raises(PermissionError):
        storage.load_json(tmp_path/'config.json')
    assert len(attempts) == 1


def test_malformed_heartbeat_is_not_replaced_with_old_state(tmp_path):
    path = tmp_path/'process.json'
    path.write_text('{"status":NaN}', encoding='utf-8')
    with pytest.raises(ValueError):
        storage.load_json(path)
