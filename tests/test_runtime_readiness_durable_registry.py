"""Do not mistake a drained ephemeral queue for a healthy runtime."""
from threading import Event, Thread
import time
from bcc.config import defaults
from bcc.coordination import Coordinator
from bcc.runtime_health import observe
from bcc.runtime_service import serve
from bcc.storage import Repository
from bcc.supervisor import Supervisor, ReconcileResult


def test_durable_registry_presence_not_bounded_queue(tmp_path):
    control = Coordinator(tmp_path / 'coordination.sqlite')
    control.initialize()
    assert control.has_registrations() is False
    control.register('VELES/one')
    assert control.has_registrations() is True
    control.unregister('VELES/one')
    assert control.has_registrations() is False


def test_all_reconciliations_failing_cannot_certify_service_ready(tmp_path):
    repo = Repository(tmp_path / 'runtime')
    cfg = defaults(); cfg['name'] = 'Readiness evidence'
    folder = repo.create(cfg)
    service = Supervisor(repo)
    service.register(folder)
    def fail_every_time(key):
        return ReconcileResult(key, 'RUNNING', 'Ошибка', 'error', 'simulated failed reconcile')
    service.reconcile_key = fail_every_time
    stop = Event()
    task = Thread(target=serve, args=(repo.root,), kwargs=dict(
        supervisor=service, stop=stop, interval=.025, inventory_interval=.1,
        batch_size=1), daemon=True)
    task.start()
    try:
        # After inventory and multiple failed reconciliations, a transient
        # executor will be empty between tasks, but the durable bot remains.
        time.sleep(.35)
        assert observe(repo.root) is None
        assert service.coordinator.has_registrations()
    finally:
        stop.set()
        task.join(timeout=5)
    assert not task.is_alive()
