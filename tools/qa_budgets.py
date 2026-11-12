"""Single source of truth for native Windows QA time budgets (seconds).

The outer launcher must allow all inner stages to finish and record results,
including failures. These bounds do not relax per-test modal watchdogs.
"""

REGRESSION_GROUPS = 6
REGRESSION_SHARD_TIMEOUT = 360
RESOURCE_REPLAY_TIMEOUT = 600
REGRESSION_REPORT_MARGIN = 90

WINDOWS_QA_TIMEOUTS = {
    "collection": 180,
    # General groups plus the separately isolated OS-process suite, then
    # the resource replay. The outer watchdog must outlive every inner one.
    "tests": ((REGRESSION_GROUPS + 1) * REGRESSION_SHARD_TIMEOUT
              + RESOURCE_REPLAY_TIMEOUT + REGRESSION_REPORT_MARGIN),
    "gui": 120,
    "public_sources": 90,
}
AUTOMATED_QA_TIMEOUT = sum(WINDOWS_QA_TIMEOUTS.values()) + 90
