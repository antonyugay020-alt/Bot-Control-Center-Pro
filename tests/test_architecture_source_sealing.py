"""The exact native check inputs must be present and immutable after extraction."""
import json
import zipfile
from pathlib import Path

import pytest

from tools.seal_source import seal
from tools.verify_package import SourceIntegrityError, verify


def test_source_seal_excludes_runtime_debris_and_verifies_entire_tree(tmp_path):
    source = Path(__file__).resolve().parents[1]
    output = tmp_path / 'sealed.zip'
    checkpoint = 'BCC_ARCHITECTURE_TEST'
    result = seal(source, output, checkpoint=checkpoint)
    assert result['source_files'] > 100
    with zipfile.ZipFile(output) as archive:
        members = archive.namelist()
        assert all(name.startswith(checkpoint + '/') for name in members)
        assert not any('__pycache__' in name or '.pytest_cache' in name or name.endswith('.sqlite') for name in members)
        archive.extractall(tmp_path / 'unpacked')
    root = tmp_path / 'unpacked' / checkpoint
    assert verify(root)['status'] == 'PASS'
    manifest = json.loads((root / 'SHA256_MANIFEST.json').read_text(encoding='utf-8'))
    assert 'files' in manifest and 'bcc/worker.py' in manifest['files']
    (root / 'bcc' / 'runtime_service.py').write_text('corrupt', encoding="utf-8")
    with pytest.raises(SourceIntegrityError, match='SHA-256'):
        verify(root)


def test_source_seal_rejects_unsafe_root(tmp_path):
    source = Path(__file__).resolve().parents[1]
    with pytest.raises(ValueError, match='Invalid unique'):
        seal(source, tmp_path / 'bad.zip', checkpoint='../collision')
