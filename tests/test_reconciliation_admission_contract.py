"""Control-plane backlog invariants across the executor and registry cursor."""
from threading import Event
from bcc.runtime_service import ReconciliationPlane, _serve_loop


class SlowSupervisor:
    def __init__(self):
        self.entered = Event()
        self.release = Event()
        self.calls = []

    def reconcile_key(self, key):
        self.calls.append(key)
        self.entered.set()
        self.release.wait(3)
        return key


def test_refused_work_does_not_count_as_admitted():
    owner = SlowSupervisor()
    plane = ReconciliationPlane(owner, workers=1)
    try:
        assert plane.schedule(['a']) == 1
        assert owner.entered.wait(2)
        assert plane.schedule(['b', 'c']) == 1
        assert plane.fleet.snapshot()['waiting'] == 1
    finally:
        owner.release.set()
        plane.close()


def test_priority_admission_eviction_remains_bounded():
    owner = SlowSupervisor()
    plane = ReconciliationPlane(owner, workers=1)
    try:
        assert plane.schedule(['a']) == 1
        assert owner.entered.wait(2)
        assert plane.schedule(['b']) == 1
        assert plane.schedule(['urgent'], urgent=True) == 1
        state = plane.fleet.snapshot()
        assert state['waiting'] == 1
        assert state['urgent_waiting'] == 1
    finally:
        owner.release.set()
        plane.close()


def test_cursor_progress_is_limited_to_admitted_prefix():
    from bcc.runtime_service import advance_cursor
    assert advance_cursor(None, ['a', 'b', 'c'], 1) == 'a'
    assert advance_cursor('a', ['b', 'c'], 0) == 'a'
    assert advance_cursor('a', ['b', 'c'], 2) == 'c'
    assert advance_cursor('c', [], 0) is None
