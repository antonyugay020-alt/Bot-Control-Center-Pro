"""The unattended live smoke must own its account and fail closed."""
import json
from pathlib import Path

import pytest

from tools import auto_live_paper


def test_live_smoke_config_is_public_paper_with_finite_test_budget():
    cfg = auto_live_paper.config()
    assert cfg['mode'] == 'PAPER' and cfg['exchange'] == 'BYBIT_LINEAR'
    assert cfg['symbols'] == ['BTCUSDT'] and cfg['capital'] == 500
    assert cfg['settings']['orders']['count'] == 1
    assert cfg['sections'][0]['entry']['conditions'][0]['indicator'] == 'PRICE'


@pytest.mark.parametrize('fresh', [True, False])
def test_owned_paper_cycle_requires_entry_exit_ledger_freshness_and_cleanup(tmp_path, monkeypatch, fresh):
    calls = []
    class Owner:
        draining = False
        closed = False
        def create_bot(self, cfg):
            assert cfg['mode'] == 'PAPER'
            calls.append('create')
            folder = tmp_path / 'owned_bot'
            folder.mkdir()
            return folder
        def request_start(self, folder):
            calls.append('start')
            (folder / 'paper.sqlite').write_bytes(b'test fixture')
            (folder / 'paper-account.json').write_text('{}', encoding='utf8')
        def submit_drain(self, folder, *, draining):
            assert draining
            self.draining = True
            calls.append('drain')
        def submit_manual_close(self, folder):
            assert self.draining
            self.closed = True
            calls.append('close')
    owner = Owner()
    class Session:
        def __init__(self, root, output, **kwargs):
            self.root, self.output, self.owner = Path(root), Path(output), owner
        def __enter__(self):
            self.root.mkdir(parents=True)
            self.output.mkdir(parents=True)
            return self
        def __exit__(self, *args):
            (self.output / 'session.json').write_text(json.dumps({
                'status': 'STOPPED_STATE_PRESERVED', 'services_retired': True}), encoding='utf8')
    class FakeStore:
        def __init__(self, folder):
            self.path = folder / 'paper.sqlite'
            self.marker = folder / 'paper-account.json'
        def read(self, include_charts=False):
            return {'strategy': {'draining': owner.draining},
                    'positions': {} if owner.closed else {'test': {}}}
        def history(self, limit=500):
            result = [{'action': 'OPEN'}]
            if owner.closed: result.append({'action': 'CLOSE'})
            return result
        def audit(self): calls.append('audit')
    monkeypatch.setattr(auto_live_paper, 'AcceptanceSession', Session)
    monkeypatch.setattr(auto_live_paper, 'Store', FakeStore)
    monkeypatch.setattr(auto_live_paper, 'execution_freshness',
                        lambda *_: {'status': 'PASS' if fresh else 'PARTIAL_EVIDENCE'})
    monkeypatch.setattr(auto_live_paper, 'export_current_report', lambda *a, **k: None)
    monkeypatch.setattr(auto_live_paper.time, 'sleep', lambda _: None)
    result = auto_live_paper.run(tmp_path / 'fresh_output', duration=15)
    assert calls == ['create', 'start', 'drain', 'close', 'audit']
    assert result['status'] == ('PAPER_CYCLE_PASS' if fresh else 'NOT_CONFIRMED')
    assert result['real_orders'] is False and result['release_ready'] is False
    assert (tmp_path / 'fresh_output' / 'live_paper_report.json').is_file()


def test_owned_paper_rejects_existing_workspace(tmp_path):
    existing = tmp_path / 'existing'
    existing.mkdir()
    with pytest.raises(ValueError, match='Fresh'):
        auto_live_paper.run(existing)


def test_waits_for_delayed_worker_account_without_creating_or_reading_it(tmp_path, monkeypatch):
    import sqlite3
    calls = []
    folder = tmp_path / 'owned_bot'
    class Owner:
        draining = False
        closed = False
        def create_bot(self, cfg):
            folder.mkdir()
            return folder
        def request_start(self, _): calls.append('start')
        def submit_drain(self, _, *, draining):
            self.draining = True
            calls.append('drain')
        def submit_manual_close(self, _):
            self.closed = True
            calls.append('close')
    owner = Owner()
    class Session:
        def __init__(self, root, output, **kwargs):
            self.root, self.output, self.owner = Path(root), Path(output), owner
        def __enter__(self):
            self.root.mkdir(parents=True)
            self.output.mkdir(parents=True)
            return self
        def __exit__(self, *args):
            (self.output / 'session.json').write_text(json.dumps({
                'status': 'STOPPED_STATE_PRESERVED', 'services_retired': True}), encoding='utf8')
    class FakeStore:
        def __init__(self, folder):
            self.path, self.marker = folder / 'paper.sqlite', folder / 'paper-account.json'
        def read(self, include_charts=False):
            if not self.marker.is_file():
                raise sqlite3.OperationalError('unable to open database file')
            calls.append('read')
            return {'strategy': {'draining': owner.draining},
                    'positions': {} if owner.closed else {'test': {}}}
        def history(self, limit=500):
            return [{'action': 'OPEN'}] + ([{'action': 'CLOSE'}] if owner.closed else [])
        def audit(self): calls.append('audit')
    def startup_sleep(_):
        calls.append('wait')
        if calls.count('wait') == 2:
            (folder / 'paper.sqlite').write_bytes(b'created by worker fixture')
            (folder / 'paper-account.json').write_text('{}', encoding='utf8')
    monkeypatch.setattr(auto_live_paper, 'AcceptanceSession', Session)
    monkeypatch.setattr(auto_live_paper, 'Store', FakeStore)
    monkeypatch.setattr(auto_live_paper, 'execution_freshness', lambda *_: {'status': 'PASS'})
    monkeypatch.setattr(auto_live_paper, 'export_current_report', lambda *a, **k: None)
    monkeypatch.setattr(auto_live_paper.time, 'sleep', startup_sleep)
    result = auto_live_paper.run(tmp_path / 'fresh_output', duration=15)
    assert result['status'] == 'PAPER_CYCLE_PASS'
    assert calls.index('read') > calls.index('wait')
    assert calls.count('wait') >= 2 and calls.count('audit') == 1


