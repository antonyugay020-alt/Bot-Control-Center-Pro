"""BCC development-only Kie.ai gateway: OFFLINE until provider contract is verified.

This module deliberately contains no HTTP client. The Windows credential is
used only by a future separately reviewed transport; this CLI never loads its
value, invokes an external process, modifies BCC, or sends a billable request.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable

# Python -I does not add the script's directory to sys.path.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODEL_LABEL = "Claude Sonnet 5.5 (Kie.ai)"
NETWORK_GATE_ENABLED = False
MAX_PROMPT_CHARS = 8192
MAX_REPLY_CHARS = 32768


class GatewayBlocked(RuntimeError):
    """Provider or requested operation is intentionally not enabled."""


@dataclass(frozen=True)
class PreparedRequest:
    task_sha256: str
    max_input_tokens: int
    max_output_tokens: int
    hold_micro_usd: int

    def public_summary(self) -> dict[str, Any]:
        return {"model": MODEL_LABEL, "task_sha256": self.task_sha256,
                "max_input_tokens": self.max_input_tokens,
                "max_output_tokens": self.max_output_tokens,
                "maximum_quoted_usd": f"{self.hold_micro_usd / 1_000_000:.6f}",
                "network_enabled": False, "user_approval_required": True}


def prepare(task: str, *, max_input_tokens: int = 16000,
            max_output_tokens: int = 4000) -> PreparedRequest:
    """Calculate a dry-run cost without printing or transmitting the task."""
    if not isinstance(task, str) or not task.strip():
        raise GatewayBlocked("Task text is empty")
    if len(task) > MAX_PROMPT_CHARS or "\x00" in task:
        raise GatewayBlocked("Task exceeds the safe local input boundary")
    from tools.bcc_claude_budget import quoted_micro_usd
    return PreparedRequest(
        hashlib.sha256(task.encode("utf-8")).hexdigest(),
        max_input_tokens,
        max_output_tokens,
        quoted_micro_usd(max_input_tokens, max_output_tokens),
    )


def normalize_reply(data: Any, max_output_tokens: int = 4000) -> dict[str, Any]:
    """Strict OFFLINE shape check; it does not prove real Kie API compatibility.

    Accept only sample Anthropic Messages or OpenAI Chat Completions text
    envelopes. Tool execution, streaming, hidden reasoning and multimodal
    support require a separate verified contract and are not assumed.
    """
    if not isinstance(data, dict):
        raise GatewayBlocked("Response is not an object")
    usage = data.get("usage")
    if not isinstance(usage, dict):
        raise GatewayBlocked("No token usage receipt")
    if "input_tokens" in usage and "output_tokens" in usage:
        it, ot = usage["input_tokens"], usage["output_tokens"]
    elif "prompt_tokens" in usage and "completion_tokens" in usage:
        it, ot = usage["prompt_tokens"], usage["completion_tokens"]
    else:
        raise GatewayBlocked("Unsupported token usage schema")
    if type(it) is not int or type(ot) is not int or it < 0 or ot < 0 or ot > max_output_tokens:
        raise GatewayBlocked("Invalid or excessive token usage")
    if isinstance(data.get("content"), list):
        blocks = data["content"]
        if len(blocks) != 1 or not isinstance(blocks[0], dict) or blocks[0].get("type") != "text":
            raise GatewayBlocked("Unexpected tool or multimodal content")
        value = blocks[0].get("text")
    elif isinstance(data.get("choices"), list) and len(data["choices"]) == 1:
        choice = data["choices"][0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise GatewayBlocked("Invalid completion envelope")
        message = choice["message"]
        if message.get("tool_calls") or message.get("function_call"):
            raise GatewayBlocked("Tool execution is not enabled")
        value = message.get("content")
    else:
        raise GatewayBlocked("Unsupported response envelope")
    if not isinstance(value, str) or not value or len(value) > MAX_REPLY_CHARS:
        raise GatewayBlocked("Empty or excessive text reply")
    return {"text_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            "input_tokens": it, "output_tokens": ot, "text_length": len(value)}


def offline_probe() -> dict[str, Any]:
    """Validate local message-shape parsing with a synthetic, non-billable reply."""
    fake = {"content": [{"type": "text", "text": "OFFLINE_BCC_TEST_OK"}],
            "usage": {"input_tokens": 12, "output_tokens": 6}}
    return {"mock_contract": "PASS", "mock_result": normalize_reply(fake),
            "real_provider_compatibility": "UNVERIFIED", "paid_calls": "DISABLED"}


def key_status(check: Callable[[], bool] | None = None) -> str:
    """Returns presence only. Never read, log, or transmit the key value."""
    if check is None:
        if os.name != "nt":
            return "WINDOWS_ONLY"
        from tools.bcc_kie_credentials import WindowsCredentialStore
        check = WindowsCredentialStore().is_present
    return "PRESENT_VALUE_HIDDEN" if check() else "ABSENT"


def credential_readback(reader: Callable[[], str] | None = None) -> str:
    """Locally check vault readback; discard secret, disclose no value/hash/length."""
    if reader is None:
        if os.name != "nt":
            return "WINDOWS_ONLY"
        from tools.bcc_kie_credentials import WindowsCredentialStore
        reader = WindowsCredentialStore().load_for_agent
    try:
        value = reader()
        if (not isinstance(value, str) or not value or value != value.strip()
                or any(ch in value for ch in ("\x00", "\r", "\n"))):
            raise GatewayBlocked("Invalid stored credential")
    except Exception:
        return "UNREADABLE_OR_INVALID"
    finally:
        # Python does not guarantee zeroization of immutable str objects.
        value = None
    return "READABLE_VALUE_HIDDEN"


def default_ledger_path() -> Path:
    """Keep the budget DB away from GitHub checkout and live bot databases."""
    local = os.getenv("LOCALAPPDATA")
    if os.name != "nt" or not local or not Path(local).is_absolute():
        raise GatewayBlocked("Only a dedicated Windows user local-data path is allowed")
    return Path(local) / "BCCDevAgent" / "claude_budget.sqlite3"


def paid_request(*_args: Any, **_kwargs: Any) -> None:
    """Fail closed even if code/prompt/environment attempts to enable payment."""
    raise GatewayBlocked("Kie.ai endpoint and tool calling are unverified; no paid calls")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="BCC offline Claude gateway preflight")
    ap.add_argument("command", choices=("doctor", "mock", "quote"))
    ap.add_argument("--task-file", type=Path, help="Local text only; quote command")
    ns = ap.parse_args(argv)
    if ns.command == "mock":
        print(json.dumps(offline_probe(), sort_keys=True))
        return 0
    if ns.command == "doctor":
        print(json.dumps({"scope": "BCC_DEVELOPMENT_ONLY", "model": MODEL_LABEL,
                          "credential": key_status(), "local_readback": credential_readback(),
                          "network_enabled": NETWORK_GATE_ENABLED,
                          "api_contract": "UNVERIFIED", "work_graphify_bridge": "NOT_CONNECTED",
                          "monthly_budget_usd": 20, "requires_per_request_approval": True},
                         sort_keys=True))
        return 0
    if ns.task_file is None or not ns.task_file.is_file() or ns.task_file.stat().st_size > 16384:
        print("A small, local --task-file is required (no key content).", file=sys.stderr)
        return 2
    try:
        request = prepare(ns.task_file.read_text(encoding="utf-8"))
    except (GatewayBlocked, UnicodeError, ValueError) as exc:
        print(f"Blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(request.public_summary(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
