"""Disabled-by-default fixed-origin Kie.ai Responses HTTPS transport.

This file contains a testable one-POST implementation for screenshot-based
Terra/Kimi routes. No public paid API route is exposed. No code in this
module is authorized to use Windows credentials or the spending ledger.
The private transport function is gated OFF; tests use a fake connection.
"""
from __future__ import annotations

import http.client
import json
from typing import Any, Callable

from tools.bcc_kie_responses_protocol import (
    API_HOST, MAX_RESPONSE_UTF8, Reply, Request, ResponsesBlocked,
    build_request, endpoint, parse_response,
)

LIVE_TRANSPORT_ENABLED = False
TIMEOUT_SECONDS = 20
MAX_HTTP_BODY = 32768


def _headers(secret: str) -> dict[str, str]:
    if (type(secret) is not str or not secret or secret != secret.strip()
            or any(ch in secret for ch in ("\x00", "\r", "\n"))):
        raise ResponsesBlocked("Invalid local provider credential")
    return {
        "Authorization": "Bearer " + secret,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _validate_request(request: Request) -> tuple[str, int]:
    """Rebuild full body to exclude injected tools, redirects, data and URLs."""
    if not isinstance(request, Request):
        raise ResponsesBlocked("Unknown HTTPS request type")
    ep = endpoint(request.model)
    if (request.host != API_HOST or request.host != ep.host
            or request.path != ep.path or request.network_enabled
            or request.agent_execution_enabled):
        raise ResponsesBlocked("Unexpected provider route or requested side effects")
    if type(request.body) is not bytes or len(request.body) > MAX_HTTP_BODY:
        raise ResponsesBlocked("Invalid request body")
    try:
        body = json.loads(request.body.decode("utf-8"))
        if type(body) is not dict or set(body) != {
                "model", "stream", "max_output_tokens", "input"}:
            raise ResponsesBlocked("Unsupported API request options")
        incoming = body["input"]
        if (type(incoming) is not list or len(incoming) != 1
                or type(incoming[0]) is not dict
                or incoming[0].get("role") != "user"
                or type(incoming[0].get("content")) is not list
                or len(incoming[0]["content"]) != 1
                or type(incoming[0]["content"][0]) is not dict):
            raise ResponsesBlocked("Unexpected message contents")
        task = incoming[0]["content"][0].get("text")
        expected = build_request(task, model=request.model,
                                 max_output_tokens=body["max_output_tokens"])
        if request.body != expected.body or request.input_sha256 != expected.input_sha256:
            raise ResponsesBlocked("Request body differs from validated canonical payload")
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise ResponsesBlocked("Malformed or unsupported HTTPS request") from None
    return task, body["max_output_tokens"]


def _post_one_mockable(
        key: str, request: Request, *,
        connection_factory: Callable[..., Any] = http.client.HTTPSConnection,
        input_token_limit: int = 100_000) -> Reply:
    """INTERNAL test seam. Never runs while LIVE_TRANSPORT_ENABLED is False.

    Exactly one POST; no redirects, streaming, tool responses or retries.
    Provider error bodies / exceptions must not be logged or echoed.
    """
    if not LIVE_TRANSPORT_ENABLED:
        raise ResponsesBlocked("Kie.ai paid transport is not enabled")
    _, output_cap = _validate_request(request)
    headers = _headers(key)
    connection = None
    try:
        connection = connection_factory(API_HOST, timeout=TIMEOUT_SECONDS)
        connection.request("POST", request.path, body=request.body, headers=headers)
        response = connection.getresponse()
        if type(response.status) is not int or response.status != 200:
            raise ResponsesBlocked("Provider did not return HTTP 200; no retry")
        raw = response.read(MAX_RESPONSE_UTF8 + 1)
        if type(raw) is not bytes or len(raw) > MAX_RESPONSE_UTF8:
            raise ResponsesBlocked("Provider response too large")
        parsed = json.loads(raw.decode("utf-8"))
        return parse_response(parsed, model=request.model,
                              output_cap=output_cap, input_cap=input_token_limit)
    except ResponsesBlocked:
        raise
    except Exception:
        raise ResponsesBlocked("Provider transport failed; do not retry") from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def send_paid_request(*_args: Any, **_kwargs: Any) -> None:
    """Public entrypoint is permanently closed until a new reviewed release."""
    raise ResponsesBlocked("Kie.ai calls require new approval and provider billing gate")
