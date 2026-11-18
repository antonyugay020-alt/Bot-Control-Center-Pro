"""Offline Kie.ai coding-model catalog for the BCC development gateway.

Source: Kie.ai email dated 2026-11-10. Prices are indicative and may change.
A model's presence here is NOT proof of endpoint availability or entitlement.
There are deliberately NO actual API model IDs, URLs, credentials or sockets.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from typing import Any

PRICE_SOURCE_DATE = "2026-11-10"
NETWORK_ENABLED = False
PAID_EXECUTION_ENABLED = False
MAX_PROMPT_BYTES = 24_000
MAX_OUTPUT_TOKENS = 8192
MAX_INPUT_CAP = 100_000
MICRO = Decimal(1_000_000)


class ModelNotReady(ValueError):
    """A required provider contract, price, approval or safe shape is missing."""


@dataclass(frozen=True)
class ModelProfile:
    name: str
    display_name: str
    input_usd_per_million: Decimal | None
    cached_input_usd_per_million: Decimal | None
    output_usd_per_million: Decimal | None
    cache_write_usd_per_million: Decimal | None
    # None means the provider API model ID was NOT verified, not an empty ID.
    api_model_id: str | None = None
    api_endpoint: str | None = None
    availability_verified: bool = False
    catalog_listed: bool = False
    advertised_input_floor_usd_per_million: Decimal | None = None
    advertised_output_floor_usd_per_million: Decimal | None = None


PROFILES = {
    "terra": ModelProfile(
        "terra", "GPT-6.1 Terra", Decimal("0.30"),
        Decimal("0.015"), Decimal("1.50"), Decimal("0.375"),
        catalog_listed=True),
    "kimi": ModelProfile(
        "kimi", "Kimi K3.1 Pro", Decimal("0.80"),
        Decimal("0.08"), Decimal("4.00"), None,
        catalog_listed=True),
    # Historical Kie.ai pricing screenshot shows "gpt-6-astra". The
    # listed minimum rates are NOT a verified per-request invoice tariff.
    # Presence in a catalog does not establish Astra Pro equivalence,
    # provider endpoint, account entitlement or model availability.
    "astra": ModelProfile("astra", "GPT-6 Astra (Kie.ai catalog)",
                          None, None, None, None,
                          catalog_listed=True,
                          advertised_input_floor_usd_per_million=Decimal("0.28"),
                          advertised_output_floor_usd_per_million=Decimal("56")),
}


def profile(name: str) -> ModelProfile:
    if not isinstance(name, str) or name not in PROFILES:
        raise ModelNotReady("Model is not listed in the verified price catalog")
    return PROFILES[name]


def _tokens(value: Any, label: str, maximum: int) -> int:
    if type(value) is not int or value < 0 or value > maximum:
        raise ModelNotReady(label + ": invalid token count")
    return value


def estimate_micro_usd(name: str, *, input_tokens: int,
                       output_tokens: int, cached_input_tokens: int = 0,
                       cache_write_tokens: int = 0) -> int:
    """Price estimate, not a provider-invoice guarantee. Cache read is a
    subset of input tokens; writes are accounted as a separate category.
    Reject unknown pricing rather than assuming a zero-cost category.
    """
    model = profile(name)
    if model.input_usd_per_million is None:
        raise ModelNotReady("No verified price for this candidate")
    incoming = _tokens(input_tokens, "input", 100_000_000)
    outgoing = _tokens(output_tokens, "output", 100_000_000)
    cached = _tokens(cached_input_tokens, "cache read", 100_000_000)
    writes = _tokens(cache_write_tokens, "cache write", 100_000_000)
    if cached > incoming:
        raise ModelNotReady("Cached input cannot exceed total input")
    if writes and model.cache_write_usd_per_million is None:
        raise ModelNotReady("Cache-write price is not verified")
    amount = (Decimal(incoming - cached) * model.input_usd_per_million
              + Decimal(cached) * model.cached_input_usd_per_million
              + Decimal(outgoing) * model.output_usd_per_million)
    if writes:
        amount += Decimal(writes) * model.cache_write_usd_per_million
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class DryRunPlan:
    model: str
    prompt_digest: str
    prompt_bytes: int
    maximum_input_tokens: int
    maximum_output_tokens: int
    estimated_max_micro_usd: int
    api_ready: bool
    network_enabled: bool
    paid_execution_enabled: bool


def plan(task: str, *, model: str,
         max_output_tokens: int = 4096) -> DryRunPlan:
    """Create a provider-independent plan. Contains NO raw prompt or API key.

    The input ceiling deliberately overestimates UTF-8 byte count and adds
    fixed protocol allowance; it is only an offline estimate, not a tokenizer
    guarantee or provider-enforced hard billing limit.
    """
    target = profile(model)
    if (type(task) is not str or not task.strip() or "\x00" in task
            or any(c == "\r" for c in task)):
        raise ModelNotReady("Invalid task")
    raw = task.encode("utf-8")
    if len(raw) > MAX_PROMPT_BYTES:
        raise ModelNotReady("Task exceeds offline byte ceiling")
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
        raise ModelNotReady("Invalid output cap")
    input_cap = len(raw) + 4096
    if input_cap > MAX_INPUT_CAP:
        raise ModelNotReady("Input cap exceeded")
    price = estimate_micro_usd(
        model, input_tokens=input_cap, output_tokens=max_output_tokens)
    return DryRunPlan(
        model=model, prompt_digest=hashlib.sha256(raw).hexdigest(),
        prompt_bytes=len(raw), maximum_input_tokens=input_cap,
        maximum_output_tokens=max_output_tokens,
        estimated_max_micro_usd=price, api_ready=bool(
            target.availability_verified and target.api_model_id and
            target.api_endpoint), network_enabled=False,
        paid_execution_enabled=False,
    )


def validate_mock_responses_reply(raw: Any, *, model: str,
                                  input_cap: int, output_cap: int) -> dict[str, Any]:
    """Strict *synthetic* OpenAI Responses-shaped fixture check.

    Kie.ai actual response format remains UNVERIFIED. Never use this helper
    as permission to send a request or as provider billing proof.
    """
    profile(model)
    _tokens(input_cap, "input cap", MAX_INPUT_CAP)
    _tokens(output_cap, "output cap", MAX_OUTPUT_TOKENS)
    if not isinstance(raw, dict) or raw.get("status") != "completed":
        raise ModelNotReady("Incomplete model response")
    outputs = raw.get("output")
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= 16:
        raise ModelNotReady("Invalid response items")
    chunks = []
    for item in outputs:
        if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "assistant":
            raise ModelNotReady("Tool call or unexpected output is not allowed")
        if item.get("status") not in (None, "completed"):
            raise ModelNotReady("Assistant message incomplete")
        content = item.get("content")
        if not isinstance(content, list) or not 1 <= len(content) <= 16:
            raise ModelNotReady("Invalid message content")
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                raise ModelNotReady("Unsupported output content")
            text = part.get("text")
            if not isinstance(text, str):
                raise ModelNotReady("Missing output text")
            chunks.append(text)
    whole = "".join(chunks)
    if not whole or len(whole.encode("utf-8")) > 131072:
        raise ModelNotReady("Empty or oversized text")
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        raise ModelNotReady("Usage missing")
    incoming = _tokens(usage.get("input_tokens"), "input", input_cap)
    outgoing = _tokens(usage.get("output_tokens"), "output", output_cap)
    details = usage.get("input_tokens_details", {})
    if not isinstance(details, dict) or set(details) - {"cached_tokens"}:
        raise ModelNotReady("Unrecognized usage detail may affect billing")
    if set(usage) - {"input_tokens", "output_tokens", "input_tokens_details",
                      "output_tokens_details", "total_tokens"}:
        raise ModelNotReady("Unrecognized usage category may affect billing")
    cached = _tokens(details.get("cached_tokens", 0), "cache read", input_cap)
    if cached > incoming:
        raise ModelNotReady("Cached input exceeds input")
    if "total_tokens" in usage and _tokens(usage["total_tokens"], "total", 200_000_000) != incoming + outgoing:
        raise ModelNotReady("Inconsistent total token count")
    reasoning = usage.get("output_tokens_details", {})
    if not isinstance(reasoning, dict) or set(reasoning) - {"reasoning_tokens"}:
        raise ModelNotReady("Unrecognized output billing details")
    if "reasoning_tokens" in reasoning and _tokens(reasoning["reasoning_tokens"], "reasoning", output_cap) > outgoing:
        raise ModelNotReady("Reasoning token count exceeds output")
    cost = estimate_micro_usd(
        model, input_tokens=incoming, output_tokens=outgoing,
        cached_input_tokens=cached)
    return {"status": "OFFLINE_MOCK_VALID", "model": model,
            "response_sha256": hashlib.sha256(whole.encode("utf-8")).hexdigest(),
            "input_tokens": incoming, "output_tokens": outgoing,
            "cached_input_tokens": cached, "estimated_micro_usd": cost,
            "provider_invoice_verified": False, "paid_calls": "DISABLED"}


def send_paid_request(*_args: Any, **_kwargs: Any) -> None:
    raise ModelNotReady("No verified Kie model IDs/endpoints and no paid authorization")


def summary() -> dict[str, Any]:
    return {
        "source": "Kie.ai newsletter 2026-11-10 and user-supplied Kie.ai pricing screenshot",
        "selection_mode": "EXPLICIT_ONLY",
        "source_price_floor_warning": "Catalog prices beginning with from are not authoritative maximum costs",
        "price_status": "HISTORICAL_QUOTE",
        "api_contract": "UNVERIFIED", "network_enabled": False,
        "paid_calls": "DISABLED",
        "models": {
            name: {
                "display": value.display_name,
                "api_model_id": value.api_model_id,
                "api_endpoint": value.api_endpoint,
                "availability_verified": value.availability_verified,
                "catalog_listed": value.catalog_listed,
                "advertised_input_floor_usd_per_million": str(value.advertised_input_floor_usd_per_million) if value.advertised_input_floor_usd_per_million is not None else None,
                "advertised_output_floor_usd_per_million": str(value.advertised_output_floor_usd_per_million) if value.advertised_output_floor_usd_per_million is not None else None,
                "input_per_million": str(value.input_usd_per_million) if value.input_usd_per_million is not None else None,
                "cached_input_per_million": str(value.cached_input_usd_per_million) if value.cached_input_usd_per_million is not None else None,
                "output_per_million": str(value.output_usd_per_million) if value.output_usd_per_million is not None else None,
            } for name, value in PROFILES.items()
        }
    }


if __name__ == "__main__":
    print(json.dumps(summary(), ensure_ascii=False, sort_keys=True))
