"""Manual QA runner is pinned to its designated, non-linked source root."""
import hashlib
import json

import pytest

from tools import manual_qa_9047 as runner


def test_existing_qa_root_requires_pinned_manifest(tmp_path, monkeypatch):
    root = tmp_path / runner.EXPECTED_NAME
    root.mkdir()
    data = b'{"test": "manifest"}'
    (root / 'SHA256_MANIFEST.json').write_bytes(data)
    monkeypatch.setattr(runner, 'EXPECTED_MANIFEST', hashlib.sha256(data).hexdigest())
    assert runner.validate_root(root) == root.resolve()
    (root / 'SHA256_MANIFEST.json').write_bytes(b'changed')
    with pytest.raises(ValueError, match='does not match'):
        runner.validate_root(root)


def test_other_installation_or_linked_runtime_folder_is_rejected(tmp_path, monkeypatch):
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    with pytest.raises(ValueError, match='BCC_NATIVE_QA_9047b10'):
        runner.validate_root(unrelated)
    root = tmp_path / runner.EXPECTED_NAME
    root.mkdir()
    data = b'qa-manifest'
    (root / 'SHA256_MANIFEST.json').write_bytes(data)
    monkeypatch.setattr(runner, 'EXPECTED_MANIFEST', hashlib.sha256(data).hexdigest())
    try:
        (root / 'check_workspaces').symlink_to(unrelated, target_is_directory=True)
    except OSError:
        pytest.skip('Directory symlinks unavailable')
    with pytest.raises(ValueError, match='Unsafe QA workspace'):
        runner.validate_root(root)


def test_prior_qa_evidence_only_copied_for_both_matching_source_boundaries(tmp_path):
    root, output = tmp_path / 'qa', tmp_path / 'evidence'
    auto = root / 'verification_reports' / 'check_example' / 'automated'
    auto.mkdir(parents=True)
    output.mkdir()
    seal = {'manifest_sha256': runner.EXPECTED_MANIFEST}
    summary = {'source_integrity': seal, 'source_integrity_final': seal, 'status': 'AUTOMATED_PASS'}
    report = {'source_integrity': seal, 'source_integrity_final': seal,
              'test_totals': {'tests': 3920, 'failures': 0}}
    (auto.parent / 'check_summary.json').write_text(json.dumps(summary), encoding='utf-8')
    (auto / 'windows_report.json').write_text(json.dumps(report), encoding='utf-8')
    result = runner.copy_existing_automated_evidence(root, output)
    assert result['status'] == 'COPIED'
    assert result['reported_test_totals']['tests'] == 3920
    assert (output / 'automated_qa' / 'check_summary.json').is_file()
    report['source_integrity_final'] = {'manifest_sha256': 'wrong'}
    (auto / 'windows_report.json').write_text(json.dumps(report), encoding='utf-8')
    assert runner.copy_existing_automated_evidence(root, output)['status'] == 'NOT_FOUND'
