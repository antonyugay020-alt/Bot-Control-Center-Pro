"""A successful stage cannot certify code changed or resealed afterwards."""
import hashlib
import json
import os
import sys
from types import SimpleNamespace

import pytest

from tools import verify_windows, windows_check, acceptance_session
from tools.verify_package import SourceIntegrityError, verify, assert_same_snapshot
from test_windows_preflight_r67 import sealed
from test_r65_windows_evidence_gate import fixture as evidence_fixture


def alter(root, mode):
    manifest = root / 'SHA256_MANIFEST.json'
    value = json.loads(manifest.read_text(encoding="utf-8"))
    if mode in ('code', 'reseal'):
        entry = root / 'app.py'
        entry.write_bytes(entry.read_bytes() + b'\nCHANGED')
        if mode == 'reseal':
            value['files']['app.py'] = hashlib.sha256(entry.read_bytes()).hexdigest()
            manifest.write_text(json.dumps(value), encoding="utf-8")
    elif mode == 'manifest':
        value['checkpoint'] += '_changed'
        manifest.write_text(json.dumps(value), encoding="utf-8")


def test_resealing_is_a_new_snapshot_even_when_each_file_hash_is_valid(sealed):
    initial = verify(sealed)
    alter(sealed, 'reseal')
    current = verify(sealed)
    assert current['status'] == initial['status'] == 'PASS'
    with pytest.raises(SourceIntegrityError, match='изменились'):
        assert_same_snapshot(initial, current)


@pytest.mark.parametrize('mode', ['none', 'code', 'reseal', 'manifest'])
def test_native_automation_rechecks_the_same_seal_after_all_steps(sealed, tmp_path, monkeypatch, mode):
    output = tmp_path / 'report'
    monkeypatch.setattr(verify_windows, 'ROOT', sealed)
    monkeypatch.setattr(verify_windows, 'is_native_windows', lambda: True)
    monkeypatch.setattr(verify_windows, 'source_fingerprint', lambda: 'f' * 64)
    monkeypatch.setattr(sys, 'argv', ['verify_windows', '--output', str(output)])
    def step(name, _arguments, out, timeout):
        if name == 'public_sources':
            evidence_fixture(out)
            alter(sealed, mode)
        return dict(step=name, status='PASS', exit_code=0)
    monkeypatch.setattr(verify_windows, 'run_step', step)
    assert verify_windows.main() == (0 if mode == 'none' else 1)
    report = json.loads((output / 'windows_report.json').read_text(encoding="utf-8"))
    assert report['status'] == ('AUTOMATED_CHECKS_PASS' if mode == 'none' else 'FAIL')
    assert report['source_integrity_final']['status'] == ('PASS' if mode == 'none' else 'FAIL')
    assert report['release_ready'] is False


@pytest.mark.parametrize('during', ['automation', 'application'])
def test_changed_package_cannot_open_or_reopen_manual_ui(sealed, monkeypatch, during):
    monkeypatch.setattr(windows_check, 'ROOT', sealed)
    monkeypatch.setattr(windows_check, 'os', SimpleNamespace(name='nt', environ=dict(os.environ)))
    scopes, visited, prompts = [], [], []
    cls = acceptance_session.AcceptanceSession
    def scope(root, output, **_kwargs):
        instance = cls(root, output, python=sys.executable)
        scopes.append(instance)
        return instance
    monkeypatch.setattr(acceptance_session, 'AcceptanceSession', scope)
    monkeypatch.setattr(windows_check, 'manual_reopen_requested', lambda: prompts.append(True) or True)
    def step(name, _arguments, output, **_kwargs):
        visited.append(name)
        if name == ('automated' if during == 'automation' else 'application'):
            alter(sealed, 'reseal')
        return dict(step=name, exit_code=0)
    monkeypatch.setattr(windows_check, 'run_step', step)
    assert windows_check.main([]) == 1
    assert len(scopes) == (0 if during == 'automation' else 1)
    assert 'application_2' not in visited
    assert not prompts
    assert all(not scope.thread.is_alive() for scope in scopes)
    report_path = next((sealed / 'verification_reports').glob('*/check_summary.json'))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report['status'] == 'SOURCE_INTEGRITY_CHANGED'
    assert report['source_integrity_final']['status'] == 'FAIL'
    assert report['release_ready'] is False
