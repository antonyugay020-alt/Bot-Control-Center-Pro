"""Atomic, cross-model Kie.ai development spending admission (NO NETWORK).

Uses the existing BCCDevAgent SQLite budget file and accounts for *all*
unresolved legacy Sonnet reservations. No provider call, key access, API
authorization UI, invoice settlement or automatic retry is present.

Prices from the catalog are not treated as verified spend caps. A host with
trusted explicit user approval must provide the exact reserved micro-USD
amount; this module does not grant approval itself.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import re
from typing import Callable

from tools.bcc_claude_budget import (
    ADMISSION_LIMIT_MICRO, HARD_MONTHLY_LIMIT_MICRO, BudgetLedger, SpendingDenied,
)
from tools.bcc_kie_models import ModelNotReady, profile

SIMULATION_ONLY = True
TABLE = "kie_model_spending"
COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
TASK_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class ModelReservation:
    request_id: str
    model: str
    reserved_micro_usd: int
    month_utc: str
    source_sha: str
    prompt_sha256: str
    paid_transport_enabled: bool = False


def _ident(value: str, field: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not value.strip():
        raise SpendingDenied("Invalid " + field)
    if "\x00" in value or "\r" in value or "\n" in value:
        raise SpendingDenied("Invalid " + field)
    return value


def _digest(value: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise SpendingDenied("Invalid pinned task or source digest")
    return value


def _micro(value: int) -> int:
    if type(value) is not int or not 1 <= value <= ADMISSION_LIMIT_MICRO:
        raise SpendingDenied("Invalid explicit per-call reservation ceiling")
    return value


def _month(now: datetime | None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise SpendingDenied("Month must use an aware UTC-compatible timestamp")
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def _sum_occupied(conn: sqlite3.Connection, table: str, month: str) -> int:
    if conn.execute(
        f"""SELECT 1 FROM {table} WHERE
        (status='HELD' AND charged_micro IS NOT NULL)
        OR (status='SETTLED' AND charged_micro IS NULL) LIMIT 1"""
    ).fetchone():
        raise SpendingDenied("Invalid spending ledger settlement state")
    amount = conn.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN status='HELD'
                 THEN hold_micro ELSE charged_micro END),0)
            FROM {table} WHERE status='HELD' OR period=?""", (month,)
    ).fetchone()[0]
    if type(amount) is not int or amount < 0:
        raise SpendingDenied("Invalid stored spending record")
    return amount


def _exists(conn: sqlite3.Connection, table: str, field: str, value: str) -> bool:
    # Only hardcoded table names and column names reach this SQL.
    return conn.execute(
        f"SELECT 1 FROM {table} WHERE {field}=? LIMIT 1", (value,)
    ).fetchone() is not None


