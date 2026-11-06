"""Cross-boundary service readiness invariants, not a unit-only health check."""
import json
from threading import Event

from bcc.runtime_service import serve


def test_reconciliation_failure_revokes_previously_published_readiness(tmp_path):
    stopped = Event()

    class FailingAfterReady:
        def inventory(self):
            pass

        def reconcile_batch(self, cursor, *, limit, sync_inventory):
            if cursor is None:
                return 'first', ['observed']
            stopped.set()
            raise OSError('simulated durable store failure')

    serve(tmp_path, supervisor=FailingAfterReady(), stop=stopped, interval=.001)
    # A past successful observation must not certify a currently failing controller.
    health = json.loads((tmp_path / '.runtime_health.json').read_text(encoding="utf-8")) if (tmp_path / '.runtime_health.json').exists() else None
    assert health is None, 'service must retire readiness when exiting'


def test_empty_registry_does_not_require_uninitialized_inventory_state(tmp_path):
    done = Event()

    class EmptyRegistry:
        def __init__(self):
            self.steps = 0
        def inventory(self):
            pass
        def reconcile_batch(self, cursor, *, limit, sync_inventory):
            self.steps += 1
            if self.steps == 2:
                done.set()
            return cursor, []

    serve(tmp_path, supervisor=EmptyRegistry(), stop=done, interval=.001)
