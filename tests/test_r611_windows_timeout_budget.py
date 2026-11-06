"""Windows subprocess timeouts must be consistent across both launch layers."""

import inspect

from tools import qa_budgets, verify_windows, windows_check


def test_timeout_definitions_are_shared_and_budgeted():
    assert windows_check.WINDOWS_QA_TIMEOUTS is qa_budgets.WINDOWS_QA_TIMEOUTS
    assert verify_windows.WINDOWS_QA_TIMEOUTS is qa_budgets.WINDOWS_QA_TIMEOUTS
    assert set(qa_budgets.WINDOWS_QA_TIMEOUTS) == {
        "collection", "tests", "gui", "public_sources"
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