def test_missing_previously_initialized_ledger_is_not_recreated(tmp_path, monkeypatch):
    folder = tmp_path / 'existing_account'
    captures = []
    class Owner:
        def create_bot(self, cfg):
            folder.mkdir()
            (folder / 'paper-account.json').write_text('{}', encoding='utf8')
            return folder
        def request_start(self, _): pass
    class Session:
        owner = Owner()
        def __init__(self, root, output, **kwargs):
            self.root, self.output = Path(root), Path(output)
        def __enter__(self):
            self.root.mkdir(parents=True)
            self.output.mkdir(parents=True)
            return self
        def __exit__(self, *args):
            (self.output / 'session.json').write_text(json.dumps({
                'status': 'STOPPED_STATE_PRESERVED', 'services_retired': True}), encoding='utf8')
    monkeypatch.setattr(auto_live_paper, 'AcceptanceSession', Session)
    monkeypatch.setattr(auto_live_paper, 'export_current_report',
                        lambda *a, **k: captures.append((a, k)))
    report = auto_live_paper.run(tmp_path / 'fresh_output', duration=15)
    assert report['status'] == 'NOT_CONFIRMED'
    assert 'paper.sqlite' in report['error']
    assert not (folder / 'paper.sqlite').exists()
    assert len(captures) == 1 and captures[0][1]['allow_empty'] is True


def test_unattended_cycle_with_real_worker_and_delayed_replay_transport(tmp_path, monkeypatch):
    """Exercise account creation, controller, OS worker and ledger together."""
    import threading
    import time
    from bcc.runtime_service import serve
    from bcc.storage import Repository, atomic_json
    from bcc.supervisor import Supervisor
    from test_process import RUNNER, launch

    holders = {}
    class Session:
        def __init__(self, root, output, **kwargs):
            self.root, self.output = Path(root), Path(output)
            self.stop = threading.Event()
            self.failures = []
        def __enter__(self):
            self.root.mkdir(parents=True)
            self.output.mkdir(parents=True)
            self.owner = Supervisor(Repository(self.root))
            runner = tmp_path / 'independent_replay_worker.py'
            original_create = self.owner.create_bot
            def create(cfg):
                folder = original_create(cfg)
                wall = time.time()
                market = int(wall) // 60 * 60 + 10
                base = int(market) // 60 * 60000 - 120000
                atomic_json(folder / 'feed.json', {
                    'price': 101, 'market_origin': market, 'wall_origin': wall,
                    'bars': [dict(t=base + i * 60000, open=x, high=x+1, low=x-1,
                                  close=x, volume=10) for i, x in enumerate([99, 101, 101])]})
                holders['folder'] = folder
                return folder
            def start(folder):
                # Real worker starts asynchronously through the real service;
                # only its public market provider is a deterministic replay.
                row = self.owner.coordinator.get_registration(self.owner._key(folder))
                script = RUNNER.replace('worker.main()',
                    "__import__('sys').argv.extend(['--control-revision', '" +
                    str(row['command_version']) + "'])\nworker.main()")
                runner.write_text(script, encoding='utf8')
                child = launch(self.owner.lifecycle, folder, runner)
                holders['child'] = child
                return child.pid
            self.owner.create_bot = create
            self.owner.lifecycle.start = start
            def service():
                try:
                    serve(self.root, supervisor=self.owner, stop=self.stop,
                          interval=.04, batch_size=3, inventory_interval=.1)
                except BaseException as exc:
                    self.failures.append(exc)
            self.thread = threading.Thread(target=service, daemon=True)
            self.thread.start()
            return self
        def __exit__(self, *args):
            if holders.get('child') and holders['child'].poll() is None:
                self.owner.request_stop(holders['folder'])
                try:
                    holders['child'].wait(timeout=10)
                except Exception:
                    self.owner.lifecycle.stop(holders['folder'], force=True)
                    holders['child'].wait(timeout=10)
            self.stop.set()
            self.thread.join(timeout=10)
            assert not self.thread.is_alive() and not self.failures
            (self.output / 'session.json').write_text(json.dumps({
                'status': 'STOPPED_STATE_PRESERVED', 'services_retired': True}), encoding='utf8')
    monkeypatch.setattr(auto_live_paper, 'AcceptanceSession', Session)
    report = auto_live_paper.run(tmp_path / 'fresh_output', duration=20)
    assert report['status'] == 'PAPER_CYCLE_PASS', report
    assert report['entry'] and report['exit'] and report['ledger'] == 'PASS'
    assert report['execution_freshness']['status'] == 'PASS'
    assert holders['child'].poll() is not None
