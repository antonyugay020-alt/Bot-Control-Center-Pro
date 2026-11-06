"""Control-plane system invariants under arbitrary registration pressure."""
import threading
import time
from bcc.fleet import Fleet
from bcc.runtime_service import ReconciliationPlane


def _collect_until(fleet, predicate, timeout=3.0):
    outcomes = []
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        outcomes.extend(fleet.collect())
        if predicate(outcomes):
            return outcomes
        time.sleep(.005)
    raise AssertionError(f'Work did not complete: {outcomes}')


def test_priority_displaces_only_unstarted_normal_task():
    hold = threading.Event()
    inside = threading.Event()
    calls = []
    fleet = Fleet(workers=1, max_pending=1)
    try:
        def slow():
            calls.append('running')
            inside.set()
            hold.wait(2)
        fleet.register('running', slow)
        fleet.register('normal', lambda: calls.append('normal'))
        fleet.register('stop', lambda: calls.append('stop'))
        assert fleet.request('running')
        assert inside.wait(1)
        assert fleet.request('normal')
        assert fleet.request('stop', urgent=True)
        assert fleet.snapshot()['waiting'] == 1
        hold.set()
        _collect_until(fleet, lambda o: len(o) >= 2)
        assert calls == ['running', 'stop']
    finally:
        hold.set()
        fleet.close()


def test_priority_promotes_existing_owner_without_duplicate_execution():
    hold = threading.Event()
    started = threading.Event()
    calls = []
    fleet = Fleet(workers=1, max_pending=2)
    try:
        def slow():
            started.set()
            hold.wait(2)
        fleet.register('busy', slow)
        fleet.register('a', lambda: calls.append('a'))
        fleet.register('b', lambda: calls.append('b'))
        fleet.request('busy')
        assert started.wait(1)
        fleet.request('a')
        fleet.request('b')
        fleet.request('b', urgent=True)
        assert fleet.snapshot()['waiting'] == 2
        hold.set()
        _collect_until(fleet, lambda o: len(o) >= 3)
        assert calls == ['b', 'a']
    finally:
        hold.set()
        fleet.close()


def test_ephemeral_registry_stays_bounded_with_many_owners():
    class Backend:
        def reconcile_key(self, key):
            return key
    plane = ReconciliationPlane(Backend(), workers=3)
    seen = set()
    try:
        for start in range(0, 240, 3):
            batch = [f'bot-{n}' for n in range(start, start + 3)]
            plane.schedule(batch)
            deadline = time.monotonic() + 2
            while not set(batch) <= seen and time.monotonic() < deadline:
                seen.update(plane.collect())
                time.sleep(.001)
            assert set(batch) <= seen
            assert plane.fleet.snapshot()['registered'] <= 3
        assert len(seen) == 240
        assert plane.fleet.snapshot()['registered'] == 0
    finally:
        plane.close()


def test_urgent_work_under_full_service_queue():
    hold = threading.Event()
    running = threading.Event()
    completed = []
    class Backend:
        def reconcile_key(self, key):
            if key == 'busy':
                running.set()
                hold.wait(2)
            completed.append(key)
            return key
    plane = ReconciliationPlane(Backend(), workers=1)
    try:
        plane.schedule(['busy'])
        assert running.wait(1)
        plane.schedule(['regular'])
        plane.schedule(['stop'], urgent=True)
        assert plane.fleet.snapshot()['waiting'] == 1
        hold.set()
        deadline = time.monotonic() + 2
        while len(completed) < 2 and time.monotonic() < deadline:
            plane.collect()
            time.sleep(.005)
        assert completed == ['busy', 'stop']
    finally:
        hold.set()
        plane.close()
