"""Single, local-user-confirmed Kie.ai GPT-6.1 Terra connectivity attempt.

This is NOT a coding agent, generic API switch, price oracle or budget cap.
The user must type a new exact confirmation on their own Windows PC.
Unlike historical Sonnet/Claude, this attempt uses a NEW request ID and
the shared Kie SQLite ledger. All other paid functions remain blocked.

NO repository files, bot data, credentials, trading databases, or user tasks
are sent. NEVER retry a failed/unknown provider response automatically.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.bcc_claude_budget import SpendingDenied
from tools.bcc_kie_credentials import CredentialError, WindowsCredentialStore
from tools.bcc_kie_model_budget import KieModelBudget
from tools.bcc_kie_responses_protocol import (
    ResponsesBlocked, API_HOST, MAX_RESPONSE_UTF8,
    Request, build_request, parse_response, endpoint,
)
from tools.bcc_kie_responses_https import _headers, _validate_request

# This is a NEW authorization workflow, never the retired Claude test.
# The typed confirmation is mandatory and cannot be passed in the command line.
REQUEST_ID = "BCC-KIE-TERRA-CONNECTIVITY-20261118-V1"
APPROVAL_ID = "BCC-KIE-TERRA-ONE-TEST-LOCAL-CONFIRM-20261118-V1"
CONFIRM_PHRASE = "APPROVE TERRA 0.01"
SOURCE_SHA = "7bba4909235cc56e0777075422c705a948eb44c2"
PROMPT = "Reply with exactly BCC_TERRA_CONNECTED and no other text."
EXPECTED_TEXT = "BCC_TERRA_CONNECTED"
OUTPUT_CAP = 256
INPUT_CAP = 4096
RESERVE_MICRO_USD = 10_000
SENTINEL_FILENAME = "terra_one_shot_20261118_v1.attempted"
PROVIDER_BILLING_VERIFIED = False


class OneShotBlocked(RuntimeError):
    """Sanitized error; never include raw body, key or exception repr."""


def request_body() -> Request:
    req = build_request(PROMPT, model="terra", max_output_tokens=OUTPUT_CAP)
    if (req.host != "api.kie.ai" or req.path != "/codex/v1/responses"
            or endpoint("terra").request_model != "gpt-6-1-terra"):
        raise OneShotBlocked("Terra request contract changed; blocked")
    return req


def _send_one_https(
    secret: str, request: Request,
    *, connection_factory: Callable[..., Any] = http.client.HTTPSConnection
) -> tuple[int, dict[str, Any] | None]:
    """Only one HTTPS POST to the fixed documented Kie Terra endpoint.

    Internal, one-shot-only transport. No redirects, tool calls, web search,
    proxy fallback, retries or raw error bodies. Kept separate from the
    permanently disabled generic paid transport.
    """
    _validate_request(request)
    if request != request_body():
        raise OneShotBlocked("Unexpected request: blocked")
    headers = _headers(secret)
    connection = None
    try:
        connection = connection_factory(API_HOST, timeout=20)
        connection.request("POST", "/codex/v1/responses",
                           body=request.body, headers=headers)
        response = connection.getresponse()
        status = int(response.status)
        if status != 200:
            # Error bodies can reflect confidential information; do not read.
            return status, None
        raw_bytes = response.read(MAX_RESPONSE_UTF8 + 1)
        if type(raw_bytes) is not bytes or len(raw_bytes) > MAX_RESPONSE_UTF8:
            raise OneShotBlocked("Terra reply exceeds allowed size")
        parsed = json.loads(raw_bytes.decode("utf-8"))
        if type(parsed) is not dict:
            raise OneShotBlocked("Unexpected Terra response envelope")
        return status, parsed
    except (OneShotBlocked, ResponsesBlocked):
        raise
    except Exception:
        # Exception text may echo authorization headers or request body.
        raise OneShotBlocked("Terra transport/response failed; no retries") from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _write_attempt_sentinel(path: Path) -> None:
    """A second durable guard in addition to the atomic shared SQLite HOLD.

    Exclusive creation before network I/O: even after a local budget file
    is accidentally deleted or moved, an old approval cannot be reused
    silently. Never remove this marker as part of the application workflow.
    """
    if path.parent.is_symlink() or path.is_symlink() or not path.parent.is_dir():
        raise OneShotBlocked("Unexpected developer state directory")
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise OneShotBlocked("Terra one-time attempt already consumed") from None
    except OSError:
        raise OneShotBlocked("Could not create durable one-time attempt guard") from None
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write("CONSUMED; ONE ATTEMPT; DO NOT RETRY\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        # Fail closed. Sentinel is never automatically removed.
        raise OneShotBlocked("Could not persist attempt guard") from None


@dataclass(frozen=True)
class SafeOutcome:
    result: str
    http_status: int | None
    input_tokens: int | None
    output_tokens: int | None
    response_sha256: str | None
    charged_usd: str = "UNVERIFIED"
    reserved_usd: str = "0.010000"
    attempts_remaining: int = 0

    def as_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, sort_keys=True)


def perform_one_attempt(
    *, approval_text: str, store: Any, state_directory: Path,
    sender: Callable[[str, Request], tuple[int, dict[str, Any] | None]],
) -> SafeOutcome:
    """Interactive confirmation, atomic money hold, exclusive marker, one POST.

    The implementation is mockable for fully offline tests. The production
    sender exists only in the interactive Windows main() below.
    """
    if approval_text != CONFIRM_PHRASE:
        raise OneShotBlocked("Explicit Terra confirmation was not given")
    if not isinstance(state_directory, Path):
        raise OneShotBlocked("Invalid developer state path")
    if state_directory.name != "BCCDevAgent" or state_directory.is_symlink():
        raise OneShotBlocked("Unexpected developer state directory")
    if not state_directory.is_dir():
        raise OneShotBlocked("Developer state directory missing")
    marker = state_directory / SENTINEL_FILENAME
    if marker.exists() or marker.is_symlink():
        raise OneShotBlocked("This one-time Terra attempt has already been consumed")
    req = request_body()
    # Secret stays in this local Python process, never put in the ledger.
    secret = store.load_for_agent()
    if type(secret) is not str or not secret:
        raise OneShotBlocked("Kie.ai credential is unavailable")

    ledger_file = state_directory / "claude_budget.sqlite3"
    if ledger_file.is_symlink():
        raise OneShotBlocked("Linked developer budget is forbidden")
    # Initialization upgrades a single dedicated development ledger only,
    # NOT any BCC trading database.
    budget = KieModelBudget(
        ledger_file,
        approval_verifier=lambda approval, request, model, amount, source, digest: (
            approval == APPROVAL_ID and request == REQUEST_ID
            and model == "terra" and amount == RESERVE_MICRO_USD
            and source == SOURCE_SHA and digest == req.input_sha256
            and approval_text == CONFIRM_PHRASE
        ),
    )
    budget.initialize()
    budget.reserve(
        request_id=REQUEST_ID, approval_id=APPROVAL_ID, model="terra",
        max_micro_usd=RESERVE_MICRO_USD, source_sha=SOURCE_SHA,
        prompt_sha256=req.input_sha256,
    )
    # IMPORTANT: the reservation is committed before the marker and network.
    # If the process dies here, its unresolved HOLD blocks a retry.
    _write_attempt_sentinel(marker)
    http_status, raw = sender(secret, req)
    if http_status != 200 or raw is None:
        return SafeOutcome(
            result="HTTP_ERROR_NO_RETRY", http_status=http_status,
            input_tokens=None, output_tokens=None, response_sha256=None,
        )
    reply = parse_response(raw, model="terra", output_cap=OUTPUT_CAP,
                           input_cap=INPUT_CAP)
    attested_model = raw.get("model") in ("gpt-6-1-terra", "gpt-6.1-terra")
    result = ("CONNECTED" if reply.text.strip() == EXPECTED_TEXT and attested_model
              else "MODEL_NOT_ATTESTED" if not attested_model
              else "VALID_RESPONSE_UNEXPECTED_TEXT")
    return SafeOutcome(
        result=result,
        http_status=200, input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
        response_sha256=reply.text_sha256,
    )


def verify_preflight(root: Path) -> None:
    """Require a current clean SHA-matching full offline Windows PASS.

    Prevent directly invoking the paid Python entrypoint without first
    completing the guarded 211-case offline test. A user-provided report
    is not trusted unless its SHA equals this exact checkout HEAD.
    """
    if root.name != "BCC_AGENT_GATEWAY" or not (root / ".git").exists():
        raise OneShotBlocked("Isolated BCC agent checkout required")
    report = root / "QA" / "BCC_KIE_QA_SUMMARY.json"
    if not report.is_file() or report.stat().st_size > 16_384:
        raise OneShotBlocked("Full offline Windows QA proof is missing")
    try:
        result = json.loads(report.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        raise OneShotBlocked("Cannot validate offline QA evidence") from None
    if not isinstance(result, dict) or any((
        result.get("check") != "BCC_KIE_OFFLINE_QA",
        result.get("result") != "OFFLINE_PASS_ONLY",
        result.get("exit_code") != 0,
        result.get("source_branch") != "agent/bcc-work-claude-gateway-20261117",
        result.get("paid_provider_calls") != 0,
        result.get("github_actions_requested") != 0,
    )):
        raise OneShotBlocked("Offline QA was not a clean PASS")
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        clean = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise OneShotBlocked("Cannot verify pinned Git checkout") from None
    if (head.returncode != 0 or clean.returncode != 0
            or clean.stdout.strip() or len(head.stdout.strip()) != 40
            or result.get("source_sha") != head.stdout.strip()):
        raise OneShotBlocked("Offline QA does not match clean Git HEAD")


def main() -> int:
    if os.name != "nt" or ROOT.name != "BCC_AGENT_GATEWAY":
        print("BLOCKED: separate BCC_AGENT_GATEWAY Windows checkout required.")
        return 2
    if not sys.stdin.isatty():
        print("BLOCKED: an interactive Windows terminal is required.")
        return 2
    try:
        verify_preflight(ROOT)
    except OneShotBlocked:
        print("BLOCKED: full matching offline Windows QA required before paid test.")
        return 2
    state_root = os.environ.get("LOCALAPPDATA")
    if not state_root:
        print("BLOCKED: Windows local developer state directory missing.")
        return 2
    state_dir = Path(state_root) / "BCCDevAgent"
    print("Kie.ai GPT-6.1 Terra: ONE text-only API connectivity request.")
    print("Endpoint: POST https://api.kie.ai/codex/v1/responses")
    print("max_output_tokens=256; no tools, no streaming, no code or bot data.")
    print("NEW $0.01 local reservation. Old Claude hold remains unchanged.")
    print("IMPORTANT: local reserve is NOT a provider-enforced invoice cap.")
    print("Kie.ai Terra pricing quotes CONFLICT; charges/minimum fees unverified.")
    print("A server error, timeout or invalid response also consumes this attempt.")
    print("No automatic repeats, fallback, GitHub actions or agent execution.")
    print("Type exactly APPROVE TERRA 0.01, or anything else to cancel.")
    try:
        typed = input("> ")
    except (KeyboardInterrupt, EOFError):
        print("CANCELLED: no provider call.")
        return 1
    if typed != CONFIRM_PHRASE:
        print("CANCELLED: no provider call.")
        return 1
    try:
        outcome = perform_one_attempt(
            approval_text=typed, store=WindowsCredentialStore(),
            state_directory=state_dir, sender=_send_one_https,
        )
        print(outcome.as_json())
        if outcome.result == "CONNECTED":
            print("ONE Terra API answer confirmed; billing not yet verified.")
            return 0
        print("No confirmed Terra reply; DO NOT RETRY; inspect Kie.ai Logs.")
        return 1
    except (OneShotBlocked, SpendingDenied, ResponsesBlocked, CredentialError,
            OSError, ValueError):
        print("BLOCKED/UNKNOWN: never retry; check local ledger and Kie.ai bill.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
