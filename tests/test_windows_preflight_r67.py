"""Regression of preflight source integrity and truthful Windows QA gates."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from tools.verify_package import SourceIntegrityError, verify, REQUIRED_FILES
from tools.windows_check import acceptance_status, automated_step_passed


@pytest.fixture
def sealed(tmp_path):
    root = tmp_path / 'sealed'
    root.mkdir()
    # A minimal self-contained source package with thirty manifest entries.
    tracked = list(REQUIRED_FILES)
    tracked += [f'tests/test_{i:02d}.py' for i in range(30)]
    files = {}
    for item in tracked:
        path = root / item
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = ('source:' + item).encode()
        path.write_bytes(payload)
        files[item] = hashlib.sha256(payload).hexdigest()
    (root / 'SHA256_MANIFEST.json').write_text(
        json.dumps({'checkpoint': 'QA-FIXTURE', 'files': files}), encoding='utf-8'
    )
    return root


def test_sealed_complete_snapshot_passes(sealed):
    assert verify(sealed) == {'status': 'PASS', 'verified_files': len(REQUIRED_FILES) + 30,
                              'checkpoint': 'QA-FIXTURE', 'manifest_sha256': hashlib.sha256(
                                  (sealed / 'SHA256_MANIFEST.json').read_bytes()).hexdigest()}


def test_modified_source_fails_before_execution(sealed):
    (sealed / 'bcc').mkdir(exist_ok=True)
    path = sealed / 'app.py'
    path.write_text('modified', encoding="utf-8")
    with pytest.raises(SourceIntegrityError, match='SHA-256'):
        verify(sealed)


def test_missing_module_and_unsealed_python_are_rejected(sealed):
    (sealed / 'tools/verify_windows.py').unlink()
    with pytest.raises(SourceIntegrityError, match='Отсутствует'):
        verify(sealed)
    (sealed / 'tools/verify_windows.py').write_bytes(b'source:tools/verify_windows.py')
    (sealed / 'bcc').mkdir(exist_ok=True)
    (sealed / 'bcc/hidden.py').write_text('unexpected module', encoding="utf-8")
    with pytest.raises(SourceIntegrityError, match='Незарегистрированный'):
        verify(sealed)


def test_invalid_manifest_relative_path_rejected(sealed):
    data = json.loads((sealed / 'SHA256_MANIFEST.json').read_text(encoding="utf-8"))
    data['files']['../escape.py'] = '0' * 64
    (sealed / 'SHA256_MANIFEST.json').write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SourceIntegrityError, match='Недопустимый'):
        verify(sealed)


@pytest.mark.parametrize(('step', 'accepted'), [
    ({'exit_code': 0}, True), ({'exit_code': 1}, False),
    ({'exit_code': None, 'error': 'Timeout'}, False),
    ({'exit_code': 0, 'error': 'Incomplete'}, False),
])
def test_manual_paper_cannot_start_after_failed_automation(step, accepted):
    assert automated_step_passed(step) is accepted


def test_automated_pass_is_not_manual_paper_pass():
    assert acceptance_status(True, True) == 'AUTOMATED_PASS_MANUAL_PENDING'
    assert acceptance_status(True, False) == 'AUTOMATED_PASS_MANUAL_NOT_RUN'
    assert acceptance_status(False, True) == 'AUTOMATED_FAILED'
