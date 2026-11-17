"""One user-approved BCC-to-Kie Sonnet 5.5 connection test.

This is deliberately NOT a general Claude executor. It sends exactly one
hardcoded harmless prompt, without trading data, tool calls, retry or streaming.
The sole authorized use is the user's 2026-11-17 approval of ONE paid test with
a locally reserved ceiling of USD 0.01. The actual provider bill is external.

The Windows vault key is never printed, passed as an argument, stored in
GitHub, or included in reports.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.bcc_claude_budget import BudgetLedger, SpendingDenied, quoted_micro_usd
from tools.bcc_kie_credentials import WindowsCredentialStore
from tools.bcc_kie_protocol import (
    API_HOST, API_PATH, MODEL_ID, ProtocolBlocked,
    auth_headers, build_payload, parse_response,
)

REQUEST_ID = "BCC-KIE-SONNET55-CONNECTIVITY-20261117-V1"
APPROVAL_ID = "BCC-KIE-ONE-TEST-USER-AUTHORIZED-20261117"
APPROVAL_PHRASE = "APPROVE 0.01"
PROMPT = "Reply with exactly BCC_KIE_CONNECTED and nothing else."
EXPECTED_ANSWER = "BCC_KIE_CONNECTED"
OUTPUT_LIMIT_TOKENS = 32
RESERVED_INPUT_TOKENS = 12_000
RESERVED_OUTPUT_TOKENS = 100
RESERVE_MICRO_USD = quoted_micro_usd(RESERVED_INPUT_TOKENS, RESERVED_OUTPUT_TOKENS)
assert RESERVE_MICRO_USD == 10_000
MAX_REPLY_BYTES = 131_072


class OneShotError(RuntimeError):
    """Sanitized error with no API key, user prompt, or provider body."""


def _post_once(key: str, payload: dict[str, Any],
               *, connection_factory: Callable[..., Any] = http.client.HTTPSConnection
               ) -> tuple[int, dict[str, Any] | None]:
    """At most one HTTPS POST to an immutable host/path; never follow redirects."""
    if payload != build_payload(PROMPT, max_tokens=OUTPUT_LIMIT_TOKENS):
        raise OneShotError("Unexpected payload: request blocked")
    conn = None
    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > 4096:
            raise OneShotError("Payload too large")
        conn = connection_factory(API_HOST, timeout=20)
        conn.request("POST", API_PATH, body=body, headers=auth_headers(key))
        response = conn.getresponse()
        status = int(response.status)
        if status != 200:
            # Do not print provider error bodies; they may echo confidential data.
            return status, None
        data = response.read(MAX_REPLY_BYTES + 1)
        if len(data) > MAX_REPLY_BYTES:
            raise OneShotError("Response too large")
        result = json.loads(data.decode("utf-8"))
        if not isinstance(result, dict):
            raise OneShotError("Unexpected response")
        return status, result
    except OneShotError:
        raise
    except Exception:
        # No repr(exception) because network errors can include secret material.
        raise OneShotError("Connection or response decoding failed; do NOT retry") from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def run_one_shot(*, approval_text: str, store: Any, ledger_path: Path,
                 send: Callable[[str, dict[str, Any]], tuple[int, dict[str, Any] | None]]
                 = _post_once) -> dict[str, Any]:
    """One authorized attempt, persistent hold BEFORE network I/O.

    Returns sanitized data only. Never settles a real charge from model-reported
    usage; the $0.01 hold remains until verified provider billing reconciliation.
    """
    if approval_text != APPROVAL_PHRASE:
        raise OneShotError("User confirmation missing; request not attempted")
    payload = build_payload(PROMPT, max_tokens=OUTPUT_LIMIT_TOKENS)
    # Confirm local key access without ever displaying it.
    key = store.load_for_agent()
    if not isinstance(key, str) or not key:
        raise OneShotError("Windows credential unavailable")
    ledger_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    verifier = lambda approval, request, amount: (
        approval == APPROVAL_ID and request == REQUEST_ID and amount == 10_000
    )
    ledger = BudgetLedger(ledger_path, approval_verifier=verifier)
    # A second attempt cannot be admitted, including following 401, 503,
    # timeout, process crash or HTTP success: same request/approval ID.
    ledger.reserve(
        REQUEST_ID, approval_id=APPROVAL_ID,
        max_input_tokens=RESERVED_INPUT_TOKENS,
        max_output_tokens=RESERVED_OUTPUT_TOKENS,
    )
    status, raw = send(key, payload)  # SINGLE attempt; no retry.
    if status != 200 or raw is None:
        return {"result": "HTTP_ERROR_NO_RETRY", "http_status": status,
                "attempts_allowed_remaining": 0,
                "reservation_usd": "0.010000", "provider_charge": "UNKNOWN"}
    parsed = parse_response(raw, max_output_tokens=OUTPUT_LIMIT_TOKENS)
    if (parsed.usage.input_tokens + parsed.usage.cache_5m_tokens
            + parsed.usage.cache_1h_tokens + parsed.usage.cache_read_tokens
            > RESERVED_INPUT_TOKENS):
        raise OneShotError("Input token usage exceeded locally reserved ceiling")
    if parsed.usage.estimated_micro_usd > RESERVE_MICRO_USD:
        raise OneShotError("Usage estimate exceeded reserved amount; reconcile provider bill")
    answer = "".join(block["text"] for block in raw["content"])
    return {
        "result": "CONNECTED" if answer.strip() == EXPECTED_ANSWER
                  else "VALID_RESPONSE_UNEXPECTED_TEXT",
        "http_status": status, "model": MODEL_ID,
        "input_tokens": parsed.usage.input_tokens,
        "output_tokens": parsed.usage.output_tokens,
        "cache_read_tokens": parsed.usage.cache_read_tokens,
        "cache_write_5m_tokens": parsed.usage.cache_5m_tokens,
        "cache_write_1h_tokens": parsed.usage.cache_1h_tokens,
        "estimated_usd": f"{parsed.usage.estimated_micro_usd / 1_000_000:.6f}",
        "provider_charge": "UNVERIFIED",
        "reservation_usd": "0.010000",
        "attempts_allowed_remaining": 0,
    }


def main() -> int:
    if os.name != "nt":
        print("BLOCKED: real one-shot test runs only on the user's Windows PC.")
        return 2
    print("BCC Kie.ai one-time Claude Sonnet 5.5 connection test")
    print("One harmless message, 32 maximum output tokens, no tools or retries.")
    print("Locally reserved amount: USD 0.01; exact Kie.ai invoice cannot be capped here.")
    print("The unused reservation will remain held pending provider reconciliation.")
    print("Only one attempt is allowed, even if the server returns an error.")
    print("Type APPROVE 0.01 to send it once, anything else cancels.")
    try:
        confirmation = input("> ")
    except (KeyboardInterrupt, EOFError):
        print("CANCELLED: no request sent.")
        return 1
    if confirmation != APPROVAL_PHRASE:
        print("CANCELLED: no request sent.")
        return 1
    try:
        output = run_one_shot(
            approval_text=confirmation,
            store=WindowsCredentialStore(),
            ledger_path=Path(os.environ["LOCALAPPDATA"]) / "BCCDevAgent" / "claude_budget.sqlite3",
        )
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        if output["result"] != "CONNECTED":
            print("NOT CONFIRMED. Do NOT retry; inspect Kie.ai billing first.")
            return 1
        print("Confirmed one Kie.ai Claude response. No further calls authorized.")
        return 0
    except (OneShotError, ProtocolBlocked, SpendingDenied, OSError, KeyError, ValueError):
        print("BLOCKED OR UNKNOWN: no automatic retry; verify provider billing.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
