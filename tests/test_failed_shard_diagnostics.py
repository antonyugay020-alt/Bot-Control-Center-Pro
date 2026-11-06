"""All completed failed cases survive merging; acceptance still fails closed."""
import json
import xml.etree.ElementTree as ET

import pytest

from tools import run_partitioned_tests as runner


@pytest.mark.parametrize('result', ['failure', 'error', 'skipped', 'success'])
def test_complete_failure_keeps_merged_junit_and_cannot_run_as_pass(tmp_path, monkeypatch, result):
    inventory = tmp_path / 'inventory.json'
    cases = [['tests.test_alpha', 'test_alpha'], ['tests.test_beta', 'test_beta']]
    inventory.write_text(json.dumps(dict(schema=1, cases=cases)), encoding='utf-8')
    monkeypatch.setattr(runner, 'partitions', lambda *_a, **_k: [['test_alpha.py'], ['test_beta.py']])
    def command(arguments, **_options):
        module = 'alpha' if 'tests/test_alpha.py' in arguments else 'beta'
        status = result if module == 'alpha' else 'success'
        out = next(arg.split('=', 1)[1] for arg in arguments if arg.startswith('--junitxml='))
        suite = ET.Element('testsuite', tests='1', failures=str(int(status == 'failure')),
                           errors=str(int(status == 'error')), skipped=str(int(status == 'skipped')))
        case = ET.SubElement(suite, 'testcase', classname='tests.test_' + module, name='test_' + module)
        if status != 'success':
            ET.SubElement(case, status, message='Индикатор / Әліпби').text = 'Полная причина ошибки'
        ET.ElementTree(suite).write(out, encoding='utf-8')
        return 1 if status in ('failure', 'error') else 0
    monkeypatch.setattr(runner, 'run_logged_command', command)
    output = tmp_path / 'run/shards'
    assert runner.run(inventory, output, headless=True) == (0 if result == 'success' else 1)
    merged = ET.parse(output.parent / 'tests.xml').getroot()
    assert len(list(merged.iter('testcase'))) == 2
    if result != 'success':
        assert next(merged.iter(result)).get('message') == 'Индикатор / Әліпби'
    status = json.loads((output / 'status.json').read_text(encoding='utf-8'))
    assert status['status'] == ('PASS' if result == 'success' else 'FAIL')
    assert status['totals']['tests'] == status['inventory_count'] == 2
    assert status['shards'][0]['totals'] is not None


def test_incomplete_failed_shard_never_becomes_complete_merged_evidence(tmp_path, monkeypatch):
    inventory = tmp_path / 'inventory.json'
    inventory.write_text(json.dumps(dict(schema=1, cases=[['tests.test_alpha', 'test_alpha']])), encoding='utf-8')
    monkeypatch.setattr(runner, 'partitions', lambda *_a, **_k: [['test_alpha.py']])
    def command(arguments, **_options):
        out = next(arg.split('=', 1)[1] for arg in arguments if arg.startswith('--junitxml='))
        ET.ElementTree(ET.Element('testsuite', tests='0', failures='0', errors='0', skipped='0')).write(out, encoding='utf-8')
        return 1
    monkeypatch.setattr(runner, 'run_logged_command', command)
    output = tmp_path / 'run/shards'
    assert runner.run(inventory, output) == 1
    assert not (output.parent / 'tests.xml').exists()
