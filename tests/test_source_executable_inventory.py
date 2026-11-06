"""New executable entry points must not escape snapshot sealing."""
import json
from pathlib import Path

import pytest

from tools.verify_package import SourceIntegrityError, verify


@pytest.mark.parametrize('path', [
    'START_ALTERNATE.bat',
    'launch_extra.pyw',
    'tools/secret_bootstrap.ps1',
    '.github/workflows/unsealed-ci.yml',
    'bcc/unsealed_strategy.py',
])
def test_unmanifested_executable_is_rejected(tmp_path, path):
    """A newly introduced launcher must require explicit manifest resealing."""
    import shutil
    source = Path(__file__).resolve().parents[1]
    manifest = json.loads((source / 'SHA256_MANIFEST.json').read_text(encoding='utf-8'))
    for name in manifest['files']:
        source_file = source / name
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, target)
    shutil.copyfile(source / 'SHA256_MANIFEST.json', tmp_path / 'SHA256_MANIFEST.json')
    candidate = tmp_path / path
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text('unsafe extra entrypoint', encoding='utf-8')
    with pytest.raises(SourceIntegrityError, match='Незарегистрированный исполняемый файл'):
        verify(tmp_path)
