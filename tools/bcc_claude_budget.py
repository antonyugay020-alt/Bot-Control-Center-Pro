"""Offline, fail-closed spending ledger for an optional BCC development agent.

No API/client/network code belongs here. Importing this module cannot issue an API call.
The caller must supply a trusted authorization verifier; without one, every paid
reservation is rejected. The authorized usage must be capped by the transport.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Callable


MICRO_USD = 1_000_000
SONNET_INPUT_PER_MILLION_USD = Decimal("0.80")
SONNET_OUTPUT_PER_MILLION_USD = Decimal("4.00")
HARD_MONTHLY_LIMIT_MICRO = 20 * MICRO_USD
ADMISSION_LIMIT_MICRO = 18 * MICRO_USD  # A $2 safety reserve


class SpendingDenied(RuntimeError):
    """A request is not admitted; no paid provider call may follow."""


def quoted_micro_usd(max_input_tokens: int, max_output_tokens: int) -> int:
    """Conservative Sonnet 5.5 estimate from 2026-11-17 Kie.ai screenshots.

    Cache, provider charges and endpoint-specific token accounting are unverified.
    This bound cannot by itself guarantee the provider's final invoice.
    """
    if type(max_input_tokens) is not int or type(max_output_tokens) is not int:
        raise ValueError("token caps must be plain integers")
    if max_input_tokens <= 0 or max_output_tokens <= 0:
        raise ValueError("token caps must be positive")
    amount = (Decimal(max_input_tokens) * SONNET_INPUT_PER_MILLION_USD
              + Decimal(max_output_tokens) * SONNET_OUTPUT_PER_MILLION_USD)
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class Reservation:
    request_id: str
    period: str
    held_micro_usd: int


# Verifier receives only the opaque approval ID, request ID, and exact maximum spend.
# It MUST be provided by a trusted user-approval boundary, not by the AI prompt.
ApprovalVerifier = Callable[[str, str, int], bool]


class BudgetLedger:
    """Transactionally hold money before any permitted provider request.

    No real API adapter exists in this module. Incomplete/unknown requests retain
    their holds across UTC calendar-month changes until verified settlement.
    """

    def __init__(self, db_path: str | Path, approval_verifier: ApprovalVerifier | None = None):
        self.db_path = str(db_path)
        self.approval_verifier = approval_verifier
        with closing(self._connect()) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS claude_spending (
                request_id TEXT PRIMARY KEY,
                period TEXT NOT NULL,
                approval_id TEXT NOT NULL UNIQUE,
                hold_micro INTEGER NOT NULL CHECK(hold_micro > 0),
                charged_micro INTEGER,
                status TEXT NOT NULL CHECK(status IN ('HELD','SETTLED')),
                CHECK(charged_micro IS NULL OR charged_micro >= 0)
            )""")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=20, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 20000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _other_model_occupied(conn: sqlite3.Connection, period: str) -> int:
        """Include new Kie model holds in the original Claude admission gate.

        Migration is optional for old installations. Once the table exists,
        a malformed row must block all spending, not disappear from SUM().
        """
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kie_model_spending'"
        ).fetchone()
        if exists is None:
            return 0
        invalid = conn.execute("""SELECT 1 FROM kie_model_spending
            WHERE (status='HELD' AND charged_micro IS NOT NULL)
               OR (status='SETTLED' AND charged_micro IS NULL) LIMIT 1""").fetchone()
        if invalid:
            raise SpendingDenied("Model spending ledger has invalid settlement records")
        amount = conn.execute("""SELECT COALESCE(SUM(
            CASE WHEN status='HELD' THEN hold_micro ELSE charged_micro END),0)
            FROM kie_model_spending WHERE status='HELD' OR period=?""",
            (period,)).fetchone()[0]
        if type(amount) is not int or amount < 0:
            raise SpendingDenied("Invalid shared provider spending total")
        return amount

    @staticmethod
    def _period(now: datetime | None) -> str:
        if now is None:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("period must use timezone-aware UTC time")
        return now.astimezone(timezone.utc).strftime("%Y-%m")

    def reserve(self, request_id: str, *, approval_id: str = "", max_input_tokens: int,
                max_output_tokens: int, now: datetime | None = None) -> Reservation:
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("invalid request ID")
        if not isinstance(approval_id, str) or not (1 <= len(approval_id) <= 128) or not approval_id.strip():
            raise SpendingDenied("explicit user approval is missing")
        cost = quoted_micro_usd(max_input_tokens, max_output_tokens)
        if cost > ADMISSION_LIMIT_MICRO:
            raise SpendingDenied("single request exceeds admission limit")
        if self.approval_verifier is None or not self.approval_verifier(approval_id, request_id, cost):
            raise SpendingDenied("approval not verified by trusted host")
        period = self._period(now)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Unresolved holds from ANY month continue to consume the pool.
                # Settled costs are counted in their own billing month.
                occupied = conn.execute("""SELECT COALESCE(SUM(
                  CASE WHEN status='HELD' THEN hold_micro ELSE charged_micro END
                ),0) FROM claude_spending
                WHERE status='HELD' OR period=?""", (period,)).fetchone()[0]
                occupied += self._other_model_occupied(conn, period)
                if occupied + cost > ADMISSION_LIMIT_MICRO:
                    raise SpendingDenied("shared monthly admission budget exhausted")
                if conn.execute("SELECT 1 FROM claude_spending WHERE request_id=?",
                                (request_id,)).fetchone():
                    raise SpendingDenied("request already reserved; never auto-retry unknown charge")
                if conn.execute("SELECT 1 FROM claude_spending WHERE approval_id=?",
                                (approval_id,)).fetchone():
                    raise SpendingDenied("approval already used for another request")
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kie_model_spending'"
                ).fetchone():
                    if conn.execute(
                        "SELECT 1 FROM kie_model_spending WHERE request_id=? OR approval_id=?",
                        (request_id, approval_id)
                    ).fetchone():
                        raise SpendingDenied("request or approval already used by another Kie model")
                conn.execute("""INSERT INTO claude_spending
                  (request_id,period,approval_id,hold_micro,charged_micro,status)
                  VALUES(?,?,?,?,NULL,'HELD')""", (request_id, period, approval_id, cost))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return Reservation(request_id, period, cost)

    def settle(self, request_id: str, charged_micro: int) -> None:
        """Only caller with verified billing evidence may settle a hold.

        Unknown, timed-out or failed responses MUST NOT be settled as free.
        In the live adapter, verify provider receipts before calling this method.
        """
        if type(charged_micro) is not int or charged_micro < 0:
            raise ValueError("invalid charge")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT hold_micro, status FROM claude_spending WHERE request_id=?",
                                   (request_id,)).fetchone()
                if row is None or row[1] != "HELD":
                    raise SpendingDenied("unrecognized or already settled request")
                if charged_micro > row[0]:
                    raise SpendingDenied("provider amount exceeds reserved maximum; reconciliation needed")
                conn.execute("""UPDATE claude_spending
                  SET charged_micro=?,status='SETTLED' WHERE request_id=?""",
                             (charged_micro, request_id))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def accounting(self, now: datetime | None = None) -> dict[str, int]:
        period = self._period(now)
        with closing(self._connect()) as conn:
            charged = conn.execute("""SELECT COALESCE(SUM(charged_micro),0)
                FROM claude_spending WHERE period=? AND status='SETTLED'""", (period,)).fetchone()[0]
            held = conn.execute("""SELECT COALESCE(SUM(hold_micro),0)
                FROM claude_spending WHERE status='HELD'""").fetchone()[0]
            other_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kie_model_spending'"
            ).fetchone()
            if other_exists:
                # The historical endpoint now shares the same $18 admission
                # pool. Never display fictitious available funds.
                self._other_model_occupied(conn, period)
                charged += conn.execute("""SELECT COALESCE(SUM(charged_micro),0)
                    FROM kie_model_spending WHERE period=? AND status='SETTLED'""",
                    (period,)).fetchone()[0]
                held += conn.execute("""SELECT COALESCE(SUM(hold_micro),0)
                    FROM kie_model_spending WHERE status='HELD'""").fetchone()[0]
        return {
            "charged_micro_usd": charged,
            "unresolved_holds_micro_usd": held,
            "available_admission_micro_usd": max(0, ADMISSION_LIMIT_MICRO - charged - held),
            "hard_monthly_limit_micro_usd": HARD_MONTHLY_LIMIT_MICRO,
        }
