"""Run native Windows checks and produce an evidence report, never a release claim."""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import struct
import zlib
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bcc import VERSION
from bcc.storage import atomic_json
from bcc.runtime_paths import python_environment
from tools.process_guard import run_logged_command
from tools.qa_budgets import WINDOWS_QA_TIMEOUTS


def source_fingerprint():
    digest = hashlib.sha256()
    for folder in (ROOT / "bcc", ROOT / "tests", ROOT / "tools"):
        for path in sorted(folder.rglob("*.py")):
            digest.update(path.relative_to(ROOT).as_posix().encode())
            digest.update(path.read_bytes())
    for name in (
        "app.py",
        "launch.pyw",
        "requirements.txt",
        "requirements-dev.txt",
        "START.bat",
        "INSTALL.bat",
        "VALIDATE_WINDOWS.bat",
        "CHECK_WINDOWS.bat",
        "tools/start_windows_check.ps1",
    ):
        digest.update(name.encode())
        digest.update((ROOT / name).read_bytes())
    return digest.hexdigest()


def run_step(name, arguments, output, timeout):
    print(name + "…", flush=True)
    log = output / (name + ".log")
    env = python_environment()
    env["PYTHONIOENCODING"] = "utf-8"
    if name == "tests":
        env["BCC_TEST_ARTIFACTS"] = str(output / "process_checks")
        # The project's tests rely on pytest core only; ambient machine-wide
        # plugins (instrumentation/reporters) can spawn background workers and
        # prevent the process from exiting after showing 100% passed. A native
        # acceptance runner must use a reproducible plugin environment.
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    with log.open("w", encoding="utf-8") as stream:
        try:
            code = run_logged_command(
                [sys.executable, *arguments],
                cwd=ROOT,
                stdout=stream,
                timeout=timeout,
                env=env,
            )
            return {
                "step": name,
                "status": "PASS" if code == 0 else "FAIL",
                "exit_code": code,
                "log": log.name,
            }
        except subprocess.TimeoutExpired:
            return {"step": name, "status": "FAIL", "error": "Timeout", "log": log.name}
        except OSError as exc:
            return {"step": name, "status": "FAIL",
                    "error": f"Cannot execute check: {exc}", "log": log.name}



def read_test_totals(junit):
    """Count actual completed native tests; missing/empty XML must fail closed."""
    tree = ET.parse(junit).getroot()
    suites = ([tree] if tree.tag == "testsuite" else
              list(tree.findall("testsuite")) if tree.tag == "testsuites" else [])
    if not suites:
        raise ValueError("Нет корректного JUnit testsuite")
    keys = ("tests", "failures", "errors", "skipped")
    totals = dict.fromkeys(keys, 0)
    for suite in suites:
        declared = {key: int(suite.get(key, 0)) for key in keys}
        cases = suite.findall("testcase")
        actual = {
            "tests": len(cases),
            "failures": sum(bool(case.findall("failure")) for case in cases),
            "errors": sum(bool(case.findall("error")) for case in cases),
            "skipped": sum(bool(case.findall("skipped")) for case in cases),
        }
        if (declared != actual or any(n < 0 for n in declared.values())
                or len(cases) > 20000):
            raise ValueError("JUnit сводка не совпадает с реальными testcase")
        for key in keys:
            totals[key] += actual[key]
    return totals


