"""Regression for the double-click Windows AI entrypoint (Linux mock tests).

These never claim to execute Windows CMD, Codex, or Ollama on the user's PC.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from tools import windows_ai_launcher as launch
from tools import ai_dev_pipeline as pipeline


def test_batch_launchers_hold_screen_and_keep_last_log():
    root = Path(__file__).resolve().parents[1]
    for action in ('CYCLE', 'SETUP'):
        source = (root / f'AI_{action}_WINDOWS.bat').read_text(encoding='utf-8')
        assert 'if /I "%BCC_AI_NO_PAUSE%"=="1"' in source
        assert 'pause' in source
        assert 'PYTHONIOENCODING=utf-8' in source
        assert 'PYTHONUTF8=1' in source
        assert 'set "RC=%ERRORLEVEL%"' in source
        assert '.venv\\Scripts\\python.exe' in source
        assert f'AI_{action}_WINDOWS_LAST.log' in source
        assert f'-m tools.windows_ai_launcher {action.lower()}' in source
        assert 'goto :finish' in source
        assert 'BLOCKED' in source


def test_streaming_tool_output_and_real_exit_code(monkeypatch, tmp_path, capsys):
    class Child:
        stdout = io.StringIO('first line\nerror message\n')
        def wait(self, *a, **kw): return 47
        def poll(self): return 47
    observed = {}
    def factory(args, **kwargs):
        observed['args'] = args
        observed['kwargs'] = kwargs
        return Child()
    with (tmp_path / 'last.log').open('w', encoding='utf-8') as log:
        code = launch.run_step('cycle', log, runner=factory, extra=('--qa', 'windows'))
    assert code == 47
    assert '-m' in observed['args']
    assert observed['args'][-2:] == ['--qa', 'windows']
    assert 'first line' in (tmp_path / 'last.log').read_text(encoding='utf-8')
    assert 'error message' in capsys.readouterr().out


def test_setup_blocks_on_missing_doctor_and_does_not_init(monkeypatch, tmp_path):
    stages = []
    monkeypatch.setattr(launch, 'WORK', tmp_path / '.ai_work')
    monkeypatch.setattr(launch, 'os', SimpleNamespace(name='nt', getenv=lambda *a: None))
    def fake_step(action, log, *, extra=()):
        stages.append(action)
        return 2
    monkeypatch.setattr(launch, 'run_step', fake_step)
    assert launch.main(['setup']) == 2
    assert stages == ['doctor']
    assert 'BLOCKED at doctor' in (launch.WORK / 'AI_SETUP_WINDOWS_LAST.log').read_text(encoding='utf-8')


def test_cycle_auto_initializes_git_and_blocks_if_windows_qa_fails(monkeypatch, tmp_path):
    stages = []
    monkeypatch.setattr(launch, 'WORK', tmp_path / '.ai_work')
    monkeypatch.setattr(launch, 'os', SimpleNamespace(name='nt', getenv=lambda *a: None))
    def fake_step(action, log, *, extra=()):
        stages.append((action, extra))
        return 3 if action == 'cycle' else 0
    monkeypatch.setattr(launch, 'run_step', fake_step)
    assert launch.main(['cycle']) == 3
    assert stages == [('doctor', ()), ('init', ()), ('cycle', ('--qa', 'windows'))]
    assert 'BLOCKED at cycle' in (launch.WORK / 'AI_CYCLE_WINDOWS_LAST.log').read_text(encoding='utf-8')


def test_non_windows_launcher_blocks_before_any_activity(monkeypatch, tmp_path):
    stages = []
    monkeypatch.setattr(launch, 'WORK', tmp_path / '.ai_work')
    monkeypatch.setattr(launch, 'run_step', lambda *a, **kw: stages.append(a))
    assert launch.main(['cycle']) == 2
    assert stages == []


def test_rejects_diagnostic_log_outside_work_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(launch, 'WORK', tmp_path / '.ai_work')
    monkeypatch.setenv('BCC_AI_LAUNCH_LOG', str(tmp_path / 'outside.log'))
    with pytest.raises(ValueError, match='inside'):
        launch._log_path('cycle')


def test_child_process_output_is_written_before_exit(tmp_path):
    logfile = tmp_path / 'child.log'
    r = pipeline.process([sys.executable, '-c', 'print("early evidence", flush=True)'],
                         timeout=6, output=logfile)
    assert r.returncode == 0
    assert 'early evidence' in logfile.read_text(encoding='utf-8')


def test_cycle_does_not_return_success_for_failed_qa(monkeypatch):
    monkeypatch.setattr(pipeline, 'cycle', lambda args: {'qa': 'FAIL', 'release_ready': False})
    assert pipeline.main(['cycle', '--qa', 'windows']) == 3
    monkeypatch.setattr(pipeline, 'cycle', lambda args: {'qa': 'PASS', 'release_ready': False})
    assert pipeline.main(['cycle', '--qa', 'windows']) == 0
