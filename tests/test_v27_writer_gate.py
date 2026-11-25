"""Real shared-WAL integration and bounded FIFO writer admission."""
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from bcc.data_cache import PublicCache
from bcc.market_hub import HubRegistry
from bcc.public_writer import WriterGate, writer_gate


def wait_for_waiters(gate, count):
    deadline = time.monotonic() + 3.
    while gate.stats()['waiting'] != count:
        assert time.monotonic() < deadline
        time.sleep(.001)


def test_fifo_order_with_timed_out_middle_and_reentry():
    gate = WriterGate()
    order = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        with gate:
            with gate:
                def enter(name, timeout):
                    if not gate.acquire(timeout=timeout):
                        return False
                    try:
                        order.append(name)
                    finally:
                        gate.release()
                    return True
                first = pool.submit(enter, 'first', 5.)
                wait_for_waiters(gate, 1)
                expired = pool.submit(enter, 'expired', .5)
                wait_for_waiters(gate, 2)
                last = pool.submit(enter, 'last', 5.)
                wait_for_waiters(gate, 3)
                assert expired.result(timeout=2) is False
                assert gate.stats()['waiting'] == 2
                assert order == []
            assert gate.stats()['held']
        assert first.result(timeout=3) and last.result(timeout=3)
    assert order == ['first', 'last']
    assert gate.stats()['timeouts'] == 1
    assert not gate.stats()['held'] and gate.stats()['waiting'] == 0


def test_gate_aliases_reentry_and_nonowner_release(tmp_path):
    path = tmp_path / 'db.sqlite'
    gate = writer_gate(path)
    assert writer_gate(path.parent / '.' / path.name) is gate
    assert writer_gate(tmp_path / 'other.sqlite') is not gate
    with gate, ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(gate.acquire, False).result(2) is False
        error = pool.submit(gate.release)
        with pytest.raises(RuntimeError):
            error.result(2)
        assert gate.acquire(False)
        gate.release()
    with pytest.raises(RuntimeError):
        gate.release()


def test_cache_and_registry_share_writer_but_registry_read_stays_independent(tmp_path):
    path = tmp_path / 'db.sqlite'
    cache = PublicCache(path)
    registry = HubRegistry(path)
    cache._ensure_schema()
    try:
        assert cache._guard is registry._writer_gate
        assert registry.claim_leader('owner', lifetime=120)
        token = registry.leader_token('owner')
        with ThreadPoolExecutor(max_workers=2) as pool:
            with cache.db() as db:
                db.execute('BEGIN IMMEDIATE')
                renew = pool.submit(registry.renew_leader, 'owner', token=token, lifetime=120)
                wait_for_waiters(cache._guard, 1)
                assert not renew.done()
                # WAL read must not queue behind the same local writer.
                assert pool.submit(registry.leader_token, 'owner').result(2) == token
                db.commit()
            assert renew.result(2)
        # A failed registry transaction releases the same local gate.
        with pytest.raises(RuntimeError):
            with registry._write_connect() as db:
                assert db.in_transaction
                raise RuntimeError('abort')
        with cache.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.commit()
    finally:
        cache.close()


def test_mixed_registry_and_public_cache_writes_preserve_data_and_fences(tmp_path):
    path = tmp_path / 'db.sqlite'
    cache = PublicCache(path)
    registry = HubRegistry(path)
    cache._ensure_schema()
    assert registry.claim_leader('owner', lifetime=120)
    token = registry.leader_token('owner')
    begin = threading.Barrier(4)
    def publish(worker):
        begin.wait(timeout=4)
        for i in range(60):
            with cache.db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT INTO cache(k,value,until,host) VALUES(?,?,?,?)',
                           (f'{worker}:{i}', '{}', time.time()+60, 'api.bybit.com'))
                db.commit()
    def register(worker):
        begin.wait(timeout=4)
        for i in range(60):
            key = registry.subscribe('https://api.bybit.com/v5/market/tickers',
                                     {'category':'linear','symbol':f'R{worker}X{i}USDT'})
            assert key
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = [pool.submit(publish, i) for i in range(2)] + [pool.submit(register, i) for i in range(2)]
            for job in jobs:
                job.result(timeout=15)
        with cache.read_db() as db:
            assert db.execute('SELECT count(*) FROM cache').fetchone()[0] == 120
            assert db.execute('SELECT count(*) FROM hub_subscriptions').fetchone()[0] == 120
        assert registry.leader_token('owner') == token
        assert cache._guard.stats()['timeouts'] == 0
        assert cache.stats['sqlite_busy'] == 0
        assert not registry.renew_leader('owner', token='stale', lifetime=120)
    finally:
        cache.close()


def test_external_sqlite_writer_is_retried_before_registry_statements(tmp_path):
    path = tmp_path / 'db.sqlite'
    cache = PublicCache(path)
    registry = HubRegistry(path)
    cache._ensure_schema()
    assert registry.claim_leader('owner', lifetime=120)
    token = registry.leader_token('owner')
    blocker = sqlite3.connect(path, isolation_level=None)
    try:
        blocker.execute('BEGIN IMMEDIATE')
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(registry.renew_leader, 'owner', token=token, lifetime=120)
            # Hold the independent SQLite connection beyond one 100ms attempt.
            time.sleep(.25)
            assert not future.done()
            blocker.rollback()
            assert future.result(3)
        assert registry.leader_token('owner') == token
        # A failed caller must not be replayed by the admission retry loop.
        calls = []
        with pytest.raises(sqlite3.OperationalError, match='caller failure'):
            with registry._write_connect() as db:
                calls.append(1)
                raise sqlite3.OperationalError('caller failure')
        assert calls == [1]
    finally:
        blocker.close()
        cache.close()
