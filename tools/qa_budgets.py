"""Single source of truth for native Windows QA time budgets (seconds).

The outer launcher must allow all inner stages to finish and record results,
including failures. These bounds do not relax per-test modal watchdogs.
"""

WINDOWS_QA_TIMEOUTS = {
    "collection": 180,
    "tests": 3300,
    "gui": 120,
    "public_sources": 90,
}
AUTOMATED_QA_TIMEOUT = sum(WINDOWS_QA_TIMEOUTS.values()) + 90
