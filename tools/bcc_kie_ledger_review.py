"""Read-only, secret-free status for the BCC Kie.ai one-shot budget record.

This does not reconcile, settle, delete, or authorize any spending.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

REQUEST_ID = "BCC-KIE-SONNET55-CONNECTIVITY-20261117-V1"
APPROVAL_ID = "BCC-KIE-ONE-TEST-USER-AUTHORIZED-20261117"


def review(db_path: Path) -> dict[str, Any]:
    """Use SQLite URI mode=ro. Unknown data fails closed, without changing DB."""
    if not db_path.is_file():
        return {"state": "UNKNOWN_BLOCKED", "reason": "LEDGER_NOT_FOUND",
                "paid_calls_authorized": False}
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=3) as conn:
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute(
                "SELECT status, hold_micro, charged_micro, period "
                "FROM claude_spending WHERE request_id=? AND approval_id=?",
                (REQUEST_ID, APPROVAL_ID),
            ).fetchall()
            if len(rows) != 1:
                return {"state": "UNKNOWN_BLOCKED", "reason": "RECORD_NOT_UNIQUE",
                        "paid_calls_authorized": False}
            status, hold, charge, period = rows[0]
            if (status not in ("HELD", "SETTLED") or type(hold) is not int or hold <= 0
                    or not isinstance(period, str) or len(period) != 7):
                raise ValueError("Invalid one-shot ledger record")
            if status == "HELD" and charge is not None:
                raise ValueError("Unexpected charge while held")
            if status == "SETTLED" and (type(charge) is not int or charge < 0):
                raise ValueError("Invalid settlement")
            return {"state": "CONSUMED_HELD" if status == "HELD" else "CONSUMED_SETTLED",
                    "period_utc": period,
                    "reserved_usd": f"{hold / 1_000_000:.6f}",
                    "locally_recorded_charge_usd": None if charge is None else f"{charge / 1_000_000:.6f}",
                    "provider_invoice_verified": False,
                    "paid_calls_authorized": False}
    except (OSError, ValueError, sqlite3.Error):
        return {"state": "UNKNOWN_BLOCKED", "reason": "LEDGER_UNREADABLE",
                "paid_calls_authorized": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only BCC Kie one-shot status")
    parser.add_argument("--ledger", type=Path, help="For tests; never provide secrets")
    args = parser.parse_args(argv)
    if args.ledger is None:
        local = os.environ.get("LOCALAPPDATA")
        if os.name != "nt" or not local:
            print(json.dumps({"state": "UNKNOWN_BLOCKED", "reason": "WINDOWS_USER_REQUIRED",
                              "paid_calls_authorized": False}))
            return 2
        path = Path(local) / "BCCDevAgent" / "claude_budget.sqlite3"
    else:
        path = args.ledger
    result = review(path)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["state"].startswith("CONSUMED_") else 2


if __name__ == "__main__":
    raise SystemExit(main())