# Native Qt coverage is mandatory. Skipping explicitly POSIX-only process
# checks on Windows is legitimate; skipping an arbitrary or Qt test is not.
REQUIRED_NATIVE_QT_MODULES = frozenset({
    "test_qt", "test_poly_indicator_qt", "test_veles_hierarchy_qt",
    "test_wizard_visual_qt", "test_settings_tree_contrast",
    "test_portfolio_recovery_qt",
})
# Merely finding test_qt in JUnit is not enough to prove that a real Qt
# dashboard button drove a worker to Stop/Restart with preserved state.
REQUIRED_NATIVE_QT_CASES = frozenset({
    ("test_native_dashboard_workflow_qt", "test_dashboard_buttons_drive_real_paper_worker_monitor_and_restart"),
    ("test_native_dashboard_workflow_qt", "test_separate_qt_windows_reopen_running_account_inside_owned_acceptance_session"),
})
# The native operator route ends with an open PAPER position.  A passing GUI
# button test does not prove closed-trade accounting or persisted recovery.
# Require all three independently executed E2E contracts in the *same*
# collected Windows regression (not a free-form self-reported PASS flag).
REQUIRED_PAPER_CYCLE_CASES = frozenset({
    ("test_paper_complete_recovery_acceptance", "test_polymarket_full_cycle_pause_reopen_official_settle_once"),
    ("test_paper_complete_recovery_acceptance", "test_veles_full_close_open_close_on_one_sqlite_ledger"),
    ("test_process", "test_process_stop_restart_force_and_recovery"),
})

# Each parametrized identity is mandatory: one successful strategy cannot
# substitute for the other engines or their cross-account recovery boundary.
CORE_INTEGRATION_STRATEGIES = ('VELES', 'GRID', 'PUMP_DUMP', 'POLYMARKET')
REQUIRED_INTEGRATION_CASES = frozenset(
    [('test_market_fleet_recovery_e2e',
      f'test_hub_restart_preserves_strategy_risk_monitor_and_closed_ledger[{strategy}]')
     for strategy in CORE_INTEGRATION_STRATEGIES]
    + [('test_portfolio_recovery_qt',
        f'test_qt_balance_details_and_reopened_window_share_recovery_boundary[{strategy}]')
       for strategy in CORE_INTEGRATION_STRATEGIES]
    + [('test_portfolio_recovery_integration',
        'test_missing_published_ledger_blocks_all_strategy_admission_and_restores_exactly'
        f'[{consumer}-{supplier}]')
       for consumer in CORE_INTEGRATION_STRATEGIES for supplier in CORE_INTEGRATION_STRATEGIES]
    + [('test_coordinator_connection_lifetime', name) for name in (
        'test_control_connection_commits_or_rolls_back_then_always_closes[False]',
        'test_control_connection_commits_or_rolls_back_then_always_closes[True]',
        'test_control_and_ledger_fences_leave_no_handles_for_windows_teardown')]
    + [('test_hub_resource_service_boundary',
        f'test_service_accepts_resource_budget_and_releases_its_lease[{workers}]') for workers in (13, 29)]
)

ALLOWED_WINDOWS_SKIPS = frozenset({
    ("test_r611_qa_process_integrity", "test_timeout_kills_descendant_process_group"),
    ("test_r611_qa_process_integrity", "test_successful_exit_with_live_child_cannot_pass"),
    ("test_r611_qa_process_integrity", "test_successful_exit_without_children_is_still_allowed"),
    ("test_r611_qa_process_integrity", "test_successful_exit_with_detached_paper_child_cannot_pass"),
    ("test_r70_source_integrity", "test_linked_directory_inside_package_is_rejected"),
})


