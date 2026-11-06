"""Boundary tests: continuous host supervision must survive unrelated faults."""
from threading import Event
from bcc.runtime_service import serve


def test_inventory_and_reconcile_failures_do_not_drop_host_ownership(tmp_path):
    done = Event()
    class FailingThenHealthy:
        def __init__(self):
            self.inventory_calls = 0
            self.reconcile_calls = 0
        def inventory(self):
            self.inventory_calls += 1
            if self.inventory_calls == 1:
                raise OSError('temporary inventory failure')
        def reconcile_batch(self, cursor, *, limit, sync_inventory):
            assert sync_inventory is False
            self.reconcile_calls += 1
            if self.reconcile_calls == 1:
                raise OSError('database temporarily locked')
            if self.reconcile_calls >= 3:
                done.set()
            return str(self.reconcile_calls), ['ok']
    host = FailingThenHealthy()
    observed = []
    serve(tmp_path, stop=done, supervisor=host, interval=0.001,
          inventory_interval=0.001, on_result=observed.append)
    assert host.inventory_calls >= 2
    assert host.reconcile_calls == 3
    assert observed == ['ok', 'ok']


def test_observer_failure_does_not_stop_bot_reconciliation(tmp_path):
    done = Event()
    class Host:
        def __init__(self): self.n = 0
        def inventory(self): pass
        def reconcile_batch(self, cursor, *, limit, sync_inventory):
            self.n += 1
            if self.n == 4: done.set()
            return self.n, [self.n]
    host = Host()
    def broken_observer(_):
        raise RuntimeError('ui notification failed')
    serve(tmp_path, stop=done, supervisor=host, interval=0.001,
          on_result=broken_observer)
    assert host.n == 4
