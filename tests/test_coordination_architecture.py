import sqlite3
import threading
import pytest
from bcc.coordination import Coordinator, BusyOwner, StaleOwner


def test_fencing_crash_recovery_and_stop(tmp_path):
    now = [100.0]
    path = tmp_path / 'authority.sqlite'
    a = Coordinator(path, clock=lambda: now[0]); a.initialize()
    b = Coordinator(path, clock=lambda: now[0])
    a.register('any/strategy/1'); a.set_desired('any/strategy/1', 'RUNNING')
    g1 = a.acquire('any/strategy/1', 'worker1', 10)
    with pytest.raises(BusyOwner):
        b.acquire('any/strategy/1', 'worker2', 10)
    now[0] = 111.0
    g2 = b.acquire('any/strategy/1', 'worker2', 10)
    assert g2 > g1
    with pytest.raises(StaleOwner):
        a.release('any/strategy/1', 'worker1', g1)
    with pytest.raises(StaleOwner):
        a.renew('any/strategy/1', 'worker1', g1, 10)
    b.set_desired('any/strategy/1', 'STOPPED')
    with pytest.raises(StaleOwner):
        b.renew('any/strategy/1', 'worker2', g2, 10)
    with pytest.raises(StaleOwner):
        b.acquire('any/strategy/1', 'worker3', 10)


def test_transactional_owner_validation(tmp_path):
    coord = Coordinator(tmp_path / 'fleet.sqlite', clock=lambda: 100)
    coord.initialize(); coord.register('a'); coord.set_desired('a', 'RUNNING')
    gen = coord.acquire('a', 'x', 20)
    with coord._write() as db:
        coord.assert_owner(db, 'a', 'x', gen)
    assert coord.update_config_version('a', 1) == 2
    with pytest.raises(StaleOwner):
        coord.update_config_version('a', 1)
    coord.release('a', 'x', gen)
    with coord._write() as db:
        with pytest.raises(StaleOwner):
            coord.assert_owner(db, 'a', 'x', gen)


def test_unbounded_registry_bounded_only_by_storage(tmp_path):
    coord = Coordinator(tmp_path / 'fleet.sqlite'); coord.initialize()
    for i in range(230):
        coord.register(f'bot-{i}')
    assert len(coord.snapshot()) == 230
    assert all(x['config_version'] == 1 for x in coord.snapshot())


def test_competing_claims_exactly_one_winner(tmp_path):
    path = tmp_path / 'fleet.sqlite'
    initial = Coordinator(path, clock=lambda: 100); initial.initialize()
    initial.register('shared'); initial.set_desired('shared', 'RUNNING')
    barrier = threading.Barrier(9)
    outcomes = []
    lock = threading.Lock()

    def contender(i):
        barrier.wait()
        try:
            result = Coordinator(path, clock=lambda: 100).acquire('shared', f'p{i}', 10)
        except BusyOwner:
            result = None
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=contender, args=(i,)) for i in range(8)]
    for t in threads: t.start()
    barrier.wait()
    for t in threads: t.join(timeout=5)
    assert len(outcomes) == 8
    assert sum(x is not None for x in outcomes) == 1


def test_fail_closed_no_unregistered_owner(tmp_path):
    path = tmp_path / 'fleet.sqlite'
    coord = Coordinator(path); coord.initialize()
    with pytest.raises(KeyError):
        coord.acquire('missing', 'w', 5)
    with pytest.raises(KeyError):
        coord.set_desired('missing', 'RUNNING')
