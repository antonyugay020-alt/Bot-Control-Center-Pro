"""The three processes must address the same durable state, independent of cwd."""
import os
import subprocess
import sys
from pathlib import Path
from bcc.runtime_paths import code_root, data_root, python_environment


def test_shared_root_override_across_processes(tmp_path, monkeypatch):
    root = tmp_path / 'State Folder'
    monkeypatch.setenv('BCC_DATA_ROOT', str(root))
    assert data_root() == root.resolve()
    env = dict(os.environ)
    env['PYTHONPATH'] = str(code_root())
    output = subprocess.check_output([sys.executable, '-c',
        'from bcc.runtime_paths import data_root; print(data_root())'],
        cwd=tmp_path, env=python_environment(env), text=True, encoding='utf-8').strip()
    assert Path(output) == root.resolve()
    assert root.is_dir()


def test_override_has_priority_over_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('BCC_DATA_ROOT', str(tmp_path / 'wrong'))
    actual = data_root(tmp_path / 'right')
    assert actual == (tmp_path / 'right').resolve()
    assert not (tmp_path / 'wrong').exists()


def test_code_and_state_locations_are_separate(tmp_path, monkeypatch):
    monkeypatch.setenv('BCC_DATA_ROOT', str(tmp_path / 'bot-state'))
    assert code_root().is_dir()
    assert data_root() != code_root()
