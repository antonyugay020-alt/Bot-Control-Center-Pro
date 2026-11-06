"""Reproducible repeated regression audit; public API availability is separate."""

import argparse
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bcc import VERSION
from bcc.storage import atomic_json
from tools.verify_windows import source_fingerprint

AREAS = [
    (
        "01",
        "Свежесть стакана и окончательное исполнение",
        [
            "test_public_clock",
            "test_market_clock",
            "test_depth_expiring",
            "test_transport_uses_book",
            "test_report_distinguishes",
        ],
    ),
    (
        "02",
        "Схема, суммы и наследование настроек",
        [
            "test_configuration_validates",
            "test_bad_money",
            "test_coin_and_section_override",
            "test_bool_schema",
        ],
    ),
    (
        "03",
        "Закрытые свечи и независимые таймфреймы",
        [
            "test_two_minute",
            "test_bar_close_clock",
            "test_higher_timeframe",
            "test_candles_reload",
        ],
    ),
    (
        "04",
        "Индикаторы, прогрев и логика сигналов",
        [
            "test_indicator_has",
            "test_known_rsi",
            "test_nested_threshold",
            "test_no_cross",
        ],
    ),
    (
        "05",
        "Глубина, IOC/FOK, шаг и бюджет",
        [
            "test_ioc_consumes",
            "test_depth_consumption",
            "test_seeded_execution",
            "test_exhausted_top",
        ],
    ),
    (
        "06",
        "Часы, погрешность, скачки и срок сверки",
        [
            "test_public_clock",
            "test_clock_diagnostics",
            "test_receipt_proof",
            "test_quote_age",
        ],
    ),
    (
        "07",
        "Денежный журнал и повреждённые состояния",
        [
            "test_realized_cannot",
            "test_corrupt_position",
            "test_book_rejection_rolls",
            "test_corrupted_imported",
        ],
    ),
    ("08", "Миграция 813 графиков и атомарность", ["test_chart_storage"]),
    (
        "09",
        "Резервирование, убыток и реинвестирование",
        [
            "test_unrealized_loss",
            "test_coin_positions_share",
            "reinvest",
            "liquidation",
        ],
    ),
    (
        "10",
        "DCA, сетка и частичные исполнения",
        [
            "test_partially_filled_dca",
            "test_veles_behaviour",
            "test_partial_exit_survives",
        ],
    ),
    (
        "11",
        "Тейки, стопы, безубыток и остатки лота",
        [
            "test_last_take_profit",
            "test_incomplete_first_take",
            "test_partial_take_profit_retry",
            "test_veles_behaviour",
        ],
    ),
    (
        "12",
        "Секции VELES, изменения и завершение",
        ["test_veles_behaviour", "test_changed_intrabar_spec", "test_disabled"],
    ),
    ("13", "GRID: уровни и ограничение цены", ["test_grid_levels", "test_grid_limit"]),
    ("14", "PUMP&DUMP: импульс и спред", ["test_pump_fast", "test_pump_spread"]),
    (
        "15",
        "Polymarket: входы, комиссии и лимиты",
        [
            "test_public_polymarket_fee",
            "test_polymarket_daily",
            "test_health_liquidity",
        ],
    ),
    (
        "16",
        "Официальные исходы и отложенный funding",
        [
            "test_polymarket_waits",
            "test_polymarket_unavailable",
            "test_late_published_funding",
            "test_funding_is_idempotent",
        ],
    ),
    (
        "17",
        "Планировщик, очередь, кэш и конкуренция",
        [
            "test_public_cache",
            "test_open_position_refresh",
            "test_slow_history",
            "test_five_pending",
        ],
    ),
    (
        "18",
        "Реальные дочерние процессы и stop/restart",
        ["test_process_stop", "test_lifecycle_identity", "test_delayed_old_stop"],
    ),
    (
        "19",
        "Перенос, копии и снимки без остановки",
        ["test_transfer", "test_max_length_bot", "test_readonly", "test_report"],
    ),
    (
        "20",
        "Qt, конструкторы, монитор и Windows-пакет",
        ["test_qt", "test_windows_package"],
    ),
]


def inspect_junit(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    names = [c.get("classname", "") + "." + c.get("name", "") for c in cases]
    skips = [names[i] for i, c in enumerate(cases) if c.find("skipped") is not None]
    bad = [
        names[i]
        for i, c in enumerate(cases)
        if c.find("failure") is not None or c.find("error") is not None
    ]
    expected_skips = (
        []
        if os.name == "nt"
        else [
            n
            for n in skips
            if "test_independent_process_identity_with_native_procfs" in n
        ]
    )
    areas = []
    for code, title, selectors in AREAS:
        selected = [n for n in names if any(s in n for s in selectors)]
        areas.append(
            dict(
                id=code,
                area=title,
                selected_cases=len(selected),
                status="PASS"
                if selected and not set(selected).intersection(bad + skips)
                else "FAIL",
            )
        )
    return dict(
        tests=len(cases),
        passed=len(cases) - len(skips) - len(bad),
        failures=len(bad),
        skipped=len(skips),
        skipped_cases=skips,
        unexpected_skips=[n for n in skips if n not in expected_skips],
        failed_cases=bad,
        areas=areas,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=ROOT / "verification_evidence/2026-10-28-audit20"
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = dict(
        app_version=VERSION,
        started=datetime.now(timezone.utc).isoformat(),
        source_sha256=source_fingerprint(),
        status="RUNNING",
        planned_runs=20,
        public_data_acceptance="SEPARATE",
        native_windows_verified=False,
        runs=[],
    )
    atomic_json(output / "audit20.json", report)
    for number in range(1, 21):
        seed = 7919 * number
        env = dict(
            os.environ,
            QT_QPA_PLATFORM="offscreen",
            BCC_AUDIT_SEED=str(seed),
            PYTHONHASHSEED=str(seed),
            PYTHONIOENCODING="utf-8",
        )
        junit = output / f"run_{number:02d}.xml"
        start = time.monotonic()
        with (output / f"run_{number:02d}.log").open("w", encoding="utf-8") as log:
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-X",
                        "faulthandler",
                        "-m",
                        "pytest",
                        "-q",
                        "tests",
                        "--junitxml=" + str(junit),
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=180,
                    check=False,
                )
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = -1
        row = dict(
            run=number,
            seed=seed,
            seconds=round(time.monotonic() - start, 3),
            exit_code=code,
        )
        if junit.exists():
            row.update(inspect_junit(junit))
        row["status"] = (
            "PASS"
            if code == 0
            and not row.get("unexpected_skips", ["missing_junit"])
            and all(a["status"] == "PASS" for a in row.get("areas", []))
            else "FAIL"
        )
        report["runs"].append(row)
        report["source_unchanged"] = source_fingerprint() == report["source_sha256"]
        atomic_json(output / "audit20.json", report)
        print(
            f"Run {number}/20 seed={seed}: {row['status']} passed={row.get('passed')} skipped={row.get('skipped')} ({row['seconds']}s)",
            flush=True,
        )
        if row["status"] != "PASS" or not report["source_unchanged"]:
            report["status"] = "FAIL"
            atomic_json(output / "audit20.json", report)
            return 1
    report.update(status="PASS", finished=datetime.now(timezone.utc).isoformat())
    atomic_json(output / "audit20.json", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
