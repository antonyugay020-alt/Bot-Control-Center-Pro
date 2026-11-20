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


def test_automatic_report_has_evidence_mapping_for_every_criterion():
    from pathlib import Path
    from tools.acceptance_auto_report import RELATED
    assert set(RELATED) == {key for key, _ in runner.CHECKS}
    for modules in RELATED.values():
        assert modules
        assert all((Path(__file__).parent / (module + '.py')).is_file() for module in modules)


def test_windows_entrypoint_runs_unattended_report_as_module():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / 'tools' / 'run_current_windows_qa.ps1').read_text(encoding='utf8')
    assert '-m tools.auto_qa_current' in text
    assert 'manual_qa_current.py' not in text


def test_automatic_report_never_promotes_missing_or_mismatched_evidence(tmp_path):
    from tools.acceptance_auto_report import build_report, render_report
    result = build_report(tmp_path, runner.CHECKS, 'a' * 64, [])
    assert result['receipt_errors'] and not result['release_ready']
    assert len(result['criteria']) == 36
    assert all(x['automated_result'] == 'RELATED_TESTS_NOT_CONFIRMED'
               and x['live_acceptance'] == 'NOT_CONFIRMED' for x in result['criteria'])
    assert 'НЕ ПОДТВЕРЖДЕНЫ' in render_report(result)


def test_automatic_report_rejects_failing_junit_even_with_claimed_pass(tmp_path, monkeypatch):
    from tools import verify_windows
    from tools.acceptance_auto_report import build_report
    base = tmp_path / 'automated_qa' / 'automated'
    base.mkdir(parents=True)
    manifest = 'a' * 64
    report = {'status': 'AUTOMATED_CHECKS_PASS',
              'source_integrity': {'manifest_sha256': manifest},
              'source_integrity_final': {'manifest_sha256': manifest}}
    (base / 'windows_report.json').write_text(json.dumps(report), encoding='utf8')
    (base / 'collected_tests.json').write_text('{}', encoding='utf8')
    (base / 'tests.xml').write_text(
        '<testsuites><testsuite><testcase classname="tests.test_strategies" name="test_x">'
        '<failure>failure</failure></testcase></testsuite></testsuites>', encoding='utf8')
    monkeypatch.setattr(verify_windows, 'native_test_evidence_problems', lambda *_: [])
    monkeypatch.setattr(verify_windows, 'verify_complete_test_inventory', lambda *_: 1)
    result = build_report(tmp_path, runner.CHECKS, manifest, [])
    assert result['receipt_errors']
    assert all(x['automated_result'] == 'RELATED_TESTS_NOT_CONFIRMED' for x in result['criteria'])


def test_passing_related_tests_never_approve_live_market_scenario(tmp_path, monkeypatch):
    from tools import verify_windows
    from tools.acceptance_auto_report import build_report
    base = tmp_path / 'automated_qa' / 'automated'
    base.mkdir(parents=True)
    manifest = 'a' * 64
    (base / 'windows_report.json').write_text(json.dumps({
        'status': 'AUTOMATED_CHECKS_PASS',
        'source_integrity': {'manifest_sha256': manifest},
        'source_integrity_final': {'manifest_sha256': manifest},
    }), encoding='utf8')
    (base / 'collected_tests.json').write_text('{}', encoding='utf8')
    (base / 'tests.xml').write_text(
        '<testsuites><testsuite><testcase classname="tests.test_strategies" name="test_grid"/>'
        '<testcase classname="tests.test_grid_preview_model" name="test_preview"/>'
        '</testsuite></testsuites>', encoding='utf8')
    monkeypatch.setattr(verify_windows, 'native_test_evidence_problems', lambda *_: [])
    monkeypatch.setattr(verify_windows, 'verify_complete_test_inventory', lambda *_: 2)
    result = build_report(tmp_path, runner.CHECKS, manifest, [])
    grid = next(row for row in result['criteria'] if row['id'] == 'grid_levels')
    assert grid['automated_result'] == 'RELATED_TESTS_PASSED'
    assert grid['live_acceptance'] == 'NOT_CONFIRMED' and not result['release_ready']


def test_automatic_report_summarises_only_owned_snapshot(tmp_path):
    import zipfile
    from tools.acceptance_auto_report import build_report
    payload = {'bots': [{'active_process_confirmed': True, 'open_positions': 3,
                         'ledger': 'AUDIT_PASS', 'execution_freshness': {'status': 'PASS'}}]}
    with zipfile.ZipFile(tmp_path / 'snapshot.zip', 'w') as z:
        z.writestr('current_bots_report/paper_bots/collection.json', json.dumps(payload))
    result = build_report(tmp_path, runner.CHECKS, 'a' * 64,
                          [{'file': 'snapshot.zip'}, {'file': '../outside.zip'}])
    assert result['snapshots'] == [{'file': 'snapshot.zip', 'bots': 1, 'open_positions': 3,
                                    'running_workers': 1, 'ledger_statuses': ['AUDIT_PASS'],
                                    'execution_freshness': ['PASS']}]
    assert all(row['live_acceptance'] == 'NOT_CONFIRMED' for row in result['criteria'])


