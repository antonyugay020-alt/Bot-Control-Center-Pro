"""Kie.ai Claude Sonnet 5.5 protocol adapter — OFFLINE ONLY, no HTTP client.

Contract source: user-provided screenshot of kie.ai/claude-sonnet-5-5,
2026-11-17. This module cannot transmit a request or authorize spending.
The real provider and its billing must still be verified separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from typing import Any

API_HOST = "api.kie.ai"
API_PATH = "/claude/v1/messages"
MODEL_ID = "claude-sonnet-5-5"
NETWORK_ENABLED = False
INPUT_USD_PER_MILLION = Decimal("0.80")
OUTPUT_USD_PER_MILLION = Decimal("4.00")
CACHE_5M_WRITE_FACTOR = Decimal("1.25")
CACHE_1H_WRITE_FACTOR = Decimal("2.00")
CACHE_READ_FACTOR = Decimal("0.10")
MAX_TEXT_LENGTH = 8192
MAX_RESPONSE_BYTES = 128 * 1024


class ProtocolBlocked(RuntimeError):
    """Unsupported content or attempted paid operation; never includes secrets."""


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_5m_tokens: int
    cache_1h_tokens: int
    cache_read_tokens: int
    estimated_micro_usd: int


@dataclass(frozen=True)
class ParsedResponse:
    content_sha256: str
    content_chars: int
    usage: Usage


def _count(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ProtocolBlocked("Invalid token count in " + field)
    return value


def build_payload(task: str, *, max_tokens: int = 4000) -> dict[str, Any]:
    """Fixed-model request without tools, streaming, caching or external URLs."""
    if not isinstance(task, str) or not task.strip() or "\x00" in task:
        raise ProtocolBlocked("Task is empty or invalid")
    if len(task) > MAX_TEXT_LENGTH:
        raise ProtocolBlocked("Task is too long")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 4000:
        raise ProtocolBlocked("Output token limit is invalid")
    return {"model": MODEL_ID,
            "max_tokens": max_tokens,
            "stream": False,
            "messages": [{"role": "user", "content": task}]}


def content_digest(payload: dict[str, Any]) -> str:
    """Stable local fingerprint; caller must not log the raw prompt."""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def auth_headers(key: str) -> dict[str, str]:
    """Build headers in memory; do not log or serialize this dictionary."""
    if not isinstance(key, str) or not key or key.strip() != key:
        raise ProtocolBlocked("Missing or invalid credential")
    if any(c in key for c in ("\r", "\n", "\x00")):
        raise ProtocolBlocked("Invalid credential")
    return {"Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json"}


def _cache_usage(raw: dict[str, Any]) -> tuple[int, int, int]:
    nested = raw.get("cache_creation", {})
    if nested is None:
        nested = {}
    if not isinstance(nested, dict):
        raise ProtocolBlocked("Invalid cache creation usage")
    five = _count(nested.get("ephemeral_5m_input_tokens", 0), "cache 5m")
    hour = _count(nested.get("ephemeral_1h_input_tokens", 0), "cache 1h")
    created = _count(raw.get("cache_creation_input_tokens", five + hour), "cache created")
    if created != five + hour:
        # An unknown cache write type must never be silently billed at a
        # lower rate. Block the response pending manual reconciliation.
        raise ProtocolBlocked("Unrecognized cache creation charges")
    read = _count(raw.get("cache_read_input_tokens", 0), "cache read")
    return five, hour, read


def parse_response(raw: Any, *, max_output_tokens: int = 4000) -> ParsedResponse:
    """Validate a non-streaming Claude Messages response, no tool execution.

    Usage-derived amounts are estimates, not provider-confirmed invoice charges.
    Any mismatch must preserve the local ledger's spending hold.
    """
    if not isinstance(raw, dict):
        raise ProtocolBlocked("Response must be an object")
    if raw.get("model") != MODEL_ID:
        raise ProtocolBlocked("Wrong response model")
    if raw.get("role") != "assistant":
        raise ProtocolBlocked("Wrong response role")
    if raw.get("stop_reason") != "end_turn":
        raise ProtocolBlocked("Incomplete response or unsupported tool use")
    body = raw.get("content")
    if not isinstance(body, list) or not 1 <= len(body) <= 16:
        raise ProtocolBlocked("Missing assistant text blocks")
    fragments = []
    for block in body:
        if not isinstance(block, dict) or block.get("type") != "text":
            raise ProtocolBlocked("Only text blocks are enabled")
        text = block.get("text")
        if not isinstance(text, str):
            raise ProtocolBlocked("Invalid text block")
        fragments.append(text)
    message = "".join(fragments)
    if not message or len(message.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ProtocolBlocked("Assistant message is empty or oversized")
    raw_usage = raw.get("usage")
    if not isinstance(raw_usage, dict):
        raise ProtocolBlocked("Missing usage receipt")
    it = _count(raw_usage.get("input_tokens"), "input")
    ot = _count(raw_usage.get("output_tokens"), "output")
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 4000 or ot > max_output_tokens:
        raise ProtocolBlocked("Output exceeds authorized token cap")
    c5, c1, cr = _cache_usage(raw_usage)
    amount = (Decimal(it) * INPUT_USD_PER_MILLION
              + Decimal(ot) * OUTPUT_USD_PER_MILLION
              + Decimal(c5) * INPUT_USD_PER_MILLION * CACHE_5M_WRITE_FACTOR
              + Decimal(c1) * INPUT_USD_PER_MILLION * CACHE_1H_WRITE_FACTOR
              + Decimal(cr) * INPUT_USD_PER_MILLION * CACHE_READ_FACTOR)
    return ParsedResponse(
        content_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        content_chars=len(message),
        usage=Usage(it, ot, c5, c1, cr,
                    int(amount.to_integral_value(rounding=ROUND_CEILING))),
    )


def send_paid_request(*_args: Any, **_kwargs: Any) -> None:
    """Intentional hard stop. No network path is included in this module."""
    raise ProtocolBlocked("Paid Kie.ai transport is disabled pending explicit user approval")


def mock_protocol_check() -> dict[str, Any]:
    """Exercise the observed screenshot fields without sending any data."""
    request = build_payload("Synthetic BCC protocol test")
    fake = {"role": "assistant", "model": MODEL_ID, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "OFFLINE_OK"}],
            "usage": {"input_tokens": 600, "output_tokens": 57,
                      "cache_creation": {"ephemeral_1h_input_tokens": 0,
                                         "ephemeral_5m_input_tokens": 0},
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0}}
    parsed = parse_response(fake)
    return {"status": "OFFLINE_PASS", "provider_contract": "SCREENSHOT_ONLY",
            "host": API_HOST, "route": API_PATH, "model": MODEL_ID,
            "request_sha256": content_digest(request),
            "usage_input_tokens": parsed.usage.input_tokens,
            "usage_output_tokens": parsed.usage.output_tokens,
            "estimated_micro_usd": parsed.usage.estimated_micro_usd,
            "network_enabled": NETWORK_ENABLED, "paid_calls": "DISABLED"}


if __name__ == "__main__":
    print(json.dumps(mock_protocol_check(), sort_keys=True))
