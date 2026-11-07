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



def test_sealed_snapshot_includes_new_entrypoints_and_ci_workflows(tmp_path):
    """A fresh executable must be sealed, not silently dropped before QA."""
    import shutil

    from tools.seal_source import source_paths
    from tools.verify_package import executable_sources

    original = Path(__file__).resolve().parents[1]
    source = tmp_path / 'working'
    for name, path in source_paths(original):
        destination = source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)

    extras = {
        'launch_extra.pyw': "print('new launcher')\n",
        'tools/fleet_operator.ps1': "Write-Output 'operator'\n",
        '.github/workflows/fleet-qa.yml': "name: Fleet QA\non: workflow_dispatch\njobs: {}\n",
    }
    for name, content in extras.items():
        destination = source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding='utf-8')
    debris = source / '.ai_work' / 'private.py'
    debris.parent.mkdir(parents=True)
    debris.write_text('must not package', encoding='utf-8')

    discovered = {name for name, _ in executable_sources(source)}
    assert set(extras) <= discovered
    assert '.ai_work/private.py' not in discovered

    output = tmp_path / 'full-source.zip'
    seal(source, output, checkpoint='BCC_NEW_SOURCE_INVENTORY')
    with zipfile.ZipFile(output) as archive:
        members = set(archive.namelist())
        assert {'BCC_NEW_SOURCE_INVENTORY/' + name for name in extras} <= members
        assert 'BCC_NEW_SOURCE_INVENTORY/.github/workflows/bcc-ci.yml' in members
        assert 'BCC_NEW_SOURCE_INVENTORY/.ai_work/private.py' not in members
        archive.extractall(tmp_path / 'unpacked')
    assert verify(tmp_path / 'unpacked' / 'BCC_NEW_SOURCE_INVENTORY')['status'] == 'PASS'

def test_source_seal_rejects_unsafe_root(tmp_path):
    source = Path(__file__).resolve().parents[1]
    with pytest.raises(ValueError, match='Invalid unique'):
        seal(source, tmp_path / 'bad.zip', checkpoint='../collision')
