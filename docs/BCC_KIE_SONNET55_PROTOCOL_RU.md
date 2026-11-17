# BCC Claude Sonnet 5.5 — зафиксированный Kie.ai API-контракт

Источник: предоставленный пользователем снимок
https://kie.ai/claude-sonnet-5-5 (17 ноября 2026 г.).
Прочитана именно вкладка API; реальный API не вызывался.

## Подтверждено скриншотом

- API host: https://api.kie.ai
- HTTP POST /claude/v1/messages
- Model in request: claude-sonnet-5-5
- Authorization: Bearer <API key>
- Content-Type: application/json
- Request fields include model, messages; reference example contains tools.
- Optional stream=true with SSE (message_start, content_block_start,
  content_block_delta, message_delta, message_stop).
- Tool calling with input_schema and tool_use blocks.
- Response fields include role, model, content, stop_reason, usage.
- Example usage includes input_tokens, output_tokens, cache_read_input_tokens,
  cache_creation_input_tokens, and nested cache_creation.ephemeral_1h_input_tokens
  / ephemeral_5m_input_tokens.
- Page notes x-api-key should also be sent in Bearer form for this service;
  our client will use Authorization and will not double-send credentials.

## Implementation in the BCC-only branch

tools/bcc_kie_protocol.py contains the request builder, in-memory Bearer
header generator, strict assistant response parser, token/cache accounting
and a synthetic protocol check. Paid transmission is HARD-DISABLED.
No network client is imported or invoked.

The initial text-only payload explicitly excludes tool declarations,
streaming and prompt caching. Tool calls and stop_reason=tool_use are
rejected. Tool execution needs a separately designed sandbox and command
allowlist, and may never touch a live Windows BCC installation.

From the user's Kie.ai pricing screenshots (not live verified):
Sonnet 5.5: input $0.80/M, output $4/M tokens;
5-minute cache write x1.25, 1-hour cache write x2, cache read x0.10.
The parser estimates charge from the usage categories but does not
equate that estimate with an authoritative provider invoice.

Default admission ceiling: $18/month within a $20 total user budget.
BudgetLedger is a separate offline prototype; it is NOT yet a verified,
provider-enforced cap, so no real request may be issued.

## Offline one-click check

From the existing independent folder on Windows:

    $dir = "$HOME\Desktop\BCC_AGENT_GATEWAY"
    if ((git -C $dir branch --show-current).Trim() -ne "agent/bcc-work-claude-gateway-20261117") { throw "Wrong branch" }
    if (git -C $dir status --porcelain) { throw "Local changes; stop" }
    git -C $dir pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "Git error" }
    & "$dir\BCC_KIE_AGENT_CHECK.cmd"

This runs the local vault status/readback, old synthetic agent mock and
Kie.ai-specific protocol mock. It must show OFFLINE_PASS and DISABLED.
Do not send or paste the key into chat, PowerShell, GitHub or logs.

## Unverified gates blocking activation

1. Successful authenticated Kie.ai response with the user's key.
2. Real payload and response compatibility, rate limits, retry policy.
3. Provider invoice/cost accounting and any server-side spending cap.
4. Trusted, explicit user authorization for each paid model request.
5. Binding to the BCC agent branch and then GPT Work/Graphify via GitHub.
6. Windows safety, adapter regression and complete BCC acceptance.

Never promote this protocol mock to completed integration or release.
No trading bot processes, databases, monitors, API credentials or
unrelated project branches are to be modified.


## 2026-11-17: guarded transport and approval transaction

In the same isolated BCC development branch, the following components were
added together as one independent vertical slice:

- tools/bcc_kie_https.py: fixed-host HTTPS request code, one POST, 20s
  timeout, bounded request/response bytes, strict payload equality, rejects
  tools and streaming, no redirects or automatic retry, and sanitizes
  transport errors (no API key in exception text). LIVE_TRANSPORT_ENABLED=False.
  No public function can turn the transport on.
- tools/bcc_kie_approval.py: deterministic request fingerprint, deliberately
  conservative local preview, trusted approval verifier requirement,
  transactional reserve via tools/bcc_claude_budget.py, receipt validation,
  retention of unresolved holds on any mismatch, and a mock-only settlement
  path. LIVE_EXECUTION_ENABLED=False and run_paid_call always rejects.
- tests/test_bcc_kie_protocol.py, tests/test_bcc_kie_https.py, and
  tests/test_bcc_kie_approval.py: 32 isolated Python tests, 0 failures
  executed offline on Linux without a single real provider call. The HTTPS
  and approval implementation/test files are Git-blob-identical to the
  locally exercised files. Native Windows execution is not yet confirmed.

Run BCC_KIE_AGENT_CHECK.cmd after safe fast-forward update in the
separate BCC_AGENT_GATEWAY folder. It runs both key readback/protocol
checks and the offline Kie unit group. It does NOT run BCC trading bots.

IMPORTANT: mocked acceptance/verifier is NOT user authorization for a real
call; mocked ledger settlement is NOT proof of actual Kie billing. Production
activation requires a trusted Windows approval UI, a verified provider
invoice/spending limit, exact token ceilings and separate explicit consent
for the particular request. Keep the two live gates OFF until then.
