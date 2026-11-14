"""Windows subprocess timeouts must be consistent across both launch layers."""

import inspect

from tools import qa_budgets, verify_windows, windows_check


def test_timeout_definitions_are_shared_and_budgeted():
    assert windows_check.WINDOWS_QA_TIMEOUTS is qa_budgets.WINDOWS_QA_TIMEOUTS
    assert verify_windows.WINDOWS_QA_TIMEOUTS is qa_budgets.WINDOWS_QA_TIMEOUTS
    assert set(qa_budgets.WINDOWS_QA_TIMEOUTS) == {
        "collection", "tests", "gui", "bybit_ws", "public_sources"
    }
    assert all(isinstance(t, int) and 0 < t < 3600
               for t in qa_budgets.WINDOWS_QA_TIMEOUTS.values())
    assert qa_budgets.AUTOMATED_QA_TIMEOUT >= (
        sum(qa_budgets.WINDOWS_QA_TIMEOUTS.values()) + 60
    )


def test_launchers_refer_to_shared_timeouts_not_stale_literals():
    source = inspect.getsource(verify_windows.main)
    for step in qa_budgets.WINDOWS_QA_TIMEOUTS:
        assert f'WINDOWS_QA_TIMEOUTS["{step}"]' in source
    launcher = inspect.getsource(windows_check.main)
    assert "timeout=AUTOMATED_QA_TIMEOUT" in launcher
    assert windows_check.automated_step_passed({
        "exit_code": None, "error": "Timeout"
    }) is False


def test_native_outer_budget_covers_all_scheduled_isolated_shards(tmp_path, monkeypatch):
    import json
    from tools import run_partitioned_tests as runner
    root = tmp_path / 'source'
    tests = root / 'tests'
    tests.mkdir(parents=True)
    modules = ['test_process.py', 'test_veles_resource_scaled_accounts_replay.py'] + [f'test_regular_{i}.py' for i in range(6)]
    for name in modules:
        (tests / name).write_text('def test_one(): pass\n', encoding='utf-8')
    inventory = tmp_path / 'inventory.json'
    inventory.write_text(json.dumps(dict(schema=1, cases=[['tests.' + name[:-3], 'test_one'] for name in modules])), encoding='utf-8')
    monkeypatch.setattr(runner, 'ROOT', root)
    schedule = runner.partitions(inventory)
    settings = inspect.signature(runner.run).parameters
    regular, heavy = settings['shard_timeout'].default, settings['heavy_timeout'].default
    required = sum(heavy if 'test_veles_resource_scaled_accounts_replay.py' in group else regular for group in schedule)
    assert len(schedule) == 8
    assert qa_budgets.WINDOWS_QA_TIMEOUTS['tests'] >= required + 60
