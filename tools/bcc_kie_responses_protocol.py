"""Text-only Kie.ai Responses protocol, pinned to user-supplied API screenshots.

2026-11-18 screenshots:
  Terra: POST https://api.kie.ai/codex/v1/responses, model gpt-6-1-terra
  Kimi:  POST https://api.kie.ai/openai/v1/responses, model kimi-k3
Kimi's page explicitly states "Chat only ... not yet adapted for agent use".
This module has NO network, credentials, filesystem, executor or billing actions.
It does not claim a successful real provider response.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from typing import Any

API_HOST = "api.kie.ai"
NETWORK_ENABLED = False
PAID_CALLS_ENABLED = False
MAX_INPUT_UTF8 = 24_000
MAX_OUTPUT_TOKENS = 8192
MAX_RESPONSE_UTF8 = 131_072


class ResponsesBlocked(ValueError):
    """Safe diagnostic: never includes response body, prompt, or credentials."""


@dataclass(frozen=True)
class Endpoint:
    name: str
    host: str
    path: str
    request_model: str
    tools_documented: bool
    chat_only_warning: bool
    api_documented: bool = True
    live_compatibility_verified: bool = False
    agent_runtime_verified: bool = False


ENDPOINTS = {
    "terra": Endpoint("terra", API_HOST, "/codex/v1/responses",
                      "gpt-6-1-terra", tools_documented=True,
                      chat_only_warning=False),
    "kimi": Endpoint("kimi", API_HOST, "/openai/v1/responses",
                     "kimi-k3", tools_documented=True,
                     chat_only_warning=True),
}
# Astra may appear in the pricing catalog, but the user hasn't supplied
# an API contract for it. Never fall back to another endpoint or model.


@dataclass(frozen=True)
class Request:
    model: str
    host: str
    path: str
    body: bytes = field(repr=False)
    input_sha256: str
    agent_execution_enabled: bool = False
    network_enabled: bool = False
    actual_provider_price_verified: bool = False


@dataclass(frozen=True)
class Reply:
    model: str
    text: str = field(repr=False)
    text_sha256: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    provider_charge_verified: bool = False
    paid_calls_enabled: bool = False


def endpoint(model: str) -> Endpoint:
    if type(model) is not str or model not in ENDPOINTS:
        raise ResponsesBlocked("Model has no documented Responses endpoint")
    return ENDPOINTS[model]


def build_request(task: str, *, model: str, max_output_tokens: int = 256) -> Request:
    """Prepare exact JSON for a single, non-streaming user text input.

    Does not insert tools, reasoning mode, web search, store, background,
    previous_response_id, file/image URLs or any side-effecting capabilities.
    No auto-switch between the two documented provider paths.
    """
    target = endpoint(model)
    if type(task) is not str or not task.strip() or "\x00" in task or "\r" in task:
        raise ResponsesBlocked("Invalid text request")
    raw = task.encode("utf-8")
    if len(raw) > MAX_INPUT_UTF8:
        raise ResponsesBlocked("Input exceeds local maximum")
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
        raise ResponsesBlocked("Invalid output token cap")
    data = {
        "model": target.request_model,
        "stream": False,
        "max_output_tokens": max_output_tokens,
        "input": [
            {"role": "user", "content": [
                {"type": "input_text", "text": task}
            ]}
        ],
    }
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > 32768:
        raise ResponsesBlocked("Encoded request too large")
    return Request(
        model=model, host=target.host, path=target.path, body=body,
        input_sha256=sha256(raw).hexdigest()
    )


def _count(x: Any, field: str, max_count: int) -> int:
    if type(x) is not int or x < 0 or x > max_count:
        raise ResponsesBlocked("Invalid " + field + " usage")
    return x


def parse_response(raw: Any, *, model: str, output_cap: int,
                   input_cap: int = 100_000) -> Reply:
    """Check a nonstreaming, plain-text Responses-shaped reply.

    Reasoning items, shown on both provider docs, are allowed but NOT
    returned/logged. Function calls and unknown side effects are rejected.
    Usage estimates are not actual provider invoices or model authorization.
    """
    endpoint(model)
    if type(output_cap) is not int or not 1 <= output_cap <= MAX_OUTPUT_TOKENS:
        raise ResponsesBlocked("Invalid output cap")
    if type(input_cap) is not int or not 1 <= input_cap <= 100_000:
        raise ResponsesBlocked("Invalid input cap")
    if type(raw) is not dict or raw.get("status") != "completed":
        raise ResponsesBlocked("Provider response incomplete")
    if raw.get("object") not in (None, "response"):
        raise ResponsesBlocked("Unexpected response object")
    # The screenshot's Kimi sample reports moonshotai/kimi-k3 while its
    # unified request example uses kimi-k3. Do not accept cross-model output.
    known_reply_models = {
        "terra": frozenset(("gpt-6-1-terra", "gpt-6.1-terra")),
        "kimi": frozenset(("kimi-k3", "moonshotai/kimi-k3")),
    }
    reply_model = raw.get("model")
    if reply_model is not None and reply_model not in known_reply_models[model]:
        raise ResponsesBlocked("Unrecognized provider reply model")
    if raw.get("error") not in (None, {}):
        raise ResponsesBlocked("Provider returned an error")
    if raw.get("incomplete_details") is not None:
        raise ResponsesBlocked("Provider response incomplete")
    items = raw.get("output")
    if type(items) is not list or not 1 <= len(items) <= 32:
        raise ResponsesBlocked("Unexpected response output")
    texts = []
    messages = 0
    for item in items:
        if type(item) is not dict:
            raise ResponsesBlocked("Unexpected response item")
        if item.get("type") == "reasoning":
            if item.get("status") not in (None, "completed"):
                raise ResponsesBlocked("Incomplete reasoning output")
            # Allow a reasoning summary exactly as illustrated on the page.
            summary = item.get("summary", [])
            if type(summary) is not list or len(summary) > 16:
                raise ResponsesBlocked("Invalid reasoning summary")
            for part in summary:
                if (type(part) is not dict
                        or part.get("type") != "summary_text"
                        or type(part.get("text")) is not str):
                    raise ResponsesBlocked("Invalid reasoning summary")
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            raise ResponsesBlocked("Tool calls and other outputs are disabled")
        if item.get("status") not in (None, "completed"):
            raise ResponsesBlocked("Incomplete assistant message")
        messages += 1
        if messages > 1:
            raise ResponsesBlocked("Multiple assistant messages")
        blocks = item.get("content")
        if type(blocks) is not list or not 1 <= len(blocks) <= 16:
            raise ResponsesBlocked("Invalid assistant content")
        for block in blocks:
            if type(block) is not dict or block.get("type") != "output_text":
                raise ResponsesBlocked("Non-text content rejected")
            if type(block.get("text")) is not str:
                raise ResponsesBlocked("Invalid text output")
            if "annotations" in block and block["annotations"] != []:
                raise ResponsesBlocked("Unreviewed annotations rejected")
            texts.append(block["text"])
    text = "".join(texts)
    if messages != 1 or not text or len(text.encode("utf-8")) > MAX_RESPONSE_UTF8:
        raise ResponsesBlocked("No bounded assistant text")
    usage = raw.get("usage")
    if type(usage) is not dict:
        raise ResponsesBlocked("Missing usage evidence")
    incoming = _count(usage.get("input_tokens"), "input", input_cap)
    outgoing = _count(usage.get("output_tokens"), "output", output_cap)
    in_details = usage.get("input_tokens_details", {})
    if type(in_details) is not dict or set(in_details) - {"cached_tokens"}:
        raise ResponsesBlocked("Unknown input billing categories")
    cached = _count(in_details.get("cached_tokens", 0), "cache", incoming)
    out_details = usage.get("output_tokens_details", {})
    if type(out_details) is not dict or set(out_details) - {"reasoning_tokens"}:
        raise ResponsesBlocked("Unknown output billing categories")
    _count(out_details.get("reasoning_tokens", 0), "reasoning", outgoing)
    if set(usage) - {"input_tokens", "output_tokens",
                     "input_tokens_details", "output_tokens_details", "total_tokens"}:
        raise ResponsesBlocked("Unknown provider billing categories")
    if "total_tokens" in usage:
        if _count(usage["total_tokens"], "total", input_cap + output_cap) != incoming + outgoing:
            raise ResponsesBlocked("Inconsistent total tokens")
    return Reply(
        model=model, text=text,
        text_sha256=sha256(text.encode("utf-8")).hexdigest(),
        input_tokens=incoming, output_tokens=outgoing,
        cached_input_tokens=cached,
    )


def send_paid_request(*_args: Any, **_kwargs: Any) -> None:
    raise ResponsesBlocked("No paid Kie.ai call authorized; transport is offline")


def describe() -> dict[str, Any]:
    return {
        "status": "DOCUMENTED_OFFLINE_NOT_LIVE_VERIFIED",
        "api_host": API_HOST,
        "transport_enabled": False,
        "paid_calls": False,
        "endpoints": {
            name: {
                "path": ep.path, "request_model": ep.request_model,
                "api_documented": ep.api_documented,
                "live_compatibility_verified": ep.live_compatibility_verified,
                "tools_documented": ep.tools_documented,
                "chat_only_warning": ep.chat_only_warning,
                "agent_runtime_verified": ep.agent_runtime_verified,
            }
            for name, ep in ENDPOINTS.items()
        },
    }


if __name__ == "__main__":
    print(json.dumps(describe(), ensure_ascii=False, sort_keys=True))
