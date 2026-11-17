"""Disabled-by-default fixed-origin Kie HTTPS transport scaffold.

No public CLI and no live request unless a separately reviewed release
explicitly enables the hard code-level gate. Not tied to trading code.
"""
from __future__ import annotations

import http.client
import json
from typing import Any, Callable

from tools.bcc_kie_protocol import (
    API_HOST, API_PATH, MODEL_ID, ProtocolBlocked, auth_headers,
    build_payload, parse_response,
)

LIVE_TRANSPORT_ENABLED = False
TIMEOUT_SECONDS = 20
MAX_REQUEST_BYTES = 32_768
MAX_RESPONSE_BYTES = 131_072


def _post_fixed_origin(
    key: str,
    payload: dict[str, Any],
    *,
    connection_factory: Callable[..., Any] = http.client.HTTPSConnection,
) -> dict[str, Any]:
    """One HTTPS POST, no redirects/retries, no payload/header logging.

    IMPORTANT: this function is hard-disabled. Tests can monkeypatch its
    module-level gate with fake HTTPSConnection, never a real connection.
    """
    if not LIVE_TRANSPORT_ENABLED:
        raise ProtocolBlocked("Paid provider transport is disabled")
    if not isinstance(payload, dict) or payload.get("model") != MODEL_ID:
        raise ProtocolBlocked("Unrecognized provider payload")
    if payload.get("stream") is not False or "tools" in payload:
        raise ProtocolBlocked("Streaming and tools are disabled")
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], dict):
        raise ProtocolBlocked("Unrecognized message payload")
    # Prevent accepting an unvalidated caller-crafted prompt or model.
    expected = build_payload(messages[0].get("content"), max_tokens=payload.get("max_tokens"))
    if payload != expected:
        raise ProtocolBlocked("Unexpected extra provider options")
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ProtocolBlocked("Request exceeds local transport budget")
        headers = auth_headers(key)
        connection = connection_factory(API_HOST, timeout=TIMEOUT_SECONDS)
        try:
            connection.request("POST", API_PATH, body=body, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                raise ProtocolBlocked("Kie server did not return HTTP 200")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProtocolBlocked("Kie response exceeds configured size limit")
            decoded = json.loads(raw.decode("utf-8"))
            parse_response(decoded, max_output_tokens=payload["max_tokens"])
            return decoded
        finally:
            connection.close()
    except ProtocolBlocked:
        raise
    except Exception:
        # This exception never includes endpoint body, headers or the API key.
        raise ProtocolBlocked("Kie HTTPS transport failed; do not retry automatically") from None


def send_paid_request(*_args: Any, **_kwargs: Any) -> None:
    """No public network route: requires a future explicit approved release."""
    raise ProtocolBlocked("Kie API calls require a separate approved integration step")
