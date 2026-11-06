"""Interactive QA owns private services while ordinary Qt windows do not."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bcc import process_os as psutil
from bcc.config import defaults
from bcc.hub_supervisor import ensure_hub, _matches
from bcc.lifecycle import Lifecycle
from bcc.runtime_health import observe
from bcc.runtime_service import HostLease
from bcc.storage import Repository, Store, atomic_json
from tools.acceptance_session import AcceptanceSession
from tools.process_guard import run_logged_command
from test_process import RUNNER, setup_bot, launch, wait_for


def test_existing_workspace_is_never_adopted_or_modified(tmp_path):
    repo = Repository(tmp_path / 'working')
    cfg = defaults('VELES')
    cfg['name'] = 'Existing account'
    bot = repo.create(cfg)
    Store(bot, cfg['capital'])
    originals = {p.relative_to(repo.root): p.read_bytes()
                 for p in repo.root.rglob('*') if p.is_file()}
    with pytest.raises(FileExistsError):
        with AcceptanceSession(repo.root, tmp_path / 'output'):
            pytest.fail('An existing account root was adopted')
    assert {p.relative_to(repo.root): p.read_bytes()
            for p in repo.root.rglob('*') if p.is_file()} == originals


def test_ui_bootstrap_reuses_session_service_and_guarded_ui_exit_succeeds(tmp_path):
    root, output = tmp_path / 'new', tmp_path / 'evidence'
    with AcceptanceSession(root, output) as session:
        first = observe(root)
        assert first
        code = (
            'from bcc.bootstrap import ensure_runtime; '
            'import sys; '
            'forbidden=lambda *_a,**_k: (_ for _ in ()).throw(AssertionError("duplicate guardian")); '
            'result=ensure_runtime(sys.argv[1],spawn=forbidden); '
            'assert result["run_id"]==sys.argv[2]'
        )
        with (output / 'ui_bootstrap.log').open('w', encoding="utf-8") as stream:
            assert run_logged_command([sys.executable, '-c', code, str(root), first['run_id']],
                                      cwd=Path(__file__).resolve().parents[1],
                                      stdout=stream, timeout=15) == 0
        assert observe(root)['run_id'] == first['run_id']
    assert observe(root) is None
    assert not session.thread.is_alive()
    with HostLease(root / '.runtime_guard.lock'), HostLease(root / '.runtime_supervisor.lock'):
        pass
    assert json.loads((output / 'session.json').read_text(encoding="utf-8"))['status'] == 'STOPPED_STATE_PRESERVED'


def stop_child(life, folder, process):
    if process.poll() is None:
        life.stop(folder, force=True)
    process.wait(timeout=10)


def test_closing_acceptance_preserves_paper_and_leaves_neighbor_running(tmp_path):
    runner = tmp_path / 'controlled_worker.py'
    runner.write_text(RUNNER, encoding='utf-8')
    neighbor_repo = Repository(tmp_path / 'working_bots')
    neighbor, _ = setup_bot(neighbor_repo, runner, 'Working neighbor')
    neighbor_life = Lifecycle(neighbor_repo)
    outside = launch(neighbor_life, neighbor, runner)
    checked = None
    try:
        wait_for(lambda: Store(neighbor).read()['positions'], seconds=20)
        original_neighbor_history = Store(neighbor).history()
        with AcceptanceSession(tmp_path / 'private_qa', tmp_path / 'evidence') as session:
            folder, _ = setup_bot(session.owner.repo, runner, 'Checked PAPER account')
            checked = launch(session.owner.lifecycle, folder, runner)
            wait_for(lambda: Store(folder).read()['positions'], seconds=20)
            before, history = Store(folder).read(), Store(folder).history()
        assert checked.wait(timeout=10) == 0
        assert outside.poll() is None
        assert Store(neighbor).history() == original_neighbor_history
        after = Store(folder).read()
        assert after['cash'] == before['cash']
        assert Store(folder).history() == history
        assert set(after['positions']) == set(before['positions'])
        for key, position in before['positions'].items():
            for field in ('position_id', 'qty', 'average', 'margin', 'fees', 'settings'):
                assert after['positions'][key][field] == position[field]
        Store(folder).audit()
        assert session.owner.coordinator.get_registration(session.owner._key(folder))['desired'] == 'STOPPED'
        assert session.owner.lifecycle.status(folder) == 'Остановлен'
    finally:
        if checked is not None and checked.poll() is None:
            stop_child(session.owner.lifecycle, folder, checked)
        stop_child(neighbor_life, neighbor, outside)


def test_session_retires_only_its_private_market_hub(tmp_path):
    neighbor = tmp_path / 'outside'
    neighbor.mkdir()
    ensure_hub(neighbor)
    record = json.loads((neighbor / 'market_hub_process.json').read_text(encoding="utf-8"))
    try:
        with AcceptanceSession(tmp_path / 'new', tmp_path / 'evidence') as session:
            ensure_hub(session.root)
            own_record = json.loads((session.root / 'market_hub_process.json').read_text(encoding="utf-8"))
            assert _matches(own_record, session.root / 'market_cache.sqlite')
        assert not _matches(own_record, session.root / 'market_cache.sqlite')
        assert _matches(record, neighbor / 'market_cache.sqlite')
    finally:
        p = psutil.Process(record['pid'])
        p.terminate()
        _, alive = psutil.wait_procs([p], timeout=8)
        for child in alive:
            child.kill()
        psutil.wait_procs(alive, timeout=5)


def test_body_failure_cannot_leave_service_or_become_manual_pass(tmp_path):
    with pytest.raises(ValueError, match='UI failure'):
        with AcceptanceSession(tmp_path / 'new', tmp_path / 'evidence') as session:
            raise ValueError('UI failure')
    assert observe(session.root) is None
    assert not session.thread.is_alive()
    report = json.loads((session.output / 'session.json').read_text(encoding="utf-8"))
    assert report['status'] == 'FAILED'
    assert report['manual_acceptance'] == 'NOT_CONFIRMED'
    assert report['release_ready'] is False


def test_queued_start_is_revoked_even_after_runtime_exit(tmp_path):
    with AcceptanceSession(tmp_path / 'new', tmp_path / 'evidence') as session:
        session.stop.set()
        session.thread.join(timeout=15)
        assert not session.thread.is_alive()
        cfg = defaults('VELES')
        cfg['name'] = 'Queued, never executed'
        folder = session.owner.create_bot(cfg)
        session.owner.request_start(folder)
        assert not (folder / 'paper.sqlite').exists()
    row = session.owner.coordinator.get_registration(session.owner._key(folder))
    assert row['desired'] == 'STOPPED'
    assert not (folder / 'paper.sqlite').exists()


def test_interrupted_retirement_is_not_resurrected_by_session_cleanup(tmp_path):
    with AcceptanceSession(tmp_path / 'new', tmp_path / 'evidence') as session:
        cfg = defaults('VELES')
        cfg['name'] = 'Retiring'
        folder = session.owner.create_bot(cfg)
        Store(folder, cfg['capital'])
        before = (folder / 'paper.sqlite').read_bytes()
        key = session.owner._key(folder)
        session.owner.coordinator.begin_retirement(key)
        (folder / '.deleting').write_text('Interrupted deletion', encoding="utf-8")
    assert (folder / 'paper.sqlite').read_bytes() == before
    row = session.owner.coordinator.get_registration(key)
    assert row['desired'] == 'STOPPED' and row['retiring']


def test_replaced_scope_identity_blocks_account_operations(tmp_path):
    with pytest.raises(RuntimeError, match='identity changed'):
        with AcceptanceSession(tmp_path / 'new', tmp_path / 'evidence') as session:
            atomic_json(session.root / 'acceptance-session.json',
                        dict(session_id='another session', data_root=str(session.root)))
    assert observe(session.root) is None
    assert session.report['status'] == 'FAILED'


def test_forced_cleanup_is_recorded_as_failed_acceptance(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match='forced cleanup'):
        with AcceptanceSession(tmp_path / 'new', tmp_path / 'evidence') as session:
            cfg = defaults('VELES')
            cfg['name'] = 'Slow stop'
            folder = session.owner.create_bot(cfg)
            forced = []
            submit = session.owner.coordinator.submit_desired
            def request(key, desired, **options):
                result = submit(key, desired, **options)
                if options.get('force'):
                    forced.append(key)
                return result
            monkeypatch.setattr(session.owner.coordinator, 'submit_desired', request)
            monkeypatch.setattr(session.owner.lifecycle, 'process',
                                lambda _folder: None if forced else object())
            session.timeout = .1
    assert forced == [session.owner._key(folder)]
    assert session.report['status'] == 'FAILED'
    assert session.report['forced_cleanup'] is True
    assert observe(session.root) is None


@pytest.mark.parametrize('automated_only,reopen', [(False, False), (True, False), (False, True)])
def test_windows_checker_owns_session_and_collects_only_after_stop(tmp_path, monkeypatch, automated_only, reopen):
    from tools import windows_check, acceptance_session
    monkeypatch.setattr(windows_check, 'ROOT', tmp_path)
    monkeypatch.setattr(windows_check, 'os', SimpleNamespace(name='nt', environ=dict(os.environ)))
    monkeypatch.setattr(windows_check, 'verify_source_snapshot', lambda: dict(status='PASS'))
    sessions, steps = [], []
    decisions = iter([True, False] if reopen else [False])
    monkeypatch.setattr(windows_check, 'manual_reopen_requested', lambda: next(decisions))
    def scope(root, output, **options):
        assert not automated_only
        session = AcceptanceSession(root, output, python=sys.executable)
        sessions.append(session)
        return session
    monkeypatch.setattr(acceptance_session, 'AcceptanceSession', scope)
    def step(name, command, output, **options):
        steps.append(name)
        if name.startswith('application'):
            assert observe(sessions[-1].root)
            assert command[-1] == str(sessions[-1].root)
        elif name == 'collect_bots':
            assert not sessions[-1].thread.is_alive()
            assert sessions[-1].report['status'] == 'STOPPED_STATE_PRESERVED'
        return dict(step=name, exit_code=0)
    monkeypatch.setattr(windows_check, 'run_step', step)
    assert windows_check.main(['--automated-only'] if automated_only else []) == 0
    assert len(sessions) == (0 if automated_only else 1)
    assert sum(s.startswith('application') for s in steps) == (0 if automated_only else 2 if reopen else 1)
    summary_path = next((tmp_path / 'verification_reports').glob('*/check_summary.json'))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary['status'] == ('AUTOMATED_PASS_MANUAL_NOT_RUN' if automated_only else 'AUTOMATED_PASS_MANUAL_PENDING')
    assert summary['release_ready'] is False
