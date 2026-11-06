"""System-wide correctness of health observations and executor failures."""
import time
from threading import Event

from bcc.runtime_service import ReconciliationPlane, _serve_loop
from bcc.supervisor import ReconcileResult


def test_failed_executor_task_is_visible_not_silently_dropped():
    class Host:
        def reconcile_key(self, key):
            raise RuntimeError('deliberate transition failure')
    plane = ReconciliationPlane(Host(), workers=1)
    try:
        assert plane.schedule(['bot']) == 1
        deadline = time.monotonic() + 2
        seen = []
        while not seen and time.monotonic() < deadline:
            seen.extend(plane.collect())
            time.sleep(.001)
        assert len(seen) == 1
        assert isinstance(seen[0], ReconcileResult)
        assert seen[0].key == 'bot'
        assert seen[0].action == 'error'
        assert 'deliberate transition failure' in seen[0].error
    finally:
        plane.close()


def test_host_health_revoked_after_reconciliation_error(tmp_path, monkeypatch):
    states = []
    from bcc import runtime_service
    monkeypatch.setattr(runtime_service, 'publish', lambda root, **kwargs: states.append(kwargs['state']))

    class Coordinator:
        def next_keys(self, cursor, *, limit):
            return ['bot']
        def urgent_force_stops(self, cursor, *, limit):
            return []
        def has_registrations(self):
            return True

    class Owner:
        coordinator = Coordinator()
        # The readiness contract now requires a successful registry scan.
        # A fake supervisor without inventory is an actual degraded host, so
        # give this executor-error test the explicitly healthy prerequisite.
        def inventory_slice(self, *, limit):
            return (0, True)

    class Plane:
        def __init__(self):
            self.calls = 0
        def schedule(self, keys, *, urgent=False):
            return len(keys)
        def collect(self):
            self.calls += 1
            if self.calls == 1:
                return [ReconcileResult('bot','RUNNING','Работает','none')]
            return [ReconcileResult('bot','RUNNING','Ошибка','error','broken')]

    class StopAfter:
        def __init__(self):
            self.calls = 0
        def is_set(self):
            return self.calls >= 2
        def wait(self, interval):
            self.calls += 1

    _serve_loop(tmp_path, Owner(), StopAfter(), .001, 1, 100,
                None, 'run', Plane())
    assert states == ['ready', 'degraded']
