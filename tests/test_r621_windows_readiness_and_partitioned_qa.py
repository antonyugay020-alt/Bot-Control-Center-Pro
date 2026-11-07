"""R6.21 regression: child-authored readiness and complete native test coverage."""
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from bcc.hub_supervisor import ensure_hub
from tools import run_partitioned_tests


@pytest.mark.parametrize('permanent', [False, True])
def test_real_service_receipt_requires_live_lease_after_wal_contention(tmp_path, monkeypatch, permanent):
    """Retry transient probes; a receipt alone cannot certify readiness."""
    import bcc.hub_supervisor as supervisor
    from bcc.market_hub import HubRegistry
    original = HubRegistry._connect
    probes = []
    def no_parent_probe(self):
        if supervisor.os.getpid() == parent_pid and spawned:
            probes.append(1)
            if permanent or len(probes) <= 3:
                raise OSError('simulated WAL probe contention')
        return original(self)
    parent_pid = supervisor.os.getpid()
    spawned = []
    spawn = supervisor._spawn_hub
    def mark_spawn(*args, **kwargs):
        child = spawn(*args, **kwargs)
        spawned.append(child)
        return child
    monkeypatch.setattr(supervisor, '_spawn_hub', mark_spawn)
    monkeypatch.setattr(HubRegistry, '_connect', no_parent_probe)
    pid = None
    try:
        if permanent:
            with pytest.raises(supervisor.HubUnavailable, match='did not become ready'):
                ensure_hub(tmp_path, timeout=1)
            assert spawned[0].poll() is not None
            record = json.loads((tmp_path / 'market_hub_process.json').read_text(encoding='utf-8'))
            assert record['leader_owner'].startswith(str(record['pid']) + ':')
            # SQLite is unavailable to parent cleanup too. Keep the exact
            # owner witness until a later start can revoke it safely.
            monkeypatch.setattr(HubRegistry, '_connect', original)
            pid = ensure_hub(tmp_path, timeout=5)
            recovered = json.loads((tmp_path / 'market_hub_process.json').read_text(encoding='utf-8'))
            assert recovered['leader_owner'] != record['leader_owner']
        else:
            pid = ensure_hub(tmp_path, timeout=5)
            assert isinstance(pid, int) and pid > 0
            assert len(probes) >= 4
        assert not list(tmp_path.glob('.market_hub_ready_*.json'))
    finally:
        if pid:
            from bcc import process_os as psutil
            p = psutil.Process(pid)
            p.terminate()
            p.wait(5)


def test_partitions_require_complete_file_inventory(tmp_path, monkeypatch):
    root = tmp_path / 'src'
    (root / 'tests').mkdir(parents=True)
    for file in ('test_alpha.py', 'test_beta.py', 'test_veles_800_accounts_replay.py'):
        (root / 'tests' / file).write_text('pass', encoding='utf-8')
    monkeypatch.setattr(run_partitioned_tests, 'ROOT', root)
    inventory = tmp_path / 'inventory.json'
    inventory.write_text(json.dumps({'schema': 1, 'cases': [
        ['tests.test_alpha', 'test_one'],
        ['tests.test_beta', 'test_two'],
        ['tests.test_veles_800_accounts_replay', 'test_many'],
    ]}), encoding='utf-8')
    groups = run_partitioned_tests.partitions(inventory, groups=2)
    assert len(groups) == 3
    assert groups[-1] == ['test_veles_800_accounts_replay.py']
    assert sorted(sum(groups, [])) == ['test_alpha.py', 'test_beta.py', 'test_veles_800_accounts_replay.py']
    (root / 'tests' / 'test_missing.py').write_text('pass', encoding='utf-8')
    with pytest.raises(ValueError, match='differs'):
        run_partitioned_tests.partitions(inventory, groups=2)


def test_junit_merge_proves_all_cases_and_preserves_failures(tmp_path):
    parts = []
    for n, fail in enumerate([False, True]):
        root = ET.Element('testsuites')
        suite = ET.SubElement(root, 'testsuite', tests='1', failures=str(int(fail)), errors='0', skipped='0')
        testcase = ET.SubElement(suite, 'testcase', classname=f'tests.test_{n}', name=f'test_{n}')
        if fail:
            ET.SubElement(testcase, 'failure', message='simulated')
        path = tmp_path / f'part{n}.xml'
        ET.ElementTree(root).write(path)
        parts.append(path)
    target = tmp_path / 'combined.xml'
    totals = run_partitioned_tests.combine_junit(parts, target)
    assert totals == {'tests': 2, 'failures': 1, 'errors': 0, 'skipped': 0}
    assert len(ET.parse(target).getroot().findall('.//testcase')) == 2


def test_native_runner_uses_partitioned_full_inventory():
    import inspect
    from tools import verify_windows, qa_budgets
    source = inspect.getsource(verify_windows.main)
    assert 'tools.run_partitioned_tests' in source
    assert 'collected_tests.json' in source
    assert 'WINDOWS_QA_TIMEOUTS["tests"]' in source
    assert qa_budgets.WINDOWS_QA_TIMEOUTS['tests'] >= 6 * 360 + 600