class KieModelBudget:
    """Model-neutral reservations sharing one SQLite file with Claude.

    This object is *not* a paid-call token. It never opens a socket or settles
    from token usage. Unknown provider bills remain HELD indefinitely.
    """

    def __init__(self, db_path: str | Path,
                 approval_verifier: Callable[[str, str, str, int, str, str], bool] | None = None):
        self.db_path = Path(db_path)
        self.approval_verifier = approval_verifier

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=20, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=20000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def initialize(self) -> None:
        """Migration only on an explicitly selected developer budget DB."""
        if self.db_path.is_symlink() or self.db_path.parent.is_symlink():
            raise SpendingDenied("Linked budget database or directory rejected")
        if not self.db_path.parent.is_dir():
            raise SpendingDenied("Budget parent does not exist")
        # Reuse the canonical legacy schema, never create a competing money DB.
        BudgetLedger(self.db_path)
        with closing(self._open()) as conn:
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                request_id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL UNIQUE,
                model TEXT NOT NULL,
                source_sha TEXT NOT NULL,
                prompt_sha256 TEXT NOT NULL,
                period TEXT NOT NULL,
                hold_micro INTEGER NOT NULL CHECK (hold_micro>0),
                charged_micro INTEGER,
                status TEXT NOT NULL CHECK (status IN ('HELD','SETTLED')),
                CHECK(charged_micro IS NULL OR charged_micro>=0)
            )""")
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}
            if not {"source_sha", "prompt_sha256"}.issubset(columns):
                raise SpendingDenied("Legacy model table lacks task binding; reviewed migration required")

    def snapshot(self, *, now: datetime | None = None) -> dict[str, int | str | bool]:
        month = _month(now)
        # Read-only access; this method never creates a database or schema.
        if not self.db_path.is_file() or self.db_path.is_symlink():
            raise SpendingDenied("Budget database not found or linked")
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
                conn.execute("PRAGMA query_only=ON")
                for table in ("claude_spending", TABLE):
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                    (table,)).fetchone() is None:
                        raise SpendingDenied("Required legacy or model spending ledger missing")
                legacy = _sum_occupied(conn, "claude_spending", month)
                models = _sum_occupied(conn, TABLE, month)
                occupied = legacy + models
                if occupied > HARD_MONTHLY_LIMIT_MICRO:
                    raise SpendingDenied("Recorded Kie spending exceeds hard monthly budget")
                return {"month_utc": month, "legacy_occupied_micro_usd": legacy,
                        "model_occupied_micro_usd": models,
                        "total_occupied_micro_usd": occupied,
                        "remaining_admission_micro_usd": max(0, ADMISSION_LIMIT_MICRO-occupied),
                        "hard_limit_micro_usd": HARD_MONTHLY_LIMIT_MICRO,
                        "paid_transport_enabled": False}
        except sqlite3.Error:
            raise SpendingDenied("Budget database unavailable; no spending allowed") from None

    def reserve(self, *, request_id: str, approval_id: str, model: str,
                max_micro_usd: int, source_sha: str, prompt_sha256: str,
                now: datetime | None = None) -> ModelReservation:
        """One atomic admission; host approval required, no provider call.

        The requested maximum is a *locally reserved* amount, not a Kie.ai
        hard invoice ceiling. Actual paid execution remains impossible.
        """
        request_id = _ident(request_id, "request ID")
        approval_id = _ident(approval_id, "approval ID")
        limit = _micro(max_micro_usd)
        source_sha = _digest(source_sha, COMMIT_SHA)
        prompt_sha256 = _digest(prompt_sha256, TASK_DIGEST)
        month = _month(now)
        try:
            profile(model)
        except ModelNotReady:
            raise SpendingDenied("Unknown model profile") from None
        if self.approval_verifier is None or not self.approval_verifier(
            approval_id, request_id, model, limit, source_sha, prompt_sha256
        ):
            raise SpendingDenied("No trusted exact user approval")
        if not self.db_path.is_file() or self.db_path.is_symlink():
            raise SpendingDenied("Initialize the explicit developer ledger first")
        try:
            with closing(self._open()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    # A missing/migrated legacy or model table fails closed.
                    occupied = (
                        _sum_occupied(conn, "claude_spending", month)
                        + _sum_occupied(conn, TABLE, month)
                    )
                    if occupied + limit > ADMISSION_LIMIT_MICRO:
                        raise SpendingDenied("Combined Kie.ai admission limit exceeded")
                    if any(_exists(conn, table, "request_id", request_id)
                           or _exists(conn, table, "approval_id", approval_id)
                           for table in ("claude_spending", TABLE)):
                        raise SpendingDenied("Request or user approval already consumed")
                    conn.execute(
                        f"INSERT INTO {TABLE} "
                        "(request_id, approval_id, model, source_sha, prompt_sha256, period, hold_micro, charged_micro, status) "
                        "VALUES (?,?,?,?,?,?,?,NULL,'HELD')",
                        (request_id, approval_id, model, source_sha, prompt_sha256, month, limit),
                    )
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
        except sqlite3.Error:
            raise SpendingDenied("Atomic budget reservation failed") from None
        return ModelReservation(request_id, model, limit, month, source_sha, prompt_sha256)

    def settle_from_unverified_usage(self, *_args, **_kwargs) -> None:
        """No self-reported provider tokens can settle a monetary reservation."""
        raise SpendingDenied("Verified provider invoice reconciliation is required")


def send_paid_request(*_args, **_kwargs) -> None:
    raise SpendingDenied("Model gateway has no authorized paid network transport")
