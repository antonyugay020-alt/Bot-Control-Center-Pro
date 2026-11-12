"""The OS replay process suite must not inherit unrelated shard state."""
import json
from tools import run_partitioned_tests as runner


def test_process_replay_gets_its_own_bounded_shard(tmp_path, monkeypatch):
    root = tmp_path / 'src'
    tests = root / 'tests'
    tests.mkdir(parents=True)
    files = ('test_alpha.py', 'test_beta.py', 'test_process.py',
             'test_veles_resource_scaled_accounts_replay.py')
    for filename in files:
        (tests / filename).write_text('def test_ok(): pass\n', encoding='utf-8')
    inventory = tmp_path / 'inventory.json'
    inventory.write_text(json.dumps({'schema': 1, 'cases': [
        ['tests.' + filename[:-3], 'test_ok'] for filename in files
    ]}), encoding='utf-8')
    monkeypatch.setattr(runner, 'ROOT', root)
    shards = runner.partitions(inventory, groups=2)
    assert shards[0] == ['test_process.py']
    assert shards[-1] == ['test_veles_resource_scaled_accounts_replay.py']
    assert sorted(file for shard in shards for file in shard) == sorted(files)
    assert all('test_process.py' not in shard for shard in shards[1:])


def test_headless_partition_accepts_full_native_inventory_but_schedules_no_qt(tmp_path, monkeypatch):
    root = tmp_path / 'src'
    tests = root / 'tests'
    tests.mkdir(parents=True)
    for name in ('test_core.py', 'test_qt.py', 'test_process.py'):
        (tests / name).write_text('def test_ok(): pass\n', encoding='utf-8')
    inv = tmp_path / 'inventory.json'
    inv.write_text(json.dumps({'schema': 1, 'cases': [
        ['tests.test_core', 'test_ok'], ['tests.test_qt', 'test_ok'],
        ['tests.test_process', 'test_ok']
    ]}), encoding='utf-8')
    monkeypatch.setattr(runner, 'ROOT', root)
    with_qt = runner.partitions(inv, groups=2)
    assert sorted(sum(with_qt, [])) == ['test_core.py', 'test_process.py', 'test_qt.py']
    without_qt = runner.partitions(inv, groups=2, excluded={'test_qt.py'})
    assert sorted(sum(without_qt, [])) == ['test_core.py', 'test_process.py']
    assert without_qt[0] == ['test_process.py']
