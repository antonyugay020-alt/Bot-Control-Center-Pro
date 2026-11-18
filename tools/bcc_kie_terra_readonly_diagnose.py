"""Read-only Terra one-shot incident examiner for 2026-11-18.

No Kie key or credential APIs. No network, paid transport, Git writes,
ledger settlement, VACUUM, ATTACH, file creation or state mutation.
Prints only sanitized state labels and no personal/trading data.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3
from typing import Any

REQUEST_ID = "BCC-KIE-TERRA-CONNECTIVITY-20261118-V1"
APPROVAL_ID = "BCC-KIE-TERRA-ONE-TEST-LOCAL-CONFIRM-20261118-V1"
SENTINEL = "terra_one_shot_20261118_v1.attempted"


def inspect(state_dir: Path) -> dict[str, Any]:
    """Read only an explicit user-local BCCDevAgent directory."""
    base = {"test": "TERRA_ONE_SHOT_20261118",
            "new_paid_requests_allowed": False,
            "provider_request_confirmed": False,
            "provider_charge_verified": False,
            "secret_read": False, "network_used": False}
    if (not isinstance(state_dir, Path) or state_dir.name != "BCCDevAgent"
            or not state_dir.is_dir() or state_dir.is_symlink()):
        return {**base, "stage": "DEVELOPER_STATE_MISSING_OR_INVALID"}
    marker = state_dir / SENTINEL
    if marker.is_symlink():
        return {**base, "stage": "UNKNOWN_MARKER_LINKED"}
    has_marker = marker.is_file()
    if marker.exists() and not has_marker:
        return {**base, "stage": "UNKNOWN_MARKER_INVALID"}
    db = state_dir / "claude_budget.sqlite3"
    if db.is_symlink():
        return {**base, "stage": "UNKNOWN_LEDGER_LINKED",
                "attempt_marker_present": has_marker}
    if not db.is_file():
        return {**base, "stage": "MARKER_WITHOUT_LEDGER" if has_marker
                else "NO_LEDGER", "attempt_marker_present": has_marker}
    try:
        uri = db.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=3)) as conn:
            conn.execute("PRAGMA query_only=ON")
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='kie_model_spending'").fetchone() is not None
            if not has_table:
                return {**base, "stage": "MARKER_WITHOUT_MODEL_TABLE"
                        if has_marker else "NO_MODEL_TABLE",
                        "attempt_marker_present": has_marker}
            rows = conn.execute(
                "SELECT status, hold_micro, charged_micro, model"
                " FROM kie_model_spending"
                " WHERE request_id=? AND approval_id=?",
                (REQUEST_ID, APPROVAL_ID),
            ).fetchall()
            if len(rows) > 1:
                return {**base, "stage": "UNKNOWN_DUPLICATE_RECORD",
                        "attempt_marker_present": has_marker}
            if not rows:
                return {**base, "stage": "MARKER_WITHOUT_TERRA_HOLD"
                        if has_marker else "NO_TERRA_HOLD",
                        "attempt_marker_present": has_marker}
            status, hold, charge, model = rows[0]
            if model != "terra" or status not in ("HELD", "SETTLED"):
                return {**base, "stage": "UNKNOWN_LEDGER_RECORD",
                        "attempt_marker_present": has_marker}
            if type(hold) is not int or hold <= 0:
                return {**base, "stage": "UNKNOWN_HOLD_VALUE",
                        "attempt_marker_present": has_marker}
            if ((status == "HELD" and charge is not None)
                    or (status == "SETTLED" and
                        (type(charge) is not int or charge < 0))):
                return {**base, "stage": "UNKNOWN_SETTLEMENT_STATE",
                        "attempt_marker_present": has_marker}
            stage = ("ATTEMPT_GATE_REACHED_HTTP_UNKNOWN" if has_marker
                     else "HOLD_BEFORE_SENTINEL")
            return {**base, "stage": stage,
                    "attempt_marker_present": has_marker,
                    "local_ledger_status": status,
                    "local_held_usd": f"{hold/1_000_000:.6f}",
                    "local_recorded_charge_usd":
                    None if charge is None else f"{charge/1_000_000:.6f}"}
    except (sqlite3.Error, OSError, ValueError):
        return {**base, "stage": "UNKNOWN_READ_ERROR",
                "attempt_marker_present": has_marker}


def main() -> int:
    if os.name != "nt" or not os.environ.get("LOCALAPPDATA"):
        print(json.dumps({"stage": "WINDOWS_LOCAL_USER_REQUIRED",
                          "network_used": False,
                          "new_paid_requests_allowed": False}))
        return 2
    base = Path(os.environ["LOCALAPPDATA"]) / "BCCDevAgent"
    result = inspect(base)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["stage"] in (
        "NO_TERRA_HOLD", "HOLD_BEFORE_SENTINEL",
        "ATTEMPT_GATE_REACHED_HTTP_UNKNOWN", "NO_LEDGER", "NO_MODEL_TABLE"
    ) else 2


if __name__ == "__main__":
    raise SystemExit(main())
