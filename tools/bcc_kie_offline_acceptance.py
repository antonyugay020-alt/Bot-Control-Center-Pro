"""One comprehensive OFFLINE Kie.ai BCC development-agent regression.

This process neither knows user API keys nor permits sockets/subprocesses.
It discovers the complete Kie-specific unittest suite, not a handpicked
single-test subset. Runs on an isolated checkout, not a live BCC runtime.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TEST_FILES = frozenset({
    "test_bcc_kie_agent.py",
    "test_bcc_kie_approval.py",
    "test_bcc_kie_credentials.py",
    "test_bcc_kie_credentials_gui.py",
    "test_bcc_kie_https.py",
    "test_bcc_kie_ledger_review.py",
    "test_bcc_kie_model_budget.py",
    "test_bcc_kie_model_pipeline.py",
    "test_bcc_kie_models.py",
    "test_bcc_kie_one_shot.py",
    "test_bcc_kie_protocol.py",
    "test_bcc_kie_responses_https.py",
    "test_bcc_kie_responses_protocol.py",
    "test_bcc_kie_task_router.py",
})
MIN_TEST_COUNT = 186  # Baseline inventory 2026-11-18: 14 modules, 186 test methods.
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["PYTHONNOUSERSITE"] = "1"
sys.dont_write_bytecode = True


class OfflineViolation(RuntimeError):
    """No secrets, URLs or raw test arguments are echoed."""


def _deny(*_args, **_kwargs):
    raise OfflineViolation("OFFLINE_QA_PROHIBITED_NETWORK_OR_SUBPROCESS")


def _emit(result: str, **facts: object) -> int:
    print("BCC_KIE_OFFLINE_RESULT=" + json.dumps({
        "result": result, "platform": platform.system(),
        "python": platform.python_version(),
        "network_and_subprocesses": "DENIED_IN_TEST_PROCESS",
        "provider_calls": 0, "github_actions": 0, **facts,
    }, sort_keys=True), flush=True)
    return 0 if result == "PASS" else 1


def main() -> int:
    found = {
        file.name for file in (ROOT / "tests").glob("test_bcc_kie_*.py")
        if file.is_file()
    }
    if found != EXPECTED_TEST_FILES:
        return _emit("BLOCKED_INVENTORY", expected=len(EXPECTED_TEST_FILES),
                     found=len(found), unexpected=sorted(found-EXPECTED_TEST_FILES),
                     missing=sorted(EXPECTED_TEST_FILES-found))

    # The script itself was launched in -I mode. Explicitly include only
    # the isolated checkout for the unittest modules and project tools.
    sys.path.insert(0, str(ROOT))
    # Discover, import and run entirely under deny-by-default sockets and
    # subprocesses. No untrusted shell commands or HTTP calls can start here.
    with ExitStack() as stack:
        stack.enter_context(patch.object(socket, "create_connection", _deny))
        stack.enter_context(patch.object(socket, "getaddrinfo", _deny))
        stack.enter_context(patch.object(socket.socket, "connect", _deny))
        stack.enter_context(patch.object(socket.socket, "connect_ex", _deny))
        stack.enter_context(patch.object(subprocess, "Popen", _deny))
        try:
            loader = unittest.defaultTestLoader
            suite = loader.discover(start_dir=str(ROOT / "tests"),
                                    pattern="test_bcc_kie_*.py",
                                    top_level_dir=str(ROOT / "tests"))
            count = suite.countTestCases()
            if count < MIN_TEST_COUNT:
                return _emit("BLOCKED_INCOMPLETE_SUITE",
                             discovered=count, minimum=MIN_TEST_COUNT)
            runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=1)
            result = runner.run(suite)
        except BaseException as e:
            # No exception repr: an imported fixture could contain a secret.
            return _emit("BLOCKED_EXECUTION", exception_type=type(e).__name__)
    if not result.wasSuccessful():
        return _emit("FAIL", tests=result.testsRun,
                     errors=len(result.errors),
                     failures=len(result.failures),
                     skipped=len(result.skipped))
    return _emit("PASS", tests=result.testsRun, errors=0, failures=0,
                 skipped=len(result.skipped))


if __name__ == "__main__":
    raise SystemExit(main())
