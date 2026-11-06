"""Bounded, evidence-checked clean-unpack regression runs.

This is development QA, not a release-certification tool. Native Windows Qt
E2E and live-data PAPER remain independent mandatory gates. Never infer success
from the last line of pytest output: require process exit 0 AND valid JUnit.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.process_guard import run_logged_command
from tools.verify_package import verify
from tools.verify_windows import read_test_totals

# Explicit: Qt test files are not silently skipped on Linux. Their absence is
# recorded as an unresolved native Windows obligation in every summary.
LINUX_QT_FILES = (
    "test_qt.py", "test_poly_indicator_qt.py",
    "test_veles_hierarchy_qt.py", "test_wizard_visual_qt.py",
    "test_settings_tree_contrast.py", "test_native_dashboard_workflow_qt.py",
)


def assess_result(exit_code, junit_path, *, minimum=500, expected=None):
    if exit_code != 0:
        return {"status": "FAIL", "reason": f"pytest did not exit 0: {exit_code}"}
    try:
        totals = read_test_totals(junit_path)
    except Exception as exc:
        # XML errors, malformed declarations and absent JUnit all block PASS.
        return {"status": "FAIL", "reason": "JUnit unreadable: " + str(exc)}
    if (totals["tests"] < minimum or totals["failures"] or totals["errors"]
            or totals["skipped"] or (expected is not None and totals["tests"] != expected)):
        return {"status": "FAIL", "reason": "JUnit totals invalid", "totals": totals}
    return {"status": "PASS", "totals": totals}


def clean_copy(destination):
    manifest_path = ROOT / "SHA256_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    destination.mkdir(parents=True)
    for name in manifest["files"]:
        source = ROOT / name
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    shutil.copyfile(manifest_path, destination / manifest_path.name)
    verify(destination)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Full clean-copy QA, never release certification")
    ap.add_argument("--runs", type=int, default=30)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args(argv)
    if not 1 <= args.runs <= 100 or not 5 <= args.timeout <= 1800:
        ap.error("Invalid runs/timeout")
    source_integrity = verify(ROOT)
    output = args.output.resolve()
    if output == ROOT or ROOT in output.parents or output in ROOT.parents:
        ap.error("QA evidence must be kept outside source tree")
    if output.exists() and any(output.iterdir()):
        ap.error("QA evidence directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "started": datetime.now(timezone.utc).isoformat(),
        "checkpoint": source_integrity["checkpoint"],
        "source_integrity": source_integrity,
        "requested_runs": args.runs,
        "runs": [], "status": "NOT_COMPLETED",
        "native_windows_qt_e2e": "NOT_PROVEN_BY_THIS_RUNNER",
        "fresh_market_paper": "NOT_PROVEN_BY_THIS_RUNNER",
        "release_ready": False,
    }
    expected = None
    try:
        for index in range(1, args.runs + 1):
            attempt = output / f"run_{index:02d}"
            attempt.mkdir()
            copied = attempt / "unpacked"
            clean_copy(copied)
            junit = attempt / "results.xml"
            log = attempt / "pytest.log"
            env = os.environ.copy()
            env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["BCC_TEST_ARTIFACTS"] = str(attempt / "process_checks")
            # Strictly distinguish Linux-only headless coverage from native Qt.
            options = [] if os.name == "nt" else [
                f"--ignore=tests/{name}" for name in LINUX_QT_FILES
            ]
            cmd = [sys.executable, "-X", "faulthandler", "-m", "pytest", "-q", "tests",
                   *options, "--junitxml=" + str(junit)]
            began = time.monotonic()
            with log.open("w", encoding="utf-8") as stream:
                try:
                    code = run_logged_command(cmd, cwd=copied, stdout=stream,
                                              timeout=args.timeout, env=env)
                    outcome = assess_result(code, junit, expected=expected)
                except subprocess.TimeoutExpired:
                    outcome = {"status": "FAIL", "reason": "QA subprocess timeout; tree killed"}
                except (OSError, ValueError) as exc:
                    outcome = {"status": "FAIL", "reason": f"QA execution error: {exc}"}
            outcome.update(run=index, duration_seconds=round(time.monotonic()-began, 2),
                           log=str(log.relative_to(output)),
                           junit=str(junit.relative_to(output)))
            report["runs"].append(outcome)
            if outcome["status"] == "PASS":
                expected = outcome["totals"]["tests"]
            report["status"] = "RUNNING" if outcome["status"] == "PASS" else "FAIL"
            (output / "QA_SUMMARY.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            if outcome["status"] != "PASS":
                return 1
        report["status"] = "HEADLESS_REPEAT_PASS" if os.name != "nt" else "TEST_REPEAT_PASS_WINDOWS_QT_UNCERTIFIED"
        return 0
    finally:
        report["finished"] = datetime.now(timezone.utc).isoformat()
        (output / "QA_SUMMARY.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

if __name__ == "__main__":
    raise SystemExit(main())
