"""Check packaging, failure reporting and isolated collection without trading."""

import importlib.util
import json
import os
import sys
import zipfile
from pathlib import Path

from test_core import attach, bars, config

from bcc.config import condition, group
from bcc.storage import Repository, Store
from bcc.strategies import Veles
from bcc.support import collect


def checker():
    path = Path(__file__).resolve().parents[1] / "tools/windows_check.py"
    spec = importlib.util.spec_from_file_location("windows_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_zip_contains_only_chosen_report_tree(tmp_path):
    module = checker()
    output = tmp_path / "report"
    output.mkdir()
    (output / "summary.json").write_text('{"release_ready": false}', encoding="utf-8")
    (tmp_path / "unrelated_private_file.txt").write_text("outside report", encoding="utf-8")
    archive = module.report_archive(output, tmp_path / "report.zip")
    with zipfile.ZipFile(archive) as z:
        assert z.namelist() == ["report/summary.json"]
        assert z.testzip() is None


def test_runner_records_nonzero_exit_and_timeout(tmp_path):
    module = checker()
    failure = module.run_step(
        "failure",
        [sys.executable, "-c", "print('failure evidence');raise SystemExit(7)"],
        tmp_path,
    )
    assert failure["exit_code"] == 7
    assert "failure evidence" in (tmp_path / "failure.log").read_text(encoding="utf-8")
    timeout = module.run_step(
        "timeout",
        [sys.executable, "-c", "import time;time.sleep(5)"],
        tmp_path,
        timeout=0.1,
    )
    assert timeout["exit_code"] is None and timeout["error"] == "Timeout"


def test_nonwindows_block_still_produces_diagnostic_archive(tmp_path, monkeypatch):
    if os.name == "nt":
        return  # This is the non-Windows branch; Windows is checked by the native gate.
    module = checker()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    assert module.main(["--automated-only"]) == 2
    (archive,) = tmp_path.glob("BCC_VALIDATION_RESULTS_*.zip")
    with zipfile.ZipFile(archive) as z:
        report = json.loads(
            z.read(next(n for n in z.namelist() if n.endswith("check_summary.json")))
        )
    assert report["status"] == "BLOCKED"
    assert not report["release_ready"]
    assert report["manual_acceptance"] == "NOT_CONFIRMED"
    assert not (tmp_path / ".check_venv").exists()


def test_collector_preserves_pending_grid_and_never_discovers_other_root(tmp_path):
    repo = Repository(tmp_path / "isolated_check")
    cfg = config()
    cfg["symbols"] = ["BTCUSDT"]
    cfg["sections"] = cfg["sections"][:1]
    cfg["sections"][0]["entry"] = group(condition("PRICE", 0, ">", 1))
    cfg["settings"]["orders"]["first_offset_pct"] = 1
    folder = repo.create(cfg)
    store = Store(folder, cfg["capital"])
    with store.transaction() as (state, events):
        attach(state)
        Veles().process(
            cfg, state, events, "BTCUSDT", bars([29, 31]), 99.99, 100.01, 120000
        )
        assert state["pending"] and not state["positions"]
        state.pop("_books")
    other_repo = Repository(tmp_path / "old_user_app")
    other = other_repo.create(config())
    (other / "private_file.txt").write_text("must never be collected", encoding="utf-8")
    before = store.path.read_bytes()
    report = collect(repo.root, tmp_path / "report")
    assert not report["errors"]
    assert len(report["bots"]) == 1
    target = tmp_path / "report" / cfg["strategy"] / folder.name
    assert Store(target).read() == store.read()
    Store(target).audit()
    assert store.path.read_bytes() == before
    assert not list((tmp_path / "report").rglob("private_file.txt"))


def test_collector_reports_broken_ledger_without_packaging_bad_database(tmp_path):
    repo = Repository(tmp_path / "check")
    folder = repo.create(config())
    store = Store(folder, 500)
    with store.connect() as db:
        state = store.read()
        state["cash"] -= 10
        db.execute("UPDATE state SET document=?", (json.dumps(state),))
    report = collect(repo.root, tmp_path / "report")
    assert report["errors"]
    assert not list((tmp_path / "report").rglob("paper.sqlite"))


def test_collector_flags_stopped_worker_error_even_when_ledger_is_valid(tmp_path):
    from bcc.storage import atomic_json

    repo = Repository(tmp_path / "check")
    folder = repo.create(config())
    Store(folder, 500)
    atomic_json(
        folder / "process.json", {"status": "ERROR", "last_error": "database is locked"}
    )
    report = collect(repo.root, tmp_path / "report")
    assert report["errors"][0]["worker_error"] == "database is locked"
    assert report["bots"][0]["worker_status"] == "ERROR"
    assert report["bots"][0]["ledger"] == "AUDIT_PASS"


def test_collector_flags_unfinished_manual_close_as_unfulfilled_request(tmp_path):
    from test_execution_runtime import open_position

    repo = Repository(tmp_path / "check")
    cfg = config()
    folder = repo.create(cfg)
    store = Store(folder, 500)
    with store.transaction() as (state, events):
        open_position(state, events, cfg["settings"])
        state.pop("_books")
        state["positions"]["BTCUSDT:main"]["exit_pending"] = {
            "reason": "MANUAL_PAPER_CLOSE",
            "ratio": 1.0,
        }
    report = collect(repo.root, tmp_path / "report")
    assert report["bots"][0]["ledger"] == "AUDIT_PASS"
    assert report["bots"][0]["pending_manual_close"] == 1
    assert report["errors"][0]["pending_manual_close"] == 1


def test_current_report_snapshots_active_bot_without_stopping_or_other_root(tmp_path):
    from test_process import RUNNER, launch, setup_bot, wait_for

    from bcc.lifecycle import Lifecycle
    from bcc.support import export_current_report

    repo = Repository(tmp_path / "current_application")
    runner = tmp_path / "report_worker.py"
    runner.write_text(RUNNER, encoding="utf-8")
    folder, _ = setup_bot(repo, runner, "Current live PAPER worker")
    other_repo = Repository(tmp_path / "other_application")
    other = other_repo.create(config())
    (other / "not_in_this_report.txt").write_text("outside current root", encoding="utf-8")
    life = Lifecycle(repo)
    child = launch(life, folder, runner)
    try:
        wait_for(lambda: Store(folder).read()["positions"])
        target = tmp_path / "current.zip"
        summary = export_current_report(repo.root, target, life=life)
        assert summary["read_only"] and summary["bot_count"] == 1
        assert summary["workers_stopped_for_report"] is False
        assert child.poll() is None and not (folder / "stop.request").exists()
        with zipfile.ZipFile(target) as archive:
            assert archive.testzip() is None
            assert not any("not_in_this_report" in name for name in archive.namelist())
            collection = json.loads(
                archive.read("current_bots_report/paper_bots/collection.json")
            )
            assert collection["bots"][0]["active_process_confirmed"]
            assert collection["bots"][0]["ledger"] == "AUDIT_PASS"
            assert collection["bots"][0]["open_positions"] == 1
            saved = json.loads(
                archive.read(
                    f"current_bots_report/paper_bots/VELES/{folder.name}/state.json"
                )
            )
            assert len(saved["positions"]) == 1
    finally:
        if child.poll() is None:
            life.stop(folder)
            child.wait(timeout=7)


def test_current_report_refuses_empty_root_and_preserves_existing_archive(tmp_path):
    import pytest

    from bcc.support import export_current_report

    repo = Repository(tmp_path / "empty")
    target = tmp_path / "report.zip"
    target.write_bytes(b"previous report must remain")
    with pytest.raises(ValueError, match="нет ботов"):
        export_current_report(repo.root, target)
    assert target.read_bytes() == b"previous report must remain"