def native_test_evidence_problems(junit):
    """Require actual execution of every Qt test module; reject unknown skips.

    Count declarations alone do not prove which modules ran. Explicit
    POSIX-only checks are allowed to skip *only* on native Windows; these
    never substitute for missing Qt coverage.
    """
    root = ET.parse(junit).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    seen, seen_cases, skipped, ids, errors = set(), set(), [], set(), []
    executed_exact = set()
    for suite in suites:
        for case in suite.findall("testcase"):
            module = (case.get("classname") or "").rsplit(".", 1)[-1]
            # pytest parametrization belongs to the base test function.
            test_name = (case.get("name") or "").split("[", 1)[0]
            key = (module, case.get("name"))
            if not module or not test_name or key in ids:
                errors.append("JUnit: empty or duplicate testcase identity")
            ids.add(key)
            if case.findall("skipped"):
                skipped.append((module, test_name))
                if (module, test_name) not in ALLOWED_WINDOWS_SKIPS:
                    errors.append("JUnit: unexpected skipped testcase " + repr((module, test_name)))
            elif not case.findall("failure") and not case.findall("error"):
                if module in REQUIRED_NATIVE_QT_MODULES:
                    seen.add(module)
                seen_cases.add((module, test_name))
                executed_exact.add(key)
    missing_cases = REQUIRED_NATIVE_QT_CASES - seen_cases
    if missing_cases:
        errors.append("Qt: missing executed dashboard lifecycle E2E: " + repr(sorted(missing_cases)))
    missing_paper = REQUIRED_PAPER_CYCLE_CASES - seen_cases
    if missing_paper:
        errors.append("PAPER: missing completed trade/recovery evidence: "
                      + repr(sorted(missing_paper)))
    missing = REQUIRED_NATIVE_QT_MODULES - seen
    missing_integration = REQUIRED_INTEGRATION_CASES - executed_exact
    if missing_integration:
        errors.append('Integrated fleet: missing executed recovery cases: '
                      + repr(sorted(missing_integration)))
    if missing:
        errors.append("Qt: missing executed native test modules: " + ", ".join(sorted(missing)))
    return errors



def verify_complete_test_inventory(junit, inventory):
    """Reject missing, unexpected or duplicated tests, including parametrizations."""
    from collections import Counter
    content = json.loads(Path(inventory).read_text(encoding="utf-8"))
    if not isinstance(content, dict) or content.get("schema") != 1:
        raise ValueError("Collection inventory schema missing")
    declared = content.get("cases")
    if not isinstance(declared, list) or not declared or any(
        not isinstance(x, list) or len(x) != 2 or
        not all(isinstance(part, str) and part for part in x)
        for x in declared
    ):
        raise ValueError("Collection inventory malformed")
    expected = Counter(map(tuple, declared))
    if any(n != 1 for n in expected.values()):
        raise ValueError("Collection inventory contains duplicate test identities")
    root = ET.parse(junit).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    actual = Counter((case.get("classname"), case.get("name"))
                     for suite in suites for case in suite.findall("testcase"))
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        raise ValueError(f"Collected/executed tests differ: missing={sum(missing.values())} "
                         f"unexpected_or_duplicate={sum(extra.values())}")
    return len(expected)

