"""One Windows check: isolated dependencies, native checks, app, report ZIP."""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import traceback
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Direct execution (python tools/windows_check.py) sets sys.path[0] to tools/,
# not to the package root. Never depend on PYTHONPATH or the caller's CWD.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.process_guard import run_logged_command
from tools.qa_budgets import WINDOWS_QA_TIMEOUTS, AUTOMATED_QA_TIMEOUT
from bcc.runtime_paths import python_environment


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run_step(name, command, output, *, timeout=None):
    print(name + "...", flush=True)
    started = time.monotonic()
    with (output / (name + ".log")).open("w", encoding="utf-8") as stream:
        try:
            code = run_logged_command(
                command, cwd=ROOT, stdout=stream, timeout=timeout, env=python_environment()
            )
            return dict(
                step=name,
                exit_code=code,
                seconds=round(time.monotonic() - started, 3),
            )
        except subprocess.TimeoutExpired:
            return dict(step=name, exit_code=None, error="Timeout")
        except OSError as exc:
            return dict(step=name, exit_code=None, error=f'{type(exc).__name__}: {exc}')


def report_archive(output, destination):
    # Explicit inputs: never sweep the installation, old bots, venv or credentials.
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file() and not path.is_symlink():
                archive.write(
                    path, output.name + "/" + path.relative_to(output).as_posix()
                )
    return destination


def automated_step_passed(step):
    """A nonzero or timed-out automation must never launch manual PAPER."""
    return step.get("exit_code") == 0 and not step.get("error")


def acceptance_status(automated_ok, manual_launched):
    if not automated_ok:
        return "AUTOMATED_FAILED"
    return "AUTOMATED_PASS_MANUAL_PENDING" if manual_launched else "AUTOMATED_PASS_MANUAL_NOT_RUN"


def manual_reopen_requested():
    """Keep the owned session alive while checking an actual Qt restart."""
    if not sys.stdin.isatty():
        return False
    try:
        return input('R — reopen the dashboard with bots still running; Enter — finish and stop this QA session: ').strip().lower() == 'r'
    except (EOFError, OSError):
        return False


def verify_source_snapshot():
    """Run the same package-integrity check used by the Windows launcher."""
    from tools.verify_package import verify
    return verify(ROOT)


