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
    assert 'README.md' in manifest['files']
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


@pytest.mark.parametrize('extra', [
    'strategy_overrides.json',
    'bcc/native_bridge.dll',
    'tools/market_policy.toml',
    'assets/dashboard.css',
    'docs/unsealed_instructions.md',
])
def test_unmanifested_nonexecutable_source_blocks_acceptance(tmp_path, extra):
    """A changed strategy/config/native asset is as significant as new Python."""
    import shutil
    from tools.seal_source import source_paths

    original = Path(__file__).resolve().parents[1]
    for name, path in source_paths(original):
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    shutil.copyfile(original / 'SHA256_MANIFEST.json', tmp_path / 'SHA256_MANIFEST.json')
    unexpected = tmp_path / extra
    unexpected.parent.mkdir(parents=True, exist_ok=True)
    unexpected.write_bytes(b'unsealed input')
    with pytest.raises(SourceIntegrityError, match='Незарегистрированный файл исходников'):
        verify(tmp_path)


def test_seal_includes_new_nonexecutable_sources_and_ignores_qa_state(tmp_path):
    """Sealing and verification must agree on the whole source tree."""
    import shutil
    from tools.seal_source import source_paths

    original = Path(__file__).resolve().parents[1]
    working = tmp_path / 'working'
    for name, path in source_paths(original):
        destination = working / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    extras = {
        'bcc/strategy_policy.json': '{"mode":"paper"}\\n',
        'assets/theme.css': 'body { color: white; }\\n',
        'docs/notes.md': 'Immutable source note\\n',
    }
    for name, content in extras.items():
        destination = working / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding='utf-8')
    # Runtime outputs must not invalidate a source snapshot or enter a ZIP.
    for name in (
        'verification_reports/run/report.json',
        'check_workspaces/qa/bot.json',
        '.check_venv/Lib/site-packages/runtime.pth',
        'MY_BOTS/VELES/account.sqlite',
    ):
        target = working / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('runtime', encoding='utf-8')
    output = tmp_path / 'all-sources.zip'
    seal(working, output, checkpoint='BCC_ALL_SOURCE_TYPES')
    with zipfile.ZipFile(output) as archive:
        members = set(archive.namelist())
        assert {'BCC_ALL_SOURCE_TYPES/' + name for name in extras} <= members
        assert not any('verification_reports/' in name or 'check_workspaces/' in name
                       or '.check_venv/' in name or 'MY_BOTS/' in name for name in members)
        archive.extractall(tmp_path / 'unpacked')
    assert verify(tmp_path / 'unpacked' / 'BCC_ALL_SOURCE_TYPES')['status'] == 'PASS'
