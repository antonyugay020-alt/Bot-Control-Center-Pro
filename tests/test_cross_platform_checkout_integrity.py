"""Verify sealed raw source bytes survive Git checkout on every platform."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.verify_package import REQUIRED_FILES


ROOT = Path(__file__).resolve().parents[1]


def test_git_attributes_are_required_by_package_guard():
    assert '.gitattributes' in REQUIRED_FILES
    attrs = (ROOT / '.gitattributes').read_text(encoding='utf-8')
    assert any(line.strip() == '* -text' for line in attrs.splitlines())


@pytest.mark.skipif(shutil.which('git') is None, reason='git executable unavailable')
def test_windows_autocrlf_cannot_rewrite_sealed_source_bytes(tmp_path):
    """Simulate a Windows-style Git checkout, including mixed line endings."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    shutil.copyfile(ROOT / '.gitattributes', repo / '.gitattributes')
    samples = {
        'bcc/worker.py': b'print("worker")\n',
        'START.bat': b'@echo off\r\necho start\r\n',
        '.github/workflows/ci.yml': b'name: QA\non: push\n',
    }
    for name, payload in samples.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    subprocess.run(['git', '-c', 'core.autocrlf=true', 'add', '.'], cwd=repo, check=True)
    target = tmp_path / 'checkout'
    target.mkdir()
    subprocess.run([
        'git', '-c', 'core.autocrlf=true', 'checkout-index', '-a',
        '--prefix=' + str(target) + '/',
    ], cwd=repo, check=True)
    for name, payload in samples.items():
        assert (target / name).read_bytes() == payload, name

def test_ci_requires_native_windows_qt_evidence_not_offscreen_pytest():
    workflow = (ROOT / '.github/workflows/bcc-ci.yml').read_text(encoding='utf-8')
    assert '  windows-native-qa:' in workflow
    linux, windows = workflow.split('  windows-native-qa:', 1)
    assert 'libegl1' in linux and 'libxkbcommon-x11-0' in linux
    assert 'QT_QPA_PLATFORM: windows' in windows
    assert 'python -m tools.verify_windows --output' in windows
    assert 'bcc-windows-native-qa' in windows
    assert 'python -m pytest tests' not in windows
