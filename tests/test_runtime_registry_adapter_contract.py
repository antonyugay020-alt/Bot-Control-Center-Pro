"""Runtime readiness and observer delivery with partial registry adapters."""
from threading import Event

from bcc.runtime_service import _serve_loop
from bcc.supervisor import ReconcileResult


def test_partial_registry_adapter_does_not_break_reconciliation_or_claim_empty(tmp_path, monkeypatch):
    from bcc import runtime_service
    states, delivered = [], []
    monkeypatch.setattr(runtime_service, 'publish', lambda root, **kw: states.append(kw['state']))

    class Registry:
        def next_keys(self, cursor, *, limit): return ['one', 'two']
        def urgent_force_stops(self, cursor, *, limit): return []
        # No has_registrations(): absence is not proof of an empty registry.

    class Host:
        coordinator = Registry()
        def inventory_slice(self, *, limit): return 0, True

    class Plane:
        def schedule(self, keys, *, urgent=False): return len(keys)
        def collect(self): return [ReconcileResult('one', 'RUNNING', 'Ошибка', 'error')]

    class Stop:
        def __init__(self): self.rounds = 0
        def is_set(self): return self.rounds >= 3
        def wait(self, duration): self.rounds += 1

    _serve_loop(tmp_path, Host(), Stop(), .001, 2, 5,
                delivered.append, 'test', Plane())
    assert len(delivered) == 3
    assert all(item.action == 'error' for item in delivered)
    assert states == ['degraded'] * 3
