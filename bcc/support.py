"""Collect only the dedicated check workspace; never discover other bot roots."""

import argparse
import math
import os
import platform
import re
import sqlite3
import shutil
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import VERSION
from .lifecycle import Lifecycle
from .storage import Repository, Store, atomic_json, load_json, paper_account_present


def execution_freshness(events):
    fills = [e for e in events if e["action"] in ("OPEN", "DCA", "CLOSE", "PARTIAL")]
    proven = [
        e
        for e in fills
        if all(k in e for k in ("book_at", "book_age_lower", "book_age_upper"))
    ]
    invalid = [
        e["id"]
        for e in proven
        if not (
            all(
                type(e[k]) in (int, float) and math.isfinite(e[k])
                for k in ("book_at", "book_age_lower", "book_age_upper")
            )
            and e["book_at"] > 0
            and -0.5 <= e["book_age_lower"] <= e["book_age_upper"] <= 3
        )
    ]
    return dict(
        status="FAIL"
        if invalid
        else "PARTIAL_EVIDENCE"
        if len(proven) < len(fills)
        else "PASS"
        if fills
        else "NO_FILLS",
        examined_fills=len(fills),
        with_age_evidence=len(proven),
        without_age_evidence=len(fills) - len(proven),
        invalid_event_ids=invalid,
        maximum_age_upper=max(
            (e["book_age_upper"] for e in proven if e["id"] not in invalid),
            default=None,
        ),
    )


def collect(root, output, *, stop_workers=False, life=None):
    if stop_workers:
        raise ValueError('Diagnostic snapshots cannot stop worker processes; submit Stop via Supervisor')
    repo = Repository(root)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    life = life or Lifecycle(repo)
    if life.repo.root != repo.root:
        raise ValueError("Контроллер относится к другой папке ботов")
    report = {
        "bots": [],
        "errors": [],
        "release_ready": False,
        "workers_stopped_for_report": stop_workers,
    }
    rows = repo.list()
    for folder, cfg in rows:
        item = {
            "bot": folder.name,
            "name": cfg.get("name"),
            "strategy": cfg.get("strategy"),
        }
        report["bots"].append(item)
        try:
            cfg, _ = repo.load(folder)
            target = output / cfg["strategy"] / folder.name
            target.mkdir(parents=True)
            # Evidence FIRST: even if process.json or the PAPER ledger is
            # damaged, the exception traceback must still reach the ZIP.
            # Sanitize lines that might contain credentials.
            for name in ("engine.log", "console.log"):
                path = folder / name
                if path.is_file() and not path.is_symlink():
                    _safe_log_tail(path, target / name, max_bytes=1_000_000)
            atomic_json(target / "config.json", cfg)
            item["process_status"] = life.status(folder)
            active = life.active_snapshot(folder)
            item["active_process_confirmed"] = active is not None
            if active and active.get("active_config"):
                from .config import validate

                try:
                    atomic_json(
                        target / "active_config.json", validate(active["active_config"])
                    )
                except ValueError as exc:
                    item["active_config_error"] = str(exc)
            process_file = folder / "process.json"
            if process_file.is_file():
                try:
                    process = load_json(process_file)
                    if not isinstance(process, dict):
                        raise ValueError("Повреждён process.json: требуется объект JSON")
                except (ValueError, OSError, TypeError, UnicodeError) as exc:
                    # A broken process snapshot is not permission to discard
                    # a valid, independently backed-up PAPER account.
                    item["process_error"] = f"{type(exc).__name__}: {exc}"[:400]
                    report["errors"].append({"bot": folder.name,
                                             "process_error": item["process_error"]})
                else:
                    item["worker_status"] = process.get("status")
                    item["worker_app_version"] = process.get("app_version")
                    if process.get("status") == "ERROR" and (
                        item["process_status"] == "Остановлен" or active
                    ):
                        message = str(process.get("last_error") or "Worker stopped with an error")[:1000]
                        item["worker_error"] = message
                        report["errors"].append(
                            {"bot": folder.name, "worker_error": message}
                        )
            if paper_account_present(folder):
                with tempfile.TemporaryDirectory(prefix="bcc-report-") as temp:
                    snapshot_path = Path(temp)
                    Store(folder).backup(snapshot_path / "paper.sqlite")
                    snapshot = Store(snapshot_path)
                    snapshot.audit()
                    state = snapshot.read()
                    item["open_positions"] = len(state["positions"])
                    item["pending_manual_close"] = sum(
                        p.get("exit_pending", {}).get("reason") == "MANUAL_PAPER_CLOSE"
                        for p in state["positions"].values()
                    )
                    if item["pending_manual_close"]:
                        report["errors"].append(
                            {
                                "bot": folder.name,
                                "pending_manual_close": item["pending_manual_close"],
                            }
                        )
                    atomic_json(target / "state.json", state)
                    history = snapshot.history(100_000)
                    item["execution_freshness"] = execution_freshness(history)
                    if item["execution_freshness"]["status"] == "FAIL":
                        report["errors"].append(
                            {
                                "bot": folder.name,
                                "execution_freshness": item["execution_freshness"],
                            }
                        )
                    atomic_json(target / "executions.json", history)
                    shutil.copy2(snapshot.path, target / "paper.sqlite")
                item["ledger"] = "AUDIT_PASS"
        except Exception as exc:
            item["error"] = str(exc)
            report["errors"].append({"bot": folder.name, "error": str(exc)})
    atomic_json(output / "collection.json", report)
    return report


