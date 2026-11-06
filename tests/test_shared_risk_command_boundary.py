"""Policy publication ordered with real durable START/STOP admission."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from threading import Event
from contextlib import contextmanager

import pytest

from bcc.config import defaults
from bcc.shared_risk import DEFAULT_POLICY, load_policy, policy_path
from bcc.storage import BotBusy, Repository
from bcc.supervisor import Supervisor


class StoppedLifecycle:
    def status(self, folder):
        return 'Остановлен'


def setup(tmp_path):
    repo = Repository(tmp_path)
    supervisor = Supervisor(repo, lifecycle=StoppedLifecycle())
    cfg = defaults('VELES')
    cfg['name'] = 'Host risk policy boundary'
    folder = supervisor.create_bot(cfg)
    return repo, supervisor, folder


def test_queued_start_blocks_policy_even_before_worker_exists(tmp_path):
    repo, supervisor, folder = setup(tmp_path)
    supervisor.submit(folder, running=True)
    with pytest.raises(BotBusy, match='команда запуска'):
        supervisor.save_shared_risk_policy(dict(max_positions=2, max_committed_usdt=200))
    assert not policy_path(repo.root).exists()
    assert load_policy(repo.root) == DEFAULT_POLICY


def test_start_stop_round_trip_during_scan_invalidates_stopped_snapshot(tmp_path):
    repo, supervisor, folder = setup(tmp_path)
    def racing_status(path):
        supervisor.submit(folder, running=True)
        supervisor.submit(folder, running=False)
        return 'Остановлен'
    supervisor.lifecycle.status = racing_status
    with pytest.raises(BotBusy, match='изменилось'):
        supervisor.save_shared_risk_policy(dict(max_positions=2, max_committed_usdt=200))
    assert not policy_path(repo.root).exists()


def test_slow_os_scan_does_not_hold_stop_command_writer(tmp_path):
    repo, supervisor, folder = setup(tmp_path)
    scanning, release = Event(), Event()
    def delayed_status(path):
        scanning.set()
        assert release.wait(5)
        return 'Остановлен'
    supervisor.lifecycle.status = delayed_status
    with ThreadPoolExecutor(max_workers=2) as pool:
        saving = pool.submit(supervisor.save_shared_risk_policy,
                             dict(max_positions=2, max_committed_usdt=200))
        try:
            assert scanning.wait(5)
            stopped = pool.submit(supervisor.submit, folder, running=False)
            assert stopped.result(timeout=2).action == 'queued'
        finally:
            release.set()
        with pytest.raises(BotBusy, match='изменилось'):
            saving.result(timeout=5)
    assert not policy_path(repo.root).exists()


def test_start_admission_waits_for_atomic_policy_publication(tmp_path, monkeypatch):
    import bcc.shared_risk as risk
    repo, supervisor, folder = setup(tmp_path)
    publishing, release = Event(), Event()
    original = risk.atomic_json
    def delayed_write(path, policy):
        publishing.set()
        assert release.wait(5)
        original(path, policy)
    monkeypatch.setattr(risk, 'atomic_json', delayed_write)
    expected = dict(max_positions=2, max_committed_usdt=200)
    with ThreadPoolExecutor(max_workers=2) as pool:
        saving = pool.submit(supervisor.save_shared_risk_policy, expected)
        try:
            assert publishing.wait(5)
            attempted = Event()
            original_write = supervisor.coordinator._write
            @contextmanager
            def observed_write():
                attempted.set()
                with original_write() as database:
                    yield database
                assert load_policy(repo.root) == expected
            monkeypatch.setattr(supervisor.coordinator, '_write', observed_write)
            starting = pool.submit(supervisor.submit, folder, running=True)
            assert attempted.wait(2)
            with pytest.raises(FutureTimeout):
                starting.result(timeout=.1)
        finally:
            release.set()
        saving.result(timeout=5)
        assert starting.result(timeout=5).action == 'queued'
    assert load_policy(repo.root) == expected
    assert supervisor.coordinator.get_registration(supervisor._key(folder))['desired'] == 'RUNNING'


def test_stopped_policy_save_preserves_bot_configuration_and_ledger(tmp_path):
    from bcc.storage import Store
    repo, supervisor, folder = setup(tmp_path)
    cfg_before = (folder / 'config.json').read_bytes()
    state_before = Store(folder, 500).read()
    supervisor.save_shared_risk_policy(dict(max_positions=2, max_committed_usdt=200))
    assert (folder / 'config.json').read_bytes() == cfg_before
    assert Store(folder).read() == state_before
    Store(folder).audit()
