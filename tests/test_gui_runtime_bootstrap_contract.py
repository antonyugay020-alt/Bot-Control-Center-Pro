"""Entrypoint equivalence and independent runtime availability contract.

Actual Qt interaction is mandatory in Windows acceptance; these portable
checks guard the upstream service bootstrap without pretending to render Qt.
"""
from pathlib import Path
import ast

import pytest

from bcc import bootstrap

ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_reuses_verified_owner_without_spawning(tmp_path, monkeypatch):
    ready = {'state': 'ready', 'run_id': 'live'}
    monkeypatch.setattr(bootstrap, 'observe', lambda _root: ready)
    def forbidden(*args, **kwargs):
        raise AssertionError('a second guardian was launched')
    assert bootstrap.ensure_runtime(tmp_path, spawn=forbidden) is ready


def test_bootstrap_starts_guardian_and_requires_verified_readiness(tmp_path, monkeypatch):
    calls = []
    def spawn(args, **kwargs):
        calls.append((args, kwargs))
    monkeypatch.setattr(bootstrap, 'observe', lambda _root: None)
    def ready(root, *, timeout):
        assert root == tmp_path.resolve()
        assert timeout == 2
        return {'run_id': 'accepted', 'state': 'ready'}
    monkeypatch.setattr(bootstrap, 'wait_ready', ready)
    state = bootstrap.ensure_runtime(tmp_path, spawn=spawn, timeout=2)
    assert state['state'] == 'ready'
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[1:3] == ['-m', 'bcc.service_guard']
    assert args[args.index('--data-root') + 1] == str(tmp_path.resolve())
    assert kwargs['stdin'] is not None
    assert kwargs["stderr"] == __import__("subprocess").STDOUT


def test_bootstrap_timeout_blocks_gui_instead_of_simulating_start(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, 'observe', lambda _root: None)
    def fail(*_args, **_kwargs):
        raise RuntimeError('Runtime service did not become ready')
    monkeypatch.setattr(bootstrap, 'wait_ready', fail)
    with pytest.raises(RuntimeError, match='did not become ready'):
        bootstrap.ensure_runtime(tmp_path, spawn=lambda *_a, **_k: None)


def test_both_gui_entrypoints_bootstrap_service_before_window():
    app = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
    fn = next(node for node in app.body
              if isinstance(node, ast.FunctionDef) and node.name == 'main')
    calls = [(node.func.id if isinstance(node.func, ast.Name) else '')
             for node in ast.walk(fn) if isinstance(node, ast.Call)]
    assert calls.index('ensure_runtime') < calls.index('MainWindow')
    consoleless = (ROOT / 'launch.pyw').read_text(encoding='utf-8')
    assert 'from app import main' in consoleless
    assert 'result = main()' in consoleless
    assert 'ensure_runtime(resolve_data_root())' not in consoleless
    # Native E2E must exercise service-driven command execution, never its
    # former test-only direct GUI -> worker shortcut.
    native = (ROOT / 'tests/test_native_dashboard_workflow_qt.py').read_text(encoding='utf-8')
    assert 'serve(win.repo.root, supervisor=win.supervisor' in native
    assert 'wait_for(lambda: len(children) == 1' in native
    assert "click_action(app, win, '▶ Запуск')" in native