def valid_png_capture(path):
    """Reject empty/forged screenshots, not merely files with a PNG signature.

    A native Qt capture must contain a sound IHDR, nonempty image stream,
    valid chunk CRCs and a terminating IEND. Avoid loading unbounded images.
    """
    path = Path(path)
    if path.stat().st_size > 20_000_000:
        return False
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset, got_header, got_image, got_end = 8, False, False, False
    image_chunks = []
    palette_entries = None
    finished_idat = False
    while offset + 12 <= len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + size
        if size > 20_000_000 or end > len(data):
            return False
        payload = data[offset + 8:offset + 8 + size]
        crc = struct.unpack_from(">I", data, offset + 8 + size)[0]
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            return False
        if not got_header:
            if kind != b"IHDR" or size != 13:
                return False
            width, height = struct.unpack_from(">II", payload)
            if not 100 <= width <= 10000 or not 100 <= height <= 10000:
                return False
            # Qt evidence is saved as 8-bit, noninterlaced PNG. Reject
            # unsupported image encodings rather than making a false claim
            # that their scanlines were decoded and verified.
            if (payload[8] != 8 or payload[9] not in (0, 2, 3, 4, 6)
                    or payload[10:] != b"\x00\x00\x00"):
                return False
            color_type = payload[9]
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
            expected = (1 + width * channels) * height
            if expected > 64_000_000:
                return False
            got_header = True
        elif kind == b"PLTE":
            # Indexed PNG cannot be decoded without its palette, and PLTE
            # cannot appear after IDAT or be repeated. A forged screenshot
            # with correct CRC and zlib bytes must not satisfy the Qt gate.
            if (got_image or palette_entries is not None or size == 0
                    or size % 3 or size > 768 or payload is None
                    or color_type in (0, 4)):
                return False
            palette_entries = size // 3
        elif kind == b"IDAT":
            if finished_idat or (color_type == 3 and palette_entries is None):
                return False
            got_image |= bool(size)
            image_chunks.append(payload)
        elif kind == b"IEND":
            got_end = size == 0
            if not (got_header and got_image and got_end and end == len(data)):
                return False
            try:
                # A correct PNG signature and CRC are not proof of a decodable
                # image. Cap inflation to avoid compression-bomb evidence.
                inflater = zlib.decompressobj()
                decoded = bytearray()
                for chunk in image_chunks:
                    decoded.extend(inflater.decompress(chunk, expected + 1 - len(decoded)))
                    if len(decoded) > expected or inflater.unconsumed_tail:
                        return False
                # A zlib stream containing arbitrary bytes is NOT a PNG
                # image. Require exactly one filter byte and the complete
                # pixel data for every row (not just successful inflation).
                row_bytes = 1 + width * channels
                structurally_valid = (
                    inflater.eof and not inflater.unused_data
                    and len(decoded) == expected
                    and all(decoded[y * row_bytes] in (0, 1, 2, 3, 4)
                            for y in range(height))
                )
                if not structurally_valid:
                    return False
                if os.name == "nt":
                    # This is native Windows Qt acceptance, not a generic
                    # file-sniffing check. Qt itself must decode the pixels.
                    try:
                        from PySide6.QtGui import QImageReader
                        reader = QImageReader(str(path))
                        reader.setDecideFormatFromContent(True)
                        rendered = reader.read()
                        return (not rendered.isNull()
                                and rendered.width() == width
                                and rendered.height() == height)
                    except (ImportError, RuntimeError, ValueError):
                        return False
                return True
            except zlib.error:
                return False
        elif kind == b"IHDR":
            return False
        elif kind and kind[0] & 32 == 0:
            # Unknown mandatory PNG chunk: fail rather than pretend to decode.
            return False
        if got_image and kind != b"IDAT":
            finished_idat = True
        offset = end
    return False


def source_access_blockers(path):
    """Record verified public-host denials without weakening the release gate.

    GitHub-hosted Windows runners may be region-blocked by an exchange. That
    cannot be fixed by changing venue, using cached prices or pretending a
    403/451 is a valid PAPER fill. Keep a small structured, credential-free
    diagnostic separate from software regression and from release approval.
    """
    from bcc.market import PublicData

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("checks"), list):
        return []
    expected = {"Bybit USDT Futures", "Polymarket Gamma + CLOB", "Binance spot impulse"}
    blockers = []
    for row in payload["checks"]:
        if (not isinstance(row, dict) or type(row.get("source")) is not str
                or row["source"] not in expected):
            continue
        if row.get("status") != "RESTRICTED":
            continue
        details = row.get("restriction")
        if (not isinstance(details, dict)
                or type(details.get("host")) is not str
                or details["host"] not in PublicData.HOSTS
                or type(details.get("http_status")) is not int
                or details["http_status"] not in (403, 451)):
            continue
        blockers.append({
            "source": row["source"], "host": details["host"],
            "http_status": details["http_status"],
            "classification": "EXTERNAL_PROVIDER_ACCESS_DENIED",
        })
    return blockers


