"""The current QA helper accepts only a fresh, explicitly pinned checkout."""
import json
import pytest
from tools import manual_qa_current as runner

SHA = '1' * 40


def make_root(tmp_path):
    root = tmp_path / 'BCC_NATIVE_QA_CURRENT_example'
    root.mkdir()
    (root / 'SHA256_MANIFEST.json').write_text('{}', encoding='utf8')
    return root


def test_current_root_requires_exact_commit_and_dedicated_directory(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    monkeypatch.setattr(runner.subprocess, 'check_output', lambda *a, **k: SHA + '\n')
    assert runner.validate_root(root, SHA) == root.resolve()
    with pytest.raises(ValueError, match='differs'):
        runner.validate_root(root, '2' * 40)
    with pytest.raises(ValueError, match='full lowercase'):
        runner.validate_root(root, '9047b10')
    root.rename(tmp_path / 'unrelated')
    with pytest.raises(ValueError, match='freshly created'):
        runner.validate_root(tmp_path / 'unrelated', SHA)


@pytest.mark.parametrize('simulate_unavailable', [False, True])
def test_current_root_rejects_linked_runtime_directory(tmp_path, monkeypatch, simulate_unavailable):
    root = make_root(tmp_path)
    other = tmp_path / 'other'
    other.mkdir()
    link = root / 'check_workspaces'
    if simulate_unavailable:
        def deny_symlink(*args, **kwargs):
            raise OSError('simulated Windows symlink privilege unavailable')
        monkeypatch.setattr(type(link), 'symlink_to', deny_symlink)
    try:
        link.symlink_to(other, target_is_directory=True)
    except OSError:
        # Still exercise the production rejection branch where Windows does
        # not grant symlink creation. This is not an OS-level symlink test.
        link.mkdir()
        original = type(link).is_symlink
        monkeypatch.setattr(type(link), 'is_symlink',
                            lambda path: path == link or original(path))
    monkeypatch.setattr(runner.subprocess, 'check_output', lambda *a, **k: SHA)
    with pytest.raises(ValueError, match='Unsafe QA workspace'):
        runner.validate_root(root, SHA)


def test_prior_evidence_cannot_match_an_unverified_empty_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'EXPECTED_MANIFEST', None)
    with pytest.raises(ValueError, match='Verify'):
        runner.copy_existing_automated_evidence(tmp_path, tmp_path)


def test_manual_evidence_is_a_real_file_in_its_own_session(tmp_path):
    (tmp_path / 'snapshot.zip').write_bytes(b'evidence')
    assert runner.evidence_file(tmp_path, 'snapshot.zip')
    assert not runner.evidence_file(tmp_path, '../snapshot.zip')
    assert not runner.evidence_file(tmp_path, 'missing.zip')
    assert not runner.evidence_file(tmp_path, '')


def test_checklist_has_distinct_individual_acceptance_areas():
    keys = [key for key, _ in runner.CHECKS]
    assert len(keys) >= 30
    assert len(keys) == len(set(keys))
    assert {'poly_settlement', 'forced_worker', 'outage', 'accounting'} <= set(keys)
