"""The Modern Qt controller and worker must agree on a single launch token."""
import json
from copy import deepcopy
import subprocess
import sys
import threading
import time

import pytest

from bcc.config import defaults
from bcc.launch_claim import (check_launch_lease, claim_path, read_claim,
                              verify_worker_token, STARTUP_GRACE)
from bcc.lifecycle import Lifecycle
from bcc.storage import BotBusy, BotLock, Repository, Store, atomic_json
from bcc.transfer import export_bots
from test_process import RUNNER, setup_bot, wait_for


def fresh(tmp_path):
    repo = Repository(tmp_path / 'app')
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Two-window launch test'
    folder = repo.create(cfg)
    return repo, folder


def test_two_independent_dashboards_only_spawn_one(tmp_path, monkeypatch):
    repo, folder = fresh(tmp_path)
    first, second = Lifecycle(repo), Lifecycle(repo)
    barrier = threading.Barrier(2)
    starts = []
    errors = []
    spawned = threading.Event()

    class FakeProcess:
        # Never reuse an arbitrary real Windows runner PID. The fake Popen
        # does not own that OS process and must not impersonate its birth ID.
        pid = 2_000_000_000
        def poll(self):
            return None

    def fake_popen(*args, **kwargs):
        starts.append(args)
        spawned.set()
        time.sleep(.08)  # widen the cross-controller launch gap
        return FakeProcess()

    monkeypatch.setattr('bcc.lifecycle.subprocess.Popen', fake_popen)

    def attempt(life):
        barrier.wait()
        try:
            life.start(folder)
        except BotBusy as exc:
            errors.append(str(exc))

    a = threading.Thread(target=attempt, args=(first,))
    b = threading.Thread(target=attempt, args=(second,))
    a.start(); b.start(); a.join(timeout=10); b.join(timeout=10)
    assert not a.is_alive() and not b.is_alive()
    assert len(starts) == 1
    assert len(errors) == 1
    claim = read_claim(folder)
    assert claim and claim['token'] in (first.tokens.get(str(folder)), second.tokens.get(str(folder)))
    with pytest.raises(BotBusy):
        with BotLock(folder):
            check_launch_lease(folder)
    with pytest.raises(BotBusy):
        repo.copy(folder, history=False)
    with pytest.raises(BotBusy):
        repo.delete(folder)
    with pytest.raises(BotBusy):
        export_bots(repo, [folder], tmp_path / 'export.zip')
    with pytest.raises(BotBusy):
        cfg, digest = repo.load(folder)
        repo.save(folder, cfg, digest)
    with pytest.raises(BotBusy):
        verify_worker_token(folder, 'different-token')


def test_failed_spawn_clears_claim_for_retry(tmp_path, monkeypatch):
    repo, folder = fresh(tmp_path)
    def fail(*args, **kwargs):
        raise OSError('startup denied')
    monkeypatch.setattr('bcc.lifecycle.subprocess.Popen', fail)
    with pytest.raises(OSError, match='startup denied'):
        Lifecycle(repo).start(folder)
    assert not claim_path(folder).exists()
    assert Lifecycle(repo).status(folder) == 'Остановлен'


def test_expired_claim_can_only_be_recovered_under_exclusive_lock(tmp_path):
    repo, folder = fresh(tmp_path)
    atomic_json(claim_path(folder), {
        'token': 'a' * 32, 'at': time.time() - STARTUP_GRACE - 2,
        'pid': 2_000_000_000, 'created': 1.0,
    })
    assert Lifecycle(repo).status(folder) == 'Остановлен'
    with BotLock(folder):
        check_launch_lease(folder)
    assert not claim_path(folder).exists()
    assert repo.load(folder)[0]['strategy'] == 'POLYMARKET'


def test_corrupted_claim_fails_closed(tmp_path):
    repo, folder = fresh(tmp_path)
    claim_path(folder).write_text('{bad json', encoding='utf-8')
    with pytest.raises(BotBusy):
        Lifecycle(repo).start(folder)
    with pytest.raises(BotBusy):
        with BotLock(folder):
            check_launch_lease(folder)


def test_real_replay_worker_handshake_then_stop_and_restart(tmp_path, monkeypatch):
    repo = Repository(tmp_path / 'app')
    life = Lifecycle(repo)
    runner = tmp_path / 'replay_worker.py'
    runner.write_text(RUNNER, encoding='utf-8')
    folder, _ = setup_bot(repo, runner)
    real_popen = subprocess.Popen

    def replay_launch(command, **kwargs):
        cmd = [sys.executable, str(runner), *command[3:]]
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr('bcc.lifecycle.subprocess.Popen', replay_launch)
    proc = None
    try:
        for cycle in range(2):
            life = Lifecycle(repo)  # simulate a newly opened Dashboard
            life.start(folder)
            proc = life.children[str(folder)]
            wait_for(lambda: life.active_snapshot(folder), seconds=15)
            assert life.status(folder) == 'Работает'
            assert not claim_path(folder).exists()
            with pytest.raises(BotBusy):
                Lifecycle(repo).start(folder)
            wait_for(lambda: Store(folder).read()['positions'], seconds=15)
            before = Store(folder).read()['positions']
            life.stop(folder)
            proc.wait(timeout=10)
            Store(folder).audit()
            after = Store(folder).read()['positions']
            # A running worker may refresh market evidence between the
            # pre-stop read and the STOP acknowledgement. The observation
            # timestamp is not a trade or a financial state transition.
            # Preserve strict comparison of every other position field.
            assert before.keys() == after.keys()
            for key in before:
                old, new = deepcopy(before[key]), deepcopy(after[key])
                old_mark = old.get('margin_evidence', {}).pop('mark_at', None)
                new_mark = new.get('margin_evidence', {}).pop('mark_at', None)
                assert new_mark is None or old_mark is None or new_mark >= old_mark
                assert new == old
            assert life.status(folder) == 'Остановлен'
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill(); proc.wait(timeout=10)