def validate_required_evidence(output, *, venue="all"):
    """Fail closed on missing, malformed or incomplete *required* Windows evidence.

    This checks evidence structure only. It is NOT a substitute for genuinely
    executing Qt on Windows, or for completed market-data PAPER acceptance.
    """
    from bcc.config import STRATEGIES

    output = Path(output)
    problems = []
    try:
        totals = read_test_totals(output / "tests.xml")
        # The headless baseline already includes >500 real tests. A tiny
        # fabricated XML must not certify the entire native Windows suite.
        if totals["tests"] < 500 or totals["failures"] or totals["errors"]:
            problems.append("tests.xml: неполная или неуспешная регрессия: " + str(totals))
        problems.extend(native_test_evidence_problems(output / "tests.xml"))
        count = verify_complete_test_inventory(output / "tests.xml", output / "collected_tests.json")
        if count != totals["tests"]:
            problems.append("tests.xml: inventory count disagrees with JUnit")
    except (OSError, ValueError, ET.ParseError) as exc:
        problems.append("tests.xml: отсутствует/повреждён JUnit: " + str(exc))

    gui_dir = output / "screenshots"
    try:
        gui = json.loads((gui_dir / "gui_report.json").read_text(encoding="utf-8"))
        if (not isinstance(gui, dict) or gui.get("status") != "PASS" or
                gui.get("native_windows") is not True or gui.get("qt_platform") != "windows"):
            problems.append("gui_report.json: нет подтверждённого native Qt PASS")
        else:
            seen = {x["strategy"] for x in gui.get("checks", [])
                    if isinstance(x, dict) and x.get("status") == "PASS"
                    and x.get("created_and_reopened") == "PASS"
                    and x.get("edited_and_reopened") == "PASS"}
            if seen != set(STRATEGIES):
                problems.append("gui_report.json: не все конструкторы стратегий пройдены")
            # A constructor that only saves a name is not a financial GUI
            # acceptance: every strategy must prove durable capital adjustment
            # in the independent PAPER ledger, with a valid wizard page count.
            checks = gui.get("checks")
            if (not isinstance(checks, list) or len(checks) != len(STRATEGIES)
                    or any(not isinstance(x, dict)
                           or x.get("deposit_adjustment_with_ledger") != "PASS"
                           or type(x.get("pages")) is not int or x["pages"] < 4
                           for x in checks)):
                problems.append("gui_report.json: депозит/PAPER Ledger или страницы Qt не проверены")
            # The visual constructor check also exercises real independent
            # owner copies, ZIP/CSV round-trips, ledger audit, durable command
            # revocation and deletion. Never accept a screenshot-only report
            # in place of this complete operator route.
            required_cycle = {"copy", "transfer", "csv_history", "command_revoke",
                              "delete_and_copy_isolation", "paper_ledger_audit"}
            cycles = gui.get("operator_cycles")
            expected_cycles = {"VELES", "GRID", "PUMP_DUMP", "POLYMARKET"}
            if (not isinstance(cycles, list) or len(cycles) != len(expected_cycles)
                    or {x.get("strategy") for x in cycles if isinstance(x, dict)}
                    != expected_cycles
                    or any(not isinstance(x, dict)
                           or any(x.get(field) != "PASS" for field in required_cycle)
                           or x.get("command_scope") != "INTENT_ONLY_NOT_WORKER_E2E"
                           for x in cycles)):
                problems.append("gui_report.json: неполный Qt operator/PAPER lifecycle")
            if gui.get("all_bots_monitor") != "PASS" or gui.get("poly_coin_catalog") != "PASS":
                problems.append("gui_report.json: мониторинг или каталог монет не пройден")
            if (type(gui.get("all_bots_monitor_rows")) is not int
                    or gui["all_bots_monitor_rows"] < 4
                    or not isinstance(gui.get("all_bots_monitor_strategies"), list)
                    or not {"VELES", "GRID", "PUMP_DUMP", "POLYMARKET"}.issubset(
                        set(str(x) for x in gui["all_bots_monitor_strategies"]))):
                problems.append("gui_report.json: нет данных завершённого асинхронного снимка Qt")
            pickers = {x["theme"] for x in gui.get("coin_picker", [])
                       if isinstance(x, dict) and x.get("status") == "PASS"
                       and type(x.get("minimum_contrast")) in (int, float)
                       and x["minimum_contrast"] >= 4.5}
            if pickers != {"dark", "light"}:
                problems.append("gui_report.json: проверка контраста выбора монет неполна")
        images = ("dashboard.png", "all_bots_monitor.png", "coin_picker_polymarket.png",
                  "coin_picker_dark.png", "coin_picker_light.png", "veles_finance.png",
                  "veles_orders.png") + tuple(s.lower() + ".png" for s in STRATEGIES)
        for name in images:
            try:
                if not valid_png_capture(gui_dir / name):
                    problems.append("screenshots/" + name + ": повреждённый или пустой PNG")
            except OSError:
                problems.append("screenshots/" + name + ": отсутствует снимок")
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        problems.append("gui_report.json: отсутствует/повреждён: " + str(exc))

    try:
        report = json.loads((output / "public_sources.json").read_text(encoding="utf-8"))
        expected = ({"Bybit USDT Futures"}
                    if venue == "bybit" else
                    {"Bybit USDT Futures", "Polymarket Gamma + CLOB", "Binance spot impulse"})
        required_scope = (["bybit"] if venue == "bybit"
                          else ["bybit", "polymarket", "binance"])
        if (not isinstance(report, dict)
                or report.get("read_only") is not True
                or report.get("all_available") is not True
                or report.get("scope") != required_scope):
            
            problems.append("public_sources.json: публичные источники не подтверждены")
            for blocker in source_access_blockers(output / "public_sources.json"):
                problems.append(
                    "public_sources.json: " + blocker["source"] + " — HTTP "
                    + str(blocker["http_status"]) + " (" + blocker["host"] + ")"
                )
        else:
            passed = {x["source"] for x in report.get("checks", [])
                      if isinstance(x, dict) and x.get("status") == "PASS"}
            if passed != expected:
                problems.append("public_sources.json: хотя бы один источник недоступен")
            observed = datetime.fromisoformat(report["at"])
            if observed.tzinfo is None or abs((datetime.now(timezone.utc) - observed).total_seconds()) > 600:
                problems.append("public_sources.json: отчёт устарел или время источников неверно")
            checks = {x["source"]: x for x in report["checks"]}
            bybit = checks.get("Bybit USDT Futures", {}).get("evidence") or {}
            if (bybit.get("book_fresh") is not True or
                    type(bybit.get("book_age_upper")) not in (int, float) or
                    not 0 <= bybit["book_age_upper"] <= 3):
                problems.append("public_sources.json: Bybit стакан не подтверждён как свежий")
            if any(not isinstance(checks.get(source, {}).get("evidence"), dict)
                   or not checks[source]["evidence"] for source in expected):
                problems.append("public_sources.json: отсутствуют подтверждения данных источников")
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        problems.append("public_sources.json: отсутствует/повреждён: " + str(exc))
    return problems

