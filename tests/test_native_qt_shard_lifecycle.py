"""Native Qt and non-Qt integration inventories must not share a process."""
import json

from tools import run_partitioned_tests as runner


def test_qt_tests_have_isolated_native_lifetime_and_full_inventory(tmp_path, monkeypatch):
    root = tmp_path / 'src'
    tests = root / 'tests'
    tests.mkdir(parents=True)
    files = ('test_alpha.py', 'test_beta.py', 'test_qt.py',
             'test_constructor_review_isolation_qt.py',
             'test_settings_tree_contrast.py', 'test_process.py')
    for name in files:
        (tests / name).write_text('def test_ok(): pass\n', encoding='utf-8')
    inventory = tmp_path / 'inventory.json'
    inventory.write_text(json.dumps({'schema': 1, 'cases': [
        ['tests.' + name[:-3], 'test_ok'] for name in files
    ]}), encoding='utf-8')
    monkeypatch.setattr(runner, 'ROOT', root)
    groups = runner.partitions(inventory, groups=2)
    assert groups[0] == ['test_process.py']
    assert groups[-1] == sorted(['test_qt.py',
                                  'test_constructor_review_isolation_qt.py',
                                  'test_settings_tree_contrast.py'])
    assert sorted(x for group in groups for x in group) == sorted(files)
    assert len(groups[-1]) == 3
    assert all('test_alpha.py' not in g or 'test_qt.py' not in g for g in groups)