def _safe_log_tail(source, dest, *, max_bytes=2_000_000):
    """Bound diagnostics and remove obvious credential-bearing lines."""
    try:
        with source.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            length = stream.tell()
            stream.seek(max(0, length - max_bytes))
            data = stream.read(max_bytes)
        text = data.decode("utf-8", errors="replace")
        if length > max_bytes:
            text = "[TRUNCATED: newest 2 MB only]\n" + text.split("\n", 1)[-1]
        # Hub/worker diagnostics must not accidentally include API or wallet secrets.
        sensitive = re.compile(
            r"(?i)(?:api[_-]?key|api[_-]?secret|private[_-]?key|"
            r"client[_-]?secret|password|passphrase|mnemonic|"
            r"authorization\s*[:=]|bearer\s+|sk-proj-)")
        sanitized = ["[REDACTED: sensitive log line]" if sensitive.search(line)
                     else line for line in text.splitlines()]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n".join(sanitized) + "\n", encoding="utf-8")
        return {"status": "included", "source_bytes": length, "stored_bytes": dest.stat().st_size}
    except OSError as exc:
        return {"status": "unavailable", "error_type": type(exc).__name__}


def _public_market_diagnostics(root, dest):
    """Small, read-only evidence from the shared market cache; no credentials/URLs.

    Do not archive the entire live (potentially hundreds of MB) WAL database,
    and never copy an SQLite file without a safe online backup.
    """
    dbfile = root / "market_cache.sqlite"
    info = {"exists": dbfile.is_file(), "snapshot_kind": "bounded_readonly_metadata"}
    if dbfile.is_file():
        try:
            info["size_bytes"] = dbfile.stat().st_size
            info["wal_size_bytes"] = (root / "market_cache.sqlite-wal").stat().st_size if (root / "market_cache.sqlite-wal").exists() else 0
            conn = sqlite3.connect(dbfile.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
            try:
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA busy_timeout=1000")
                # Inspect only known public-data tables, with bounded aggregate queries.
                for table in ("hub_subscriptions", "cache", "hub_leader"):
                    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                    if exists:
                        info[table + "_rows"] = conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                if "hub_subscriptions_rows" in info:
                    row = conn.execute(
                        "SELECT COUNT(*), SUM(CASE WHEN last_error != '' THEN 1 ELSE 0 END), "
                        "SUM(CASE WHEN active_until > ? THEN 1 ELSE 0 END) "
                        "FROM hub_subscriptions", (time.time(),)).fetchone()
                    info["subscriptions_total"] = row[0]
                    info["subscriptions_with_error"] = row[1] or 0
                    info["subscriptions_active"] = row[2] or 0
                    # Count overload without logging URLs, tokens or symbols.
                    now = time.time()
                    # Observability only: never expose subscription symbols
                    # or public request payloads in an exported report.
                    cols = {row[1] for row in conn.execute('PRAGMA table_info(hub_subscriptions)')}
                    if 'priority_until' in cols:
                        info['urgent_active'] = conn.execute(
                            'SELECT COUNT(*) FROM hub_subscriptions WHERE active_until>? AND priority_until>?',
                            (time.time(), time.time())).fetchone()[0]
                        info['urgent_overdue'] = conn.execute(
                            'SELECT COUNT(*) FROM hub_subscriptions WHERE active_until>? AND priority_until>? AND next_due<=?',
                            (time.time(), time.time(), time.time())).fetchone()[0]
                    info["subscriptions_overdue"] = conn.execute(
                        "SELECT COUNT(*) FROM hub_subscriptions WHERE active_until>? AND next_due<=?",
                        (now, now),
                    ).fetchone()[0]
                    info["subscriptions_not_yet_fetched"] = conn.execute(
                        "SELECT COUNT(*) FROM hub_subscriptions WHERE active_until>? AND next_due=0",
                        (now,),
                    ).fetchone()[0]
                    oldest = conn.execute(
                        "SELECT MIN(next_due) FROM hub_subscriptions WHERE active_until>? AND next_due>0 AND next_due<=?",
                        (now, now),
                    ).fetchone()[0]
                    # 0 is an initial "due now" sentinel, not a Unix timestamp.
                    info["oldest_overdue_seconds"] = (
                        round(max(0, now - oldest), 2) if oldest is not None else 0
                    )
                if "cache_rows" in info:
                    info["cache_unexpired"] = conn.execute(
                        "SELECT COUNT(*) FROM cache WHERE until > ?", (time.time(),)
                    ).fetchone()[0]
            finally:
                conn.close()
        except (OSError, sqlite3.Error) as exc:
            info["read_error"] = type(exc).__name__ + ": " + str(exc)[:150]
    atomic_json(dest, info)
    return info


def _collect_app_diagnostics(root, dest):
    diagnostics = {}
    dest.mkdir(parents=True, exist_ok=True)
    for filename in ("market_hub.log", "ui_startup.log"):
        source = root / filename
        if source.is_file() and not source.is_symlink():
            diagnostics[filename] = _safe_log_tail(source, dest / filename)
        else:
            diagnostics[filename] = {"status": "absent"}
    # Do not export unvalidated process JSON: it can contain arbitrary content.
    process = root / "market_hub_process.json"
    if process.is_file() and not process.is_symlink():
        try:
            record = load_json(process)
            if isinstance(record, dict):
                atomic_json(dest / "market_hub_process.json", {
                    "pid": record.get("pid") if type(record.get("pid")) is int else None,
                    "created": record.get("created") if type(record.get("created")) in (int, float)
                    and math.isfinite(record["created"]) else None,
                })
                diagnostics["market_hub_process.json"] = {"status": "included"}
        except (OSError, UnicodeError, ValueError):
            diagnostics["market_hub_process.json"] = {"status": "unreadable"}
    diagnostics["market_cache.sqlite"] = _public_market_diagnostics(
        root, dest / "market_cache_diagnostics.json"
    )
    atomic_json(dest / "diagnostics_manifest.json", diagnostics)
    return diagnostics


def export_current_report(root, target, *, life=None, allow_empty=False, session_info=None):
    """Snapshot the explicitly selected app root; never stop/restart its workers."""
    root = Path(root).resolve(strict=True)
    if not (root / "MY_BOTS").is_dir() or (root / "MY_BOTS").is_symlink():
        raise ValueError("Выберите папку программы, в которой находится MY_BOTS")
    target = Path(target).resolve()
    if target.suffix.lower() != ".zip" or target.is_relative_to(
        Path(root).resolve() / "MY_BOTS"
    ):
        raise ValueError("Сохраните ZIP отчёта вне папки MY_BOTS")
    with tempfile.TemporaryDirectory(
        prefix=".bcc-current-report-", dir=target.parent
    ) as temp:
        folder = Path(temp) / "current_bots_report"
        report = collect(root, folder / "paper_bots", stop_workers=False, life=life)
        if not report["bots"] and not allow_empty:
            raise ValueError(
                "В выбранной папке нет ботов. Для проверочного запуска выберите его папку внутри check_workspaces, содержащую MY_BOTS"
            )
        diagnostics = _collect_app_diagnostics(root, folder / "diagnostics")
        summary = {
            "app_version": VERSION,
            "at": datetime.now(timezone.utc).isoformat(),
            "platform": platform.platform(),
            "data_root": str(Path(root).resolve()),
            "read_only": True,
            "workers_stopped_for_report": False,
            "bot_count": len(report["bots"]),
            "errors": report["errors"],
            "release_ready": False,
            "diagnostics_included": diagnostics,
            "session_info": session_info,
        }
        atomic_json(folder / "report_summary.json", summary)
        pending = Path(temp) / "report.zip"
        with zipfile.ZipFile(pending, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(folder.rglob("*")):
                if file.is_file():
                    archive.write(
                        file, Path("current_bots_report") / file.relative_to(folder)
                    )
        os.replace(pending, target)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = collect(args.root, args.output)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
