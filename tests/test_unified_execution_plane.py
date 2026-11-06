"""End-to-end control-plane contracts, including cross-bot isolation."""
from collections import Counter
from threading import Event, Lock, Thread
import time

from bcc.runtime_service import ReconciliationPlane, serve
from bcc.supervisor import Supervisor


def test_bounded_parallel_reconciliation_preserves_one_owner_and_isolation():
    lock = Lock()
    entered = Event()
    release = Event()
    running = Counter()
    peak = Counter()
    results = []

    class Backend:
        def reconcile_key(self, key):
            with lock:
                running[key] += 1
                peak[key] = max(peak[key], running[key])
                entered.set()
            release.wait(2)
            with lock:
                running[key] -= 1
            if key == 'bad':
                raise ValueError('broken bot')
            return key

    plane = ReconciliationPlane(Backend(), workers=3)
    try:
        plane.schedule(['a', 'b', 'bad'])
        assert entered.wait(2)
        for _ in range(15):
            plane.schedule(['a', 'b', 'bad'])
        assert plane.fleet.snapshot()['active'] <= 3
        assert plane.fleet.snapshot()['waiting'] <= 3
        release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            results.extend(plane.collect())
            if plane.fleet.snapshot()['active'] == 0 and plane.fleet.snapshot()['waiting'] == 0:
                break
            time.sleep(.01)
        assert all(count <= 1 for count in peak.values())
        assert 'a' in results and 'b' in results
    finally:
        release.set()
        plane.close()


def test_production_service_uses_execution_plane_without_gui(tmp_path):
    stopped = Event()
    callbacks = []
    class Registry:
        def next_keys(self, cursor, *, limit):
            return ['bot-a', 'bot-b'] if cursor is None else []
        def urgent_force_stops(self, cursor, *, limit):
            return ['bot-a'] if cursor is None else []
    class Host(Supervisor):
        def __init__(self):
            self.coordinator = Registry()
            self.calls = Counter()
        def inventory_slice(self, *, limit):
            return 0, True
        def reconcile_key(self, key):
            self.calls[key] += 1
            return key
    host = Host()
    def observe(value):
        callbacks.append(value)
        if set(callbacks) == {'bot-a', 'bot-b'}:
            stopped.set()
    thread = Thread(target=serve, args=(tmp_path,), kwargs={
        'stop': stopped, 'interval': .005, 'batch_size': 2,
        'supervisor': host, 'on_result': observe}, daemon=True)
    thread.start()
    thread.join(3)
    stopped.set()
    thread.join(2)
    assert not thread.is_alive()
    assert 'bot-a' in callbacks and 'bot-b' in callbacks
    assert not (tmp_path / '.runtime_health.json').exists()


def test_durable_ui_to_service_to_lifecycle_across_service_restart(tmp_path):
    from test_supervisor_integration import Repo, Life
    repo, life = Repo(tmp_path), Life()
    ui = Supervisor(repo, lifecycle=life)
    assert ui.request_start(repo.folder).action == 'queued'
    assert life.calls == []

    def run_until(action):
        ended = Event()
        seen = []
        runtime = Supervisor(repo, lifecycle=life)
        def record(result):
            seen.append(result.action)
            if result.action == action:
                ended.set()
        task = Thread(target=serve, args=(tmp_path,), kwargs={
            'stop': ended, 'interval': .005, 'batch_size': 2,
            'inventory_interval': .01, 'supervisor': runtime,
            'on_result': record}, daemon=True)
        task.start()
        assert ended.wait(3), seen
        task.join(3)
        assert not task.is_alive()
        assert not (tmp_path / '.runtime_health.json').exists()

    run_until('start')
    assert life.calls == ['start']
    assert Supervisor(repo, lifecycle=life).request_stop(repo.folder).action == 'queued'
    assert life.calls == ['start']
    run_until('stop')
    assert life.calls == ['start', 'stop']
    assert Supervisor(repo, lifecycle=life).coordinator.get_registration('RSI/one')['desired'] == 'STOPPED'


def test_previously_registered_fleet_never_becomes_unbounded_queue():
    hold = Event()
    class Host:
        def reconcile_key(self, key):
            hold.wait(2)
            return key
    plane = ReconciliationPlane(Host(), workers=2)
    try:
        # A long-lived service may already know many registered identities.
        # Being known must not bypass bounded admission on later sweeps.
        for i in range(80):
            key = f'bot-{i}'
            plane.fleet.register(key, lambda k=key: plane.supervisor.reconcile_key(k))
        plane.schedule([f'bot-{i}' for i in range(80)])
        plane.schedule([f'bot-{i}' for i in range(80)], urgent=True)
        snap = plane.fleet.snapshot()
        assert snap['active'] + snap['waiting'] <= 4
    finally:
        hold.set()
        plane.close()
