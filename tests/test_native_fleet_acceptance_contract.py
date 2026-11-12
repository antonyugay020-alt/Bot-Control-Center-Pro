"""Synthetic negative evidence checks, never a native acceptance claim."""
import xml.etree.ElementTree as ET
import subprocess
import sys
import pytest
from tools.verify_windows import REQUIRED_INTEGRATION_CASES, native_test_evidence_problems
from test_r65_windows_evidence_gate import fixture


@pytest.mark.parametrize('module,name', sorted(REQUIRED_INTEGRATION_CASES))
@pytest.mark.parametrize('fault', ['omitted', 'failed', 'skipped', 'wrong_identity'])
def test_each_native_strategy_and_cross_strategy_identity_must_execute(tmp_path, module, name, fault):
    fixture(tmp_path)
    path = tmp_path / 'tests.xml'
    tree = ET.parse(path)
    root = tree.getroot()
    target = next(case for case in root.findall('testcase')
                  if case.get('classname') == 'tests.' + module and case.get('name') == name)
    if fault == 'omitted':
        root.remove(target)
    elif fault in ('failed', 'skipped'):
        ET.SubElement(target, 'failure' if fault == 'failed' else 'skipped')
    else:
        target.set('name', name + '_UNVERIFIED')
    tree.write(path)
    assert any('Integrated fleet' in error and name in error for error in native_test_evidence_problems(path))


def test_required_integration_identities_are_real_collected_cases(tmp_path):
    from tools.collect_test_inventory import case_identity
    modules = sorted({module for module, _ in REQUIRED_INTEGRATION_CASES})
    result = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q',
                             *('tests/' + name + '.py' for name in modules)],
                            capture_output=True, text=True, timeout=60, check=True)
    actual = set()
    for line in result.stdout.splitlines():
        if line.startswith('tests/') and '::' in line:
            module, name = case_identity(line)
            actual.add((module.removeprefix('tests.'), name))
    assert REQUIRED_INTEGRATION_CASES <= actual


def test_resource_workloads_have_exact_completed_junit_inventory(tmp_path):
    import json
    from tools.collect_test_inventory import case_identity
    from tools.verify_windows import read_test_totals, verify_complete_test_inventory
    module = 'tests/test_strategy_dispatch.py'
    collected = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q', module],
                               capture_output=True, text=True, check=True, timeout=30)
    cases = [case_identity(line) for line in collected.stdout.splitlines()
             if line.startswith('tests/') and '::' in line]
    inventory, junit = tmp_path / 'inventory.json', tmp_path / 'results.xml'
    inventory.write_text(json.dumps(dict(schema=1, cases=cases)), encoding='utf-8')
    subprocess.run([sys.executable, '-m', 'pytest', '-q', module, '--junitxml=' + str(junit)],
                   capture_output=True, text=True, check=True, timeout=30)
    assert read_test_totals(junit) == dict(tests=len(cases), failures=0, errors=0, skipped=0)
    assert verify_complete_test_inventory(junit, inventory) == len(cases)
