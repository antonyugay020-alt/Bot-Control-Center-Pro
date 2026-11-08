"""A passing exit code must never certify missing or forged shard results."""
import json
import xml.etree.ElementTree as ET

import pytest

from tools.run_partitioned_tests import validate_shard_junit


def evidence(tmp_path, *, names=('test_first', 'test_second'), missing=False,
             duplicate=False, skipped=False, unexpected=False):
    inventory = tmp_path / 'collected.json'
    inventory.write_text(json.dumps({'schema': 1, 'cases': [
        ['tests.test_alpha', name] for name in names
    ]}), encoding='utf-8')
    observed = list(names)
    if missing:
        observed.pop()
    if duplicate:
        observed.append(observed[0])
    if unexpected:
        observed.append('test_forged')
    root = ET.Element('testsuite', tests=str(len(observed)), failures='0',
                      errors='0', skipped=str(len(observed) if skipped else 0))
    for name in observed:
        case = ET.SubElement(root, 'testcase', classname='tests.test_alpha', name=name)
        if skipped:
            ET.SubElement(case, 'skipped')
    result = tmp_path / 'shard.xml'
    ET.ElementTree(root).write(result, encoding='utf-8')
    return result, inventory


def test_complete_shard_evidence_is_accepted(tmp_path):
    path, inventory = evidence(tmp_path)
    assert validate_shard_junit(path, inventory, ['test_alpha.py'], headless=True)['tests'] == 2


@pytest.mark.parametrize('tamper', ['missing', 'duplicate', 'unexpected'])
def test_shard_rejects_partial_or_forged_results(tmp_path, tamper):
    path, inventory = evidence(tmp_path, **{tamper: True})
    with pytest.raises(ValueError, match='inventory mismatch'):
        validate_shard_junit(path, inventory, ['test_alpha.py'], headless=True)


def test_shard_rejects_linux_skipped_results(tmp_path):
    path, inventory = evidence(tmp_path, skipped=True)
    with pytest.raises(ValueError, match='Skipped headless'):
        validate_shard_junit(path, inventory, ['test_alpha.py'], headless=True)


def test_shard_rejects_native_unapproved_skips(tmp_path):
    path, inventory = evidence(tmp_path, skipped=True)
    with pytest.raises(ValueError, match='Unexpected skipped native'):
        validate_shard_junit(path, inventory, ['test_alpha.py'], headless=False)


def test_shard_rejects_cross_module_assignment(tmp_path):
    path, inventory = evidence(tmp_path)
    with pytest.raises(ValueError, match='empty or duplicated'):
        validate_shard_junit(path, inventory, ['test_beta.py'], headless=True)


def test_pytest_passing_subtests_reconcile_only_with_matching_summary(tmp_path):
    from tools.run_partitioned_tests import reconcile_pytest_subtest_totals

    xml, inventory = evidence(tmp_path)
    log = tmp_path / 'shard.log'
    parsed = ET.parse(xml)
    parsed.getroot().set('tests', '5')
    parsed.write(xml, encoding='utf-8')
    log.write_text('==== 2 passed, 3 subtests passed in 1.2s ====\n', encoding='utf-8')
    assert reconcile_pytest_subtest_totals(xml, log) == 3
    assert validate_shard_junit(xml, inventory, ['test_alpha.py'], headless=True)['tests'] == 2


def test_subtest_header_mismatch_without_real_summary_remains_blocked(tmp_path):
    from tools.run_partitioned_tests import reconcile_pytest_subtest_totals

    xml, inventory = evidence(tmp_path)
    log = tmp_path / 'shard.log'
    parsed = ET.parse(xml)
    parsed.getroot().set('tests', '5')
    parsed.write(xml, encoding='utf-8')
    log.write_text('==== 2 passed, 1 subtests passed in 1.2s ====\n', encoding='utf-8')
    assert reconcile_pytest_subtest_totals(xml, log) == 0
    with pytest.raises(ValueError, match='testcase'):
        validate_shard_junit(xml, inventory, ['test_alpha.py'], headless=True)