def test_unattended_bundle_contains_report_and_excludes_live_cache(tmp_path, monkeypatch):
    import zipfile
    from tools import auto_qa_current, verify_windows
    manifest = 'a' * 64
    seal = {'status': 'PASS', 'manifest_sha256': manifest}
    monkeypatch.setattr(auto_qa_current, 'verify', lambda *_: seal)
    monkeypatch.setattr(verify_windows, 'native_test_evidence_problems', lambda *_: [])
    monkeypatch.setattr(verify_windows, 'verify_complete_test_inventory', lambda *_: 1)
    def copy_receipt(root, output):
        base = output / 'automated_qa' / 'automated'
        base.mkdir(parents=True)
        (base / 'windows_report.json').write_text(json.dumps({
            'status': 'AUTOMATED_CHECKS_PASS',
            'source_integrity': {'manifest_sha256': manifest},
            'source_integrity_final': {'manifest_sha256': manifest},
        }), encoding='utf8')
        (base / 'collected_tests.json').write_text('{}', encoding='utf8')
        (base / 'tests.xml').write_text(
            '<testsuites><testsuite><testcase classname="tests.test_strategies" name="test_grid"/>'
            '</testsuite></testsuites>', encoding='utf8')
        return {'reported_status': 'AUTOMATED_PASS_MANUAL_NOT_RUN'}
    monkeypatch.setattr(auto_qa_current.manual_qa_current, 'copy_existing_automated_evidence', copy_receipt)
    def fake_live(root, output, seal):
        hub = output / 'bybit_ws' / 'isolated_hub'
        hub.mkdir(parents=True)
        (hub / 'market_cache.sqlite').write_bytes(b'private runtime cache')
        (hub.parent / 'live_ws_report.json').write_text('{}', encoding='utf8')
        return {'status': 'PASS', 'evidence': 'bybit_ws/live_ws_report.json'}
    archive, report = auto_qa_current.create_report(
        tmp_path, SHA, seal, live_check=fake_live,
        paper_check=lambda *_: {'status': 'NOT_CONFIRMED'})
    assert archive.parent == tmp_path and archive.name.startswith('BCC_TEST_REPORT_AUTOMATED_')
    assert len(report['criteria']) == 36 and not report['release_ready']
    assert all(row['live_acceptance'] == 'NOT_CONFIRMED' for row in report['criteria'])
    with zipfile.ZipFile(archive) as z:
        assert {'AUTOMATED_ACCEPTANCE_REPORT.json', 'AUTOMATED_ACCEPTANCE_REPORT_RU.txt',
                'bybit_ws/live_ws_report.json'} <= set(z.namelist())
        assert not any('isolated_hub' in name for name in z.namelist())


def test_unattended_bundle_does_not_probe_exchange_after_failed_gate(tmp_path, monkeypatch):
    from tools import auto_qa_current
    seal = {'status': 'PASS', 'manifest_sha256': 'a' * 64}
    monkeypatch.setattr(auto_qa_current, 'verify', lambda *_: seal)
    monkeypatch.setattr(auto_qa_current.manual_qa_current, 'copy_existing_automated_evidence',
                        lambda *_: {'reported_status': 'AUTOMATED_FAILED'})
    def forbidden(*args):
        pytest.fail('A failed safety gate must not initiate exchange calls')
    archive, report = auto_qa_current.create_report(tmp_path, SHA, seal,
                                                     live_check=forbidden, paper_check=forbidden)
    assert archive.is_file() and report['public_bybit_ws']['status'] == 'NOT_RUN'
    assert report['receipt_errors'] and not report['release_ready']


def test_unattended_bundle_does_not_recontact_provider_after_failed_ws(tmp_path, monkeypatch):
    from tools import auto_qa_current
    seal = {'status': 'PASS', 'manifest_sha256': 'a' * 64}
    monkeypatch.setattr(auto_qa_current, 'verify', lambda *_: seal)
    monkeypatch.setattr(auto_qa_current.manual_qa_current, 'copy_existing_automated_evidence',
                        lambda *_: {'reported_status': 'AUTOMATED_PASS_MANUAL_NOT_RUN'})
    def forbidden(*args):
        pytest.fail('A WS failure may be a provider fence; do not recontact via a new cache')
    archive, report = auto_qa_current.create_report(
        tmp_path, SHA, seal, live_check=lambda *_: {'status': 'FAIL'}, paper_check=forbidden)
    assert archive.is_file() and report['public_paper_cycle']['status'] == 'NOT_RUN'