def main(argv=None):
    # Python -I ignores PYTHONIOENCODING; a cp1252 Windows CI console cannot
    # print a Cyrillic archive path by default.  Preserve the report and exit
    # code even when the caller's console encoding is not Unicode-capable.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='backslashreplace')
    parser = argparse.ArgumentParser()
    parser.add_argument("--automated-only", action="store_true")
    parser.add_argument(
        "--venue", choices=("all", "bybit"), default="all",
        help="Bybit phase is not overall BCC release acceptance"
    )
    parser.add_argument(
        "--preflight-only", action="store_true",
        help="Verify sealed sources without implying Windows Qt or PAPER approval",
    )
    args = parser.parse_args(argv)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    output = ROOT / "verification_reports" / ("check_" + stamp)
    output.mkdir(parents=True)
    archive = ROOT / ("BCC_VALIDATION_RESULTS_" + stamp + ".zip")
    workspace = ROOT / "check_workspaces" / stamp
    report = dict(
        started=datetime.now().astimezone().isoformat(),
        platform=platform.platform(),
        python=sys.version,
        steps=[],
        venue_scope=args.venue,
        deferred_venues=(["polymarket", "binance"] if args.venue == "bybit" else []),
        release_ready=False,
        manual_acceptance="NOT_CONFIRMED",
        workspace=str(workspace),
    )
    exit_code = 1
    python = None
    app_started = False
    try:
        if args.preflight_only:
            report["source_integrity"] = verify_source_snapshot()
            report["status"] = "SOURCE_INTEGRITY_PASS_ONLY"
            return 0
        if os.name != "nt":
            report.update(status="BLOCKED", error="Native Windows is required")
            return 2
        if sys.version_info < (3, 10):
            raise RuntimeError("Python 3.10+ is required; Python 3.12 is recommended")
        from tools.verify_package import SourceIntegrityError
        try:
            report["source_integrity"] = verify_source_snapshot()
        except SourceIntegrityError as exc:
            report["source_integrity"] = {"status": "FAIL", "error": str(exc)}
            raise RuntimeError("Повреждены исходники проверочного пакета: " + str(exc)) from exc
        os.environ["QT_QPA_PLATFORM"] = "windows"
        venv = ROOT / ".check_venv"
        step = run_step(
            "environment",
            [sys.executable, "-m", "venv", str(venv)],
            output,
            timeout=120,
        )
        report["steps"].append(step)
        if step["exit_code"] != 0:
            raise RuntimeError(
                "Cannot create isolated Python environment; see environment.log"
            )
        python = venv / "Scripts" / "python.exe"
        step = run_step(
            "dependencies",
            [
                str(python),
                "-m",
                "pip",
                "install",
                "-r",
                str(ROOT / "requirements-dev.txt"),
            ],
            output,
            timeout=900,
        )
        report["steps"].append(step)
        if step["exit_code"] != 0:
            raise RuntimeError("Dependency installation failed; see dependencies.log")
        step = run_step(
            "automated",
            [
                str(python),
                "tools/verify_windows.py",
                "--output",
                str(output / "automated"),
                "--venue", args.venue,
            ],
            output,
            timeout=AUTOMATED_QA_TIMEOUT,
        )
        report["steps"].append(step)
        print(
            "Automated checks: "
            + (
                "PASS"
                if step["exit_code"] == 0
                else "FAILED; logs are included in the report"
            ),
            flush=True,
        )
        if automated_step_passed(step) and not args.automated_only:
            from tools.verify_package import assert_same_snapshot
            assert_same_snapshot(report['source_integrity'], verify_source_snapshot())
            print(
                "\nOpening PAPER app in an EMPTY, separate check workspace.", flush=True
            )
            print(
                "Follow WINDOWS_CHECK_RU.md. Close the app to collect the report.",
                flush=True,
            )
            print(
                "Bots of this check will then be stopped; their state is preserved.",
                flush=True,
            )
            (output / "OBSERVATIONS.txt").write_text(
                "Опишите, что проверили и что не сработало. Этот файл можно дополнить перед отправкой отчёта.\n",
                encoding="utf-8",
            )
            from tools.acceptance_session import AcceptanceSession
            with AcceptanceSession(workspace, output / 'manual_session', python=python):
                app_started = True
                window_number = 0
                while True:
                    window_number += 1
                    assert_same_snapshot(report['source_integrity'], verify_source_snapshot())
                    step = run_step(
                        'application' if window_number == 1 else f'application_{window_number}',
                        [str(python), "app.py", "--data-root", str(workspace)],
                        output,
                    )
                    report["steps"].append(step)
                    assert_same_snapshot(report['source_integrity'], verify_source_snapshot())
                    if not automated_step_passed(step) or not manual_reopen_requested():
                        break
        report["status"] = acceptance_status(
            all(automated_step_passed(s) for s in report["steps"]),
            app_started,
        )
        if args.venue == "bybit" and report["status"].startswith("AUTOMATED_PASS_"):
            report["status"] = "BYBIT_PHASE_" + report["status"]
        # Bybit-only acceptance cannot confer a release claim on the
        # deferred Polymarket/Binance venue modules.
        exit_code = 0 if report["status"].startswith(("AUTOMATED_PASS_", "BYBIT_PHASE_AUTOMATED_PASS_")) else 1
    except BaseException as exc:
        report.update(status="ERROR", error=str(exc))
        (output / "checker_error.log").write_text(
            traceback.format_exc(), encoding="utf-8"
        )
        print(str(exc), flush=True)
    finally:
        if app_started and python is not None:
            try:
                step = run_step(
                    "collect_bots",
                    [
                        str(python),
                        "-m",
                        "bcc.support",
                        "--root",
                        str(workspace),
                        "--output",
                        str(output / "paper_bots"),
                    ],
                    output,
                    timeout=90,
                )
                report["steps"].append(step)
                if step["exit_code"] != 0:
                    report["status"] = "BOT_OR_COLLECTION_ERRORS"
                    exit_code = 1
            except BaseException:
                report["collection_error"] = traceback.format_exc()
                exit_code = 1
        if not args.preflight_only and report.get('source_integrity', {}).get('status') == 'PASS':
            try:
                from tools.verify_package import assert_same_snapshot
                report['source_integrity_final'] = assert_same_snapshot(
                    report['source_integrity'], verify_source_snapshot())
            except BaseException as exc:
                report['source_integrity_final'] = dict(status='FAIL', error=str(exc))
                report['status'] = 'SOURCE_INTEGRITY_CHANGED'
                exit_code = 1
        report["finished"] = datetime.now().astimezone().isoformat()
        write_json(output / "check_summary.json", report)
        report_archive(output, archive)
        print("\nREPORT: " + str(archive), flush=True)
        print("Send this ZIP back. This check does not certify a release.", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
