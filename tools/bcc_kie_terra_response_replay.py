"""Offline-only replay of a previously received Kie.ai Terra Responses body.

Use strictly on a previously SAVED or locally supplied JSON response. This
module has NO HTTPS client, credential reader, SQLite access, user file reads,
GitHub actions, or trading operations. It does not permit another paid request.

The 2026-11-18 dashboard confirmed the expected TEXT, but a screenshot is
not a full provider JSON response and cannot establish the old local error.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

# With Python -I the script directory is not a project import root.
# Include exactly this independent checkout without loading user site packages.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.bcc_kie_terra_one_shot import (
    EXPECTED_TEXT, SafeOutcome, assess_terra_response,
)
from tools.bcc_kie_responses_protocol import ResponsesBlocked, MAX_RESPONSE_UTF8

MAX_REPLAY_BYTES = MAX_RESPONSE_UTF8
SUCCEEDED_WITHOUT_BILLING = frozenset((
    "CONNECTED",
    "CONTENT_CONFIRMED_USAGE_UNVERIFIED",
))


def replay_bytes(data: bytes) -> dict[str, Any]:
    """A non-sensitive status report. Never return or print raw model text.

    Even after a correct text match, only the provider's own invoice can
    reconcile prior spending. The local HOLD must remain untouched.
    """
    base = {
        "mode": "OFFLINE_RESPONSE_REPLAY",
        "model": "terra",
        "new_api_calls": 0,
        "new_paid_requests_authorized": False,
        "existing_hold_changed": False,
        "provider_invoice_verified": False,
        "full_windows_e2e_verified": False,
    }
    if type(data) is not bytes or not 1 <= len(data) <= MAX_REPLAY_BYTES:
        return {**base, "stage": "INVALID_REPLAY_SIZE", "text_verified": False}
    try:
        raw = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return {**base, "stage": "INVALID_REPLAY_JSON", "text_verified": False}
    if type(raw) is not dict:
        return {**base, "stage": "INVALID_REPLAY_ENVELOPE", "text_verified": False}
    try:
        result: SafeOutcome = assess_terra_response(raw)
    except (ResponsesBlocked, ValueError, TypeError):
        return {**base, "stage": "REJECTED_RESPONSE_SCHEMA", "text_verified": False}
    text_verified = result.result in SUCCEEDED_WITHOUT_BILLING
    if not text_verified:
        return {**base, "stage": result.result,
                "text_verified": False,
                "response_sha256": result.response_sha256}
    return {
        **base,
        "stage": result.result,
        "text_verified": True,
        "usage_receipt_compatible": result.result == "CONNECTED",
        "response_sha256": result.response_sha256,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        # Token usage is NOT a provider invoice or a USD spend cap.
    }


def main() -> int:
    """Read a pasted/redirected response ONLY from stdin, not from files.

    No command-line input is accepted, so user secrets cannot enter shell
    history through this route. This is not necessary to recognize the
    already demonstrated correct Kie.ai dashboard result.
    """
    if len(sys.argv) != 1 or sys.stdin.isatty():
        print(json.dumps({"stage": "PIPE_EXISTING_JSON_ONLY", "new_api_calls": 0}))
        return 2
    report = replay_bytes(sys.stdin.buffer.read(MAX_REPLAY_BYTES + 1))
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("text_verified") else 2


if __name__ == "__main__":
    raise SystemExit(main())
