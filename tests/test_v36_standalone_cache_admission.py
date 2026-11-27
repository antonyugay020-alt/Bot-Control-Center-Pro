"""Standalone WAL readers must not enter the writer queue unnecessarily."""
from contextlib import contextmanager
import sqlite3
import time

import pytest

from bcc.data_cache import CacheBusy, CacheContention, ProviderDenied, PublicCache

URL = 'https://api.bybit.com/v5/market/kline'
PARAMS = {'symbol': 'BTCUSDT'}
HOST = 'api.bybit.com'
VALUE = {'ok': True}


@pytest.fixture
def caches(tmp_path, monkeypatch):
    monkeypatch.delenv('BCC_PUBLIC_DEMAND_HUB_CANDIDATE', raising=False)
    monkeypatch.delenv('BCC_MARKET_HUB_MODE', raising=False)
    monkeypatch.delenv('BCC_BYBIT_REST_MARK_FALLBACK', raising=False)
    producer = PublicCache(tmp_path / 'shared.sqlite')
    reader = PublicCache(producer.path)
    try:
        yield producer, reader
    finally:
        reader.close()
        producer.close()


def no_writer():
    pytest.fail('read-only observation entered the writer path')


def test_cold_attach_and_fresh_cache_hit_while_sqlite_writer_held(caches, monkeypatch):
    producer, reader = caches
    key, owner, _ = producer.begin(URL, PARAMS, HOST)
    assert producer.finish(key, owner, HOST, VALUE, ttl=30)
    blocker = sqlite3.connect(producer.path, isolation_level=None)
    try:
        blocker.execute('BEGIN IMMEDIATE')
        monkeypatch.setattr(reader, 'db', no_writer)
        assert reader.denied_status(HOST) is None
        assert reader.begin(URL, PARAMS, HOST) == (key, None, VALUE)
        assert reader._connection is None
        assert reader.stats['operations'] == 0
    finally:
        blocker.close()
    reader.close()
    assert not reader._ready


def test_active_lease_waits_without_writer_and_observes_committed_result(caches, monkeypatch):
    producer, reader = caches
    key, owner, _ = producer.begin(URL, PARAMS, HOST)
    with monkeypatch.context() as patch:
        patch.setattr(reader, 'db', no_writer)
        with pytest.raises(CacheBusy, match='другим ботом') as exc:
            reader.begin(URL, PARAMS, HOST)
        assert not isinstance(exc.value, CacheContention)
        assert producer.finish(key, owner, HOST, VALUE, ttl=30)
        assert reader.begin(URL, PARAMS, HOST) == (key, None, VALUE)
    with producer.read_db() as db:
        assert db.execute('SELECT count(*) FROM lease').fetchone()[0] == 0
        assert db.execute('SELECT count FROM request_budget WHERE host=?', (HOST,)).fetchone()[0] == 1


@pytest.mark.parametrize('status', [403, 451])
def test_read_path_does_not_bypass_committed_denial(caches, monkeypatch, status):
    producer, reader = caches
    key, owner, _ = producer.begin(URL, PARAMS, HOST)
    producer.record_http_denial(key, owner, HOST, cooldown=90, denial_status=status)
    # Even an inconsistent, still-resident row must not defeat the fence.
    with producer.db() as db:
        db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?)',
                   (key, '{"ok":true}', time.time()+30, HOST))
    monkeypatch.setattr(reader, 'db', no_writer)
    with pytest.raises(ProviderDenied) as exc:
        reader.begin(URL, PARAMS, HOST)
    assert exc.value.http_status == status


def test_expired_lease_is_not_read_only_admission(caches):
    producer, reader = caches
    key, owner, _ = producer.begin(URL, PARAMS, HOST)
    with producer.db() as db:
        db.execute('UPDATE lease SET until=? WHERE k=?', (time.time()-1, key))
    reader._ensure_schema()
    blocker = sqlite3.connect(producer.path, isolation_level=None)
    try:
        blocker.execute('BEGIN IMMEDIATE')
        with pytest.raises(CacheContention):
            reader.begin(URL, PARAMS, HOST)
        blocker.rollback()
    finally:
        blocker.close()
    replacement, new_owner, value = reader.begin(URL, PARAMS, HOST)
    assert replacement == key and new_owner and new_owner != owner and value is None
    assert not producer.finish(key, owner, HOST, VALUE, ttl=30)
    assert reader.finish(key, new_owner, HOST, VALUE, ttl=30)


@pytest.mark.parametrize('outcome', ['lease', 'cache', 'denial'])
def test_read_miss_rechecks_authority_inside_write_transaction(caches, monkeypatch, outcome):
    producer, reader = caches
    producer._ensure_schema()
    reader._ensure_schema()
    actual = reader.db
    calls = []

    @contextmanager
    def race():
        # Another process can publish/reserve/deny between read miss and
        # writer admission. Simulate this precisely, without changing clocks.
        if not calls:
            key, owner, _ = producer.begin(URL, PARAMS, HOST)
            calls.append(key)
            if outcome == 'cache':
                assert producer.finish(key, owner, HOST, VALUE, ttl=30)
            elif outcome == 'denial':
                producer.record_http_denial(key, owner, HOST, cooldown=90, denial_status=403)
        with actual() as db:
            yield db

    monkeypatch.setattr(reader, 'db', race)
    if outcome == 'lease':
        with pytest.raises(CacheBusy, match='другим ботом'):
            reader.begin(URL, PARAMS, HOST)
    elif outcome == 'denial':
        with pytest.raises(ProviderDenied):
            reader.begin(URL, PARAMS, HOST)
    else:
        assert reader.begin(URL, PARAMS, HOST) == (calls[0], None, VALUE)
    assert len(calls) == 1
    with producer.read_db() as db:
        assert db.execute('SELECT count FROM request_budget WHERE host=?', (HOST,)).fetchone()[0] == 1


def test_non_wal_schema_is_migrated_not_accepted(tmp_path, monkeypatch):
    monkeypatch.delenv('BCC_PUBLIC_DEMAND_HUB_CANDIDATE', raising=False)
    path = tmp_path / 'old.sqlite'
    db = sqlite3.connect(path)
    try:
        db.execute('CREATE TABLE unrelated(x)')
        db.commit()
    finally:
        db.close()
    cache = PublicCache(path)
    try:
        cache._ensure_schema()
        with cache.read_db() as reader:
            assert reader.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
            assert cache._schema_current(reader)
        assert cache.stats['operations'] > 0
    finally:
        cache.close()
