"""Cross-plane acceptance: discovery faults revoke observed host readiness."""
from bcc.runtime_service import _serve_loop
from bcc.supervisor import ReconcileResult


def test_ready_revoked_by_failed_inventory_even_with_successful_workers(tmp_path, monkeypatch):
    from bcc import runtime_service
    states = []
    monkeypatch.setattr(runtime_service, 'publish', lambda root, **kw: states.append(kw['state']))

    class Registry:
        def next_keys(self, cursor, *, limit): return ['VELES/account']
        def urgent_force_stops(self, cursor, *, limit): return []
        def has_registrations(self): return True

    class Supervisor:
        coordinator = Registry()

    class Executor:
        def schedule(self, keys, *, urgent=False): return len(keys)
        def collect(self):
            return [ReconcileResult('VELES/account', 'RUNNING', 'Работает', 'none')]

    class Inventory:
        def __init__(self): self.n = 0
        def step(self):
            self.n += 1
            if self.n == 1: return True, True
            raise OSError('filesystem unavailable')

    # The test must advance the scan clock deterministically. On Windows a
    # second loop can execute in <10 microseconds and correctly defer the
    # next scan, leaving the injected failure unobserved.
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(runtime_service.time, 'monotonic', lambda: next(ticks))

    class Stop:
        def __init__(self): self.n = 0
        def is_set(self): return self.n >= 2
        def wait(self, duration): self.n += 1

    _serve_loop(tmp_path, Supervisor(), Stop(), 0.001, 1, 0.00001,
                None, 'run', Executor(), Inventory())
    assert states[0] == 'ready'
    assert states[-1] == 'degraded'