def test_worker_constructor_error_is_reported_and_retry_is_safe(tmp_path, monkeypatch):
    repo = Repository(tmp_path / 'app')
    folder, _ = setup_bot(repo, tmp_path / 'ignored.py', 'Startup fail')
    fail_runner = tmp_path / 'fail_worker.py'
    fail_runner.write_text('''
import bcc.worker as worker
class BrokenPublicData:
 def __init__(self):
  raise RuntimeError('replay source intentionally unavailable')
worker.PublicData = BrokenPublicData
worker.main()
''', encoding='utf-8')
    good_runner = tmp_path / 'replay_worker.py'
    good_runner.write_text(RUNNER, encoding='utf-8')
    real_popen = subprocess.Popen
    current = {'runner': fail_runner}

    def replay(command, **kwargs):
        return real_popen([sys.executable, str(current['runner']), *command[3:]], **kwargs)

    monkeypatch.setattr('bcc.lifecycle.subprocess.Popen', replay)
    failed = Lifecycle(repo)
    failed.start(folder)
    proc = failed.children[str(folder)]
    proc.wait(timeout=15)
    assert proc.returncode != 0
    assert failed.status(folder) == 'Остановлен'
    assert not claim_path(folder).exists()
    snapshot = json.loads((folder / 'process.json').read_text(encoding="utf-8"))
    assert snapshot['status'] == 'ERROR'
    assert 'replay source intentionally unavailable' in failed.last_error(folder)
    Store(folder).audit()
    initial = Store(folder).read()

    current['runner'] = good_runner
    again = Lifecycle(repo)
    again.start(folder)
    recovered = again.children[str(folder)]
    try:
        wait_for(lambda: again.active_snapshot(folder), seconds=15)
        wait_for(lambda: Store(folder).read()['positions'], seconds=15)
        assert Store(folder).read()['initial'] == initial['initial']
        assert not claim_path(folder).exists()
        again.stop(folder)
        recovered.wait(timeout=15)
        Store(folder).audit()
    finally:
        if recovered.poll() is None:
            recovered.kill()
            recovered.wait(timeout=15)


def test_recent_proven_dead_launcher_reclaims_immediately(tmp_path):
    """Crash after spawn registration must not impose a two-minute restart blackout."""
    repo, folder = fresh(tmp_path)
    atomic_json(claim_path(folder), {
        'token': 'a' * 32, 'at': time.time(),
        'pid': 2_000_000_000, 'created': 1.0,
    })
    assert Lifecycle(repo).status(folder) == 'Остановлен'
    with BotLock(folder):
        check_launch_lease(folder)
    assert not claim_path(folder).exists()


def test_unregistered_launch_keeps_grace_fence(tmp_path):
    """Reclaim dead launches without allowing two simultaneous in-flight spawns."""
    repo, folder = fresh(tmp_path)
    atomic_json(claim_path(folder), {
        'token': 'b' * 32, 'at': time.time(),
    })
    assert Lifecycle(repo).status(folder) == 'Запуск'
    with BotLock(folder):
        with pytest.raises(BotBusy):
            check_launch_lease(folder)


def test_restricted_launcher_is_not_reclaimed(tmp_path, monkeypatch):
    """Lack of OS permission must never be mistaken for dead-worker proof."""
    from bcc import process_os as psutil
    from bcc.launch_claim import claim_pending
    repo, folder = fresh(tmp_path)
    claim = {'token': 'c' * 32, 'at': time.time(), 'pid': 918273, 'created': 123.0}
    def denied(_pid):
        raise psutil.AccessDenied(_pid)
    monkeypatch.setattr('bcc.launch_claim.psutil.Process', denied)
    assert claim_pending(claim)
    atomic_json(claim_path(folder), claim)
    with BotLock(folder):
        with pytest.raises(BotBusy):
            check_launch_lease(folder)


def test_native_lifecycle_launch_uses_worker_entrypoint_without_extra_args(tmp_path, monkeypatch):
    """Check real Lifecycle.start argv, not just the substituted replay script."""
    repo, folder = fresh(tmp_path)
    launches = []

    class FakeProcess:
        pid = 987654
        def poll(self):
            return None

    def fake_popen(args, **kwargs):
        launches.append(list(args))
        return FakeProcess()

    monkeypatch.setattr('bcc.lifecycle.subprocess.Popen', fake_popen)
    life = Lifecycle(repo)
    life.start(folder)
    assert len(launches) == 1
    argv = launches[0]
    assert argv[:3] == [sys.executable, '-m', 'bcc.worker']
    assert argv[3:] == ['--folder', str(folder), '--token', life.tokens[str(folder)]]
    assert argv.count('bcc.worker') == 1
