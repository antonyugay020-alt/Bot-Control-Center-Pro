"""Offline OpenAI API budget and authorization *simulation*, never transport.

This ledger is separate from the historical Kie.ai $20 pool. No OpenAI monthly
limit has been approved; callers MUST specify a positive limit, and it never
becomes a paid-call permit. A disposable Codespace SQLite ledger is not proof
of provider spending. No credentials, HTTPS, or provider SDK exist here.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from typing import Callable

MICRO_USD = 1_000_000
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")
SCHEMA = 1
SIMULATION_ONLY = True
PAID_TRANSPORT_ENABLED = False


class OpenAIDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class Hold:
    request_id: str
    source_sha: str
    prompt_sha256: str
    model: str
    period: str
    hold_micro_usd: int
    paid_transport_enabled: bool = False


def _month(now: datetime | None) -> str:
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise OpenAIDenied("Accounting clock must be timezone aware")
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def _positive(value: int, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 1_000_000_000:
        raise OpenAIDenied("Invalid " + label)
    return value


def _match(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise OpenAIDenied("Invalid " + label)
    return value


class OpenAIOfflineBudget:
    """SQLite-backed simulation with explicit caps and no provider authorization.

    Never create a new database on an unknown filesystem implicitly. A trusted
    host must separately prove identity, original approval and provider billing
    before a future transport can be designed. A callable verifier here only
    tests a future host contract, not the user's real-world consent.
    """

    def __init__(self, db_path: Path | str,
                 verifier: Callable[[str, str, str, str, str, int], bool] | None = None):
        self.path = Path(db_path)
        self.verifier = verifier

    def _guard_file(self, *, create: bool = False) -> None:
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise OpenAIDenied("Linked budget location refused")
        if not self.path.parent.is_dir():
            raise OpenAIDenied("Budget parent does not exist")
        if not create and not self.path.is_file():
            raise OpenAIDenied("Budget ledger absent; fail closed")

    def _connect(self) -> sqlite3.Connection:
        self._guard_file()
        try:
            conn = sqlite3.connect(str(self.path), timeout=20, isolation_level=None)
            conn.execute("PRAGMA busy_timeout=20000")
            conn.execute("PRAGMA journal_mode=WAL")
            return conn
        except sqlite3.Error:
            raise OpenAIDenied("Budget ledger unavailable") from None

    def initialize_simulation(self, *, monthly_cap_micro_usd: int) -> None:
        cap = _positive(monthly_cap_micro_usd, "explicit offline monthly cap")
        self._guard_file(create=True)
        if self.path.exists():
            raise OpenAIDenied("Existing budget file requires explicit migration review")
        # Exclusive create prevents silent reset or replacement of an existing ledger.
        with self.path.open("xb"):
            pass
        try:
            with closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("""CREATE TABLE ledger_meta (
                        schema_version INTEGER NOT NULL, cap_micro INTEGER NOT NULL)""")
                    conn.execute("INSERT INTO ledger_meta VALUES (?,?)", (SCHEMA, cap))
                    conn.execute("""CREATE TABLE openai_holds (
                        request_id TEXT PRIMARY KEY,
                        approval_id TEXT NOT NULL UNIQUE,
                        source_sha TEXT NOT NULL,
                        prompt_sha256 TEXT NOT NULL,
                        model TEXT NOT NULL,
                        period TEXT NOT NULL,
                        hold_micro INTEGER NOT NULL CHECK(hold_micro>0),
                        status TEXT NOT NULL CHECK(status='HELD')
                    )""")
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
        except sqlite3.Error:
            raise OpenAIDenied("Simulation ledger initialization failed") from None

    @staticmethod
    def _metadata(conn: sqlite3.Connection) -> int:
        try:
            rows = conn.execute(
                "SELECT schema_version, cap_micro FROM ledger_meta").fetchall()
            if len(rows) != 1 or rows[0][0] != SCHEMA:
                raise OpenAIDenied("Unknown or modified ledger schema")
            return _positive(rows[0][1], "stored cap")
        except sqlite3.Error:
            raise OpenAIDenied("Ledger metadata unavailable") from None

    @staticmethod
    def _occupied(conn: sqlite3.Connection) -> int:
        # Outstanding reservations from every billing month consume the cap.
        try:
            bad = conn.execute("""SELECT 1 FROM openai_holds
                WHERE status!='HELD' OR hold_micro<=0 LIMIT 1""").fetchone()
            if bad:
                raise OpenAIDenied("Unreconciled ledger state")
            row = conn.execute(
                "SELECT COALESCE(SUM(hold_micro),0) FROM openai_holds").fetchone()
            amount = row[0]
            if type(amount) is not int or amount < 0:
                raise OpenAIDenied("Corrupt ledger accounting")
            return amount
        except sqlite3.Error:
            raise OpenAIDenied("Ledger accounting unavailable") from None

    def snapshot(self) -> dict[str, int | str | bool]:
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA query_only=ON")
            cap = self._metadata(conn)
            held = self._occupied(conn)
            if held > cap:
                raise OpenAIDenied("Budget over cap; block")
            return {
                "status": "OFFLINE_SIMULATION_ONLY",
                "configured_cap_micro_usd": cap,
                "unresolved_holds_micro_usd": held,
                "remaining_micro_usd": cap - held,
                "paid_transport_enabled": False,
                "provider_invoice_reconciled": False,
            }

    def reserve_simulation(
        self, *, request_id: str, approval_id: str, source_sha: str,
        prompt_sha256: str, model: str, max_micro_usd: int,
        now: datetime | None = None,
    ) -> Hold:
        request_id = _match(request_id, IDENT, "request ID")
        approval_id = _match(approval_id, IDENT, "approval ID")
        source_sha = _match(source_sha, SHA40, "source SHA")
        prompt_sha256 = _match(prompt_sha256, SHA256, "prompt hash")
        model = _match(model, MODEL, "explicit model")
        amount = _positive(max_micro_usd, "explicit per-task maximum")
        period = _month(now)
        if self.verifier is None or not self.verifier(
                approval_id, request_id, model, source_sha, prompt_sha256, amount):
            raise OpenAIDenied("No trusted exact offline test approval")
        with closing(self._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cap = self._metadata(conn)
                    occupied = self._occupied(conn)
                    if occupied + amount > cap:
                        raise OpenAIDenied("Simulation monthly cap exceeded")
                    conn.execute("""INSERT INTO openai_holds
                        (request_id,approval_id,source_sha,prompt_sha256,
                         model,period,hold_micro,status)
                        VALUES (?,?,?,?,?,?,?,'HELD')""",
                        (request_id, approval_id, source_sha, prompt_sha256,
                         model, period, amount))
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            except sqlite3.IntegrityError:
                raise OpenAIDenied("Approval or request already consumed") from None
            except sqlite3.Error:
                raise OpenAIDenied("Atomic simulation reservation failed") from None
        return Hold(request_id, source_sha, prompt_sha256, model, period, amount)

    def settle(self, *_args, **_kwargs) -> None:
        raise OpenAIDenied("Only independently verified invoices may settle holds")

    def send_paid_request(self, *_args, **_kwargs) -> None:
        raise OpenAIDenied("OpenAI API transport is not implemented or approved")
