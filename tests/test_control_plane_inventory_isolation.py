"""Whole-service boundary: slow discovery cannot hold urgent stop hostage."""
from pathlib import Path
from threading import Event, Thread

from bcc.runtime_service import serve
from bcc.supervisor import Supervisor


class RepositoryStub:
    def __init__(self, root):
        self.root = root
        self.bots = root / 'MY_BOTS'
        self.bots.mkdir()
        self.folder = self.bots / 'one'
        self.folder.mkdir()

    def resolve(self, path):
        resolved = Path(path).resolve()
        if resolved != self.folder.resolve():
            raise ValueError('unexpected bot')
        return resolved


class LifecycleStub:
    def __init__(self):
        self.stopped = Event()
        self.state = 'Работает'

    def status(self, path):
        return self.state

    def stop(self, path, force=False):
        if not force:
            raise AssertionError('urgent command lost force flag')
        self.state = 'Остановлен'
        self.stopped.set()

    def active_snapshot(self, path):
        return None


def test_urgent_stop_completes_during_blocked_inventory(tmp_path):
    repo = RepositoryStub(tmp_path)
    life = LifecycleStub()
    sup = Supervisor(repo, lifecycle=life)
    sup.submit(repo.folder, running=False, force=True)
    entered = Event()
    release = Event()
    original = sup.inventory_slice

    def delayed_inventory(*, limit=8):
        entered.set()
        if not release.wait(5):
            raise TimeoutError('inventory held too long')
        return original(limit=limit)

    sup.inventory_slice = delayed_inventory
    stop = Event()
    errors = []

    def service():
        try:
            serve(tmp_path, stop=stop, supervisor=sup, interval=0.01,
                  inventory_interval=1, batch_size=2)
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=service)
    thread.start()
    try:
        assert entered.wait(2), 'background inventory was never entered'
        assert life.stopped.wait(2), 'urgent command was blocked by discovery'
        assert not release.is_set(), 'test did not exercise concurrent inventory'
    finally:
        release.set()
        stop.set()
        thread.join(timeout=4)
    assert not thread.is_alive(), 'supervisor did not terminate cleanly'
    assert not errors
    assert not sup.coordinator.get_registration('one')['force_stop'] or life.stopped.is_set()


def test_service_cannot_publish_readiness_before_initial_discovery(tmp_path):
    repo = RepositoryStub(tmp_path)
    life = LifecycleStub()
    sup = Supervisor(repo, lifecycle=life)
    entered, release, stop = Event(), Event(), Event()
    errors = []

    def blocked(*, limit=8):
        entered.set()
        if not release.wait(5):
            raise TimeoutError('unexpected stalled test')
        return (0, True)

    sup.inventory_slice = blocked

    def run():
        try:
            serve(tmp_path, stop=stop, supervisor=sup, interval=0.01,
                  inventory_interval=1, batch_size=2)
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=run)
    thread.start()
    try:
        assert entered.wait(2)
        assert not (tmp_path / '.runtime_health.json').exists(), 'unverified startup was published as ready'
    finally:
        release.set()
        stop.set()
        thread.join(timeout=4)
    assert not thread.is_alive()
    assert not errors


def test_service_shutdown_does_not_wait_for_unresponsive_discovery(tmp_path):
    repo = RepositoryStub(tmp_path)
    sup = Supervisor(repo, lifecycle=LifecycleStub())
    entered, release, stop = Event(), Event(), Event()
    errors = []

    def stuck_discovery(*, limit=8):
        entered.set()
        release.wait(5)
        return 0, True

    sup.inventory_slice = stuck_discovery

    def run():
        try:
            serve(tmp_path, stop=stop, supervisor=sup, interval=0.01, batch_size=2)
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=run)
    thread.start()
    try:
        assert entered.wait(2)
        stop.set()
        thread.join(timeout=1.5)
        assert not thread.is_alive(), 'service shutdown was held by filesystem discovery'
        assert not (tmp_path / '.runtime_health.json').exists()
    finally:
        release.set()
        thread.join(timeout=4)
    assert not errors