def is_native_windows():
    return os.name == "nt" and os.environ.get("QT_QPA_PLATFORM", "windows") == "windows"


def main():
    parser = argparse.ArgumentParser(description="Нативная проверка Windows Qt и PAPER")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT
        / "verification_reports"
        / datetime.now().strftime("%Y%m%d_%H%M%S"),
    )
    parser.add_argument(
        "--venue", choices=("all", "bybit"), default="all",
        help="Scoped Bybit phase does not certify deferred Polymarket/Binance venues"
    )
    args = parser.parse_args()
    output = args.output.resolve()
    # A reused output directory might contain stale passing screenshots/XML.
    # Never let old evidence certify a new verification attempt.
    if output.exists() and any(output.iterdir()):
        print("Refusing to reuse nonempty Windows evidence directory: " + str(output), flush=True)
        return 1
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "app_version": VERSION,
        "at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": sys.version,
        "source_sha256": None,
        "steps": [],
        "venue_scope": args.venue,
        "deferred_venues": (["polymarket", "binance"] if args.venue == "bybit" else []),
        "release_ready": False,
        "manual_acceptance": "PENDING: follow ACCEPTANCE_WINDOWS_RU.md; automated tests do not replace it",
    }
    target = output / "windows_report.json"
    if not is_native_windows():
        report.update(
            status="BLOCKED",
            error="Требуется настоящий Windows Qt; offscreen не считается нативной проверкой",
        )
        atomic_json(target, report)
        print(report["error"], flush=True)
        return 2
    from tools.verify_package import verify, assert_same_snapshot, SourceIntegrityError
    try:
        report["source_integrity"] = verify(ROOT)
        # Fingerprint only AFTER verifying the required files. Otherwise a
        # missing launcher can raise FileNotFoundError before an error report
        # has even been written, reproducing the earlier broken QA handoff.
        report["source_sha256"] = source_fingerprint()
    except (SourceIntegrityError, OSError, ValueError) as exc:
        report.update(status="FAIL", source_integrity={"status": "FAIL", "error": str(exc)})
        atomic_json(target, report)
        print("Source integrity FAILED:", exc, flush=True)
        return 1
    steps = [
        (
            "collection",
            ["-m", "tools.collect_test_inventory", "--output",
             str(output / "collected_tests.json")],
            WINDOWS_QA_TIMEOUTS["collection"],
        ),
        (
            "tests",
            ["-m", "tools.run_partitioned_tests",
             "--inventory", str(output / "collected_tests.json"),
             "--output", str(output / "test_shards")],
            WINDOWS_QA_TIMEOUTS["tests"],
        ),
        ("gui", ["-m", "bcc.gui_check", "--output", str(output / "screenshots")], WINDOWS_QA_TIMEOUTS["gui"]),
        (
            "public_sources",
            ["-m", "bcc.health", "--output", str(output / "public_sources.json"),
             "--venue", args.venue],
            WINDOWS_QA_TIMEOUTS["public_sources"],
        ),
    ]
    # The outer launcher must not time out before the inner runner can
    # finish every explicitly budgeted stage and persist FAIL diagnostics.
    # Keep these budgets in sync with tools/windows_check.py.
    for name, arguments, timeout in steps:
        try:
            assert_same_snapshot(report['source_integrity'], verify(ROOT))
        except (SourceIntegrityError, OSError, ValueError) as exc:
            report['steps'].append(dict(step='source_integrity_before_' + name,
                                        status='FAIL', error=str(exc)))
            break
        step_result = run_step(name, arguments, output, timeout)
        report["steps"].append(step_result)
        atomic_json(target, report)
        if step_result["status"] != "PASS":
            # No further GUI or market check after failed automated regression.
            break
    try:
        report["test_totals"] = read_test_totals(output / "tests.xml")
    except (OSError, ValueError, ET.ParseError):
        report["test_totals"] = None
    report["source_access_blockers"] = source_access_blockers(output / "public_sources.json")
    report["evidence_errors"] = validate_required_evidence(output, venue=args.venue)
    if report["evidence_errors"]:
        report["steps"].append({
            "step": "required_evidence", "status": "FAIL",
            "errors": report["evidence_errors"],
        })
    try:
        report['source_integrity_final'] = assert_same_snapshot(report['source_integrity'], verify(ROOT))
    except (SourceIntegrityError, OSError, ValueError) as exc:
        report['source_integrity_final'] = dict(status='FAIL', error=str(exc))
        report['steps'].append(dict(step='source_integrity_final', status='FAIL', error=str(exc)))
    report["status"] = (
        ("BYBIT_PHASE_AUTOMATED_PASS" if args.venue == "bybit"
         else "AUTOMATED_CHECKS_PASS")
        if all(s["status"] == "PASS" for s in report["steps"])
        else "FAIL"
    )
    atomic_json(target, report)
    print("Report:", target, flush=True)
    print(
        "Manual PAPER acceptance is still required. No release readiness is inferred.",
        flush=True,
    )
    return 0 if report["status"] == "AUTOMATED_CHECKS_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
