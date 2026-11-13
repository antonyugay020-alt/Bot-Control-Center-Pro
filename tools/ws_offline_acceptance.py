"""Local *isolated-checkout only* offline WebSocket acceptance gate.

No GitHub Actions, no broker credentials, no orders, no changes to a running
MY_BOTS tree. Tests are selected from the checked-out repository, never copied
from a live working bot directory. This is NOT native Windows GUI acceptance.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
SOURCES = (
    "bcc/ws_quotes.py", "bcc/ws_orderbooks.py", "bcc/ws_klines.py",
    "bcc/ws_polymarket.py", "bcc/ws_binance.py", "bcc/ws_dispatch.py",
    "bcc/market.py", "bcc/market_hub.py", "bcc/data_cache.py",
    "bcc/hub_supervisor.py", "tools/market_hub_service.py",
)
NETWORK_FLAGS = (
    "BCC_BYBIT_WS_QUOTES", "BCC_BYBIT_WS_BOOKS", "BCC_BYBIT_WS_KLINES",
    "BCC_POLY_WS_BOOKS", "BCC_BINANCE_WS_PRICE",
)


def require_isolated_checkout():
    if not (ROOT / ".git").exists():
        raise RuntimeError("QA requires an independent Git checkout")
    if (ROOT / "MY_BOTS").exists():
        raise RuntimeError("QA refuses any checkout containing MY_BOTS")
    if (ROOT / "market_cache.sqlite").exists():
        raise RuntimeError("QA refuses a checkout containing live market cache")
    if os.environ.get("BCC_MARKET_HUB_MODE") == "client":
        raise RuntimeError("QA cannot run from a live Hub client session")
    if os.environ.get("BCC_PUBLIC_CACHE"):
        raise RuntimeError("QA cannot inherit a live BCC_PUBLIC_CACHE")


def main(argv=None):
    cli = argparse.ArgumentParser()
    cli.add_argument("--repeat", type=int, default=1,
                     help="number of offline regression repetitions 1..150")
    cli.add_argument("--full-regression", action="store_true",
                     help="run all repository Python tests in addition to WS tests")
    cli.add_argument("--report", default="WS_QA_RESULT.json")
    args = cli.parse_args(argv)
    if not 1 <= args.repeat <= 150:
        cli.error("--repeat must be 1..150")
    report = Path(args.report)
    if not report.is_absolute():
        report = ROOT / report
    if ROOT not in report.resolve().parents:
        cli.error("--report must be within the isolated checkout")
    started = time.time()
    result = {
        "mode": "offline-isolated", "started_at": started,
        "repeat": args.repeat, "full_regression": args.full_regression,
        "compiled": [], "executions": [],
        "release_status": "BLOCKED_NATIVE_WINDOWS_QT_E2E_UNRUN",
    }
    try:
        require_isolated_checkout()
        for relative in SOURCES:
            filename = ROOT / relative
            ast.parse(filename.read_text(encoding="utf-8"), filename=str(filename))
            result["compiled"].append(relative)
        tests = sorted(str(path.relative_to(ROOT)) for path in
                       (ROOT / "tests").glob("test_ws_*.py"))
        if not tests:
            raise RuntimeError("No WebSocket regression tests in this checkout")
        with tempfile.TemporaryDirectory(prefix="bcc_ws_qa_") as temp:
            env = dict(os.environ)
            for flag in NETWORK_FLAGS:
                env.pop(flag, None)
            env.pop("BCC_MARKET_HUB_MODE", None)
            env.pop("BCC_PUBLIC_CACHE", None)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["BCC_QA_TEMP"] = temp
            for cycle in range(args.repeat):
                cmd = [sys.executable, "-m", "pytest", "-q", *tests]
                process = subprocess.run(
                    cmd, cwd=str(ROOT), env=env, capture_output=True,
                    text=True, errors="replace", timeout=1800,
                )
                result["executions"].append({
                    "kind": "websocket", "iteration": cycle + 1,
                    "returncode": process.returncode,
                    "tail": (process.stdout + "\n" + process.stderr)[-8000:],
                })
                if process.returncode:
                    raise RuntimeError("WebSocket regressions failed")
            if args.full_regression:
                process = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "tests"],
                    cwd=str(ROOT), env=env, capture_output=True,
                    text=True, errors="replace", timeout=3600,
                )
                result["executions"].append({
                    "kind": "full-python", "returncode": process.returncode,
                    "tail": (process.stdout + "\n" + process.stderr)[-8000:],
                })
                if process.returncode:
                    raise RuntimeError("Full repository regressions failed")
        result["python_status"] = "PASS"
    except Exception as exc:
        result["python_status"] = "BLOCKED"
        result["blocker"] = type(exc).__name__ + ": " + str(exc)
    result["elapsed_seconds"] = round(time.time() - started, 2)
    # One final result in this isolated checkout. Never package a release or
    # claim GUI acceptance from Python-only checks.
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(report)
    print(result["python_status"], result["release_status"])
    return 0 if result["python_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
