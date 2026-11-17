# BCC: optional Claude Sonnet via Kie.ai and GPT Work (development only)

Status: **OFFLINE POLICY PROTOTYPE; DISABLED FOR PAID CALLS; NOT RELEASE**.
This file has no authority to modify BCC trade execution, account credentials,
real bots, installations, or Windows processes.

## Locked scope

- BCC only, branch baseline 7bba4909235cc56e0777075422c705a948eb44c2.
- Two isolated branches: agent/bcc-work-claude-gateway-20261117 and
  agent/bcc-claude-20261117. No automatic merge or PR.
- GPT Work coordinates; Graphify supplies dependency context only when its
  availability is verified in the actual Work session. Claude is an optional
  independent contributor via a future isolated tool adapter.
- Never send API keys, live positions, trading logs, operator data, or account
  databases to a model. Treat model output as untrusted proposed code.

## Authorizations and billing

- Absolutely no paid API call without explicit user approval for the exact
  request. $20 USD shared calendar-month ceiling, $18 admission ceiling
  plus $2 safety reserve. No standing approval from a monthly budget.
- tools/bcc_claude_budget.py is a **network-free policy ledger only**:
  requires a separately trusted user-approval verifier to reserve funds;
  an unseen or timed-out request stays held across month boundaries;
  request IDs and approval IDs are one-use; SQLite BEGIN IMMEDIATE
  serializes competing reservations; billed settlement requires verified
  provider usage evidence.
- Test rates from user-shared Kie.ai screenshots dated 2026-11-17:
  Sonnet 5.5 input $0.80/1M and output $4.00/1M tokens. Do not enable
  execution until the exact model slug, endpoints, token accounting, fees,
  usage receipts, tool calling, streaming, and server-side spending limits
  are confirmed from provider documentation and an approved smoke test.
- This ledger is **not** a hard provider billing limit: price drift,
  accounting mismatches or third-party charges can exceed local estimates.
  Require a provider-level cap if available; fail closed on unknown prices.
- Do not store API credentials or approval tokens in GitHub, prompts, logs,
  trace artifacts, or source archives.

## Independent development workflow

1. Capture an immutable BCC baseline commit, current PR and blockers;
   never alter live BCC branches or a running operator checkout.
2. Work uses Graphify dependency queries only if actually accessible,
   and sanitizes relevant architectural excerpts.
3. Claude may propose patch sets only in its own checkout after tool
   compatibility, spend guard and permissions pass acceptance.
4. Review patches for exfiltration, weakened security boundaries, unsafe
   process changes and regressions. Claude cannot merge or bypass QA.
5. Integrate only after full local regression, native Windows Qt
   clean-unpack E2E and independent acceptance.
6. No GitHub Actions without explicit approval. Isolated commits use
   [skip ci]. Opening a PR may trigger Actions and is withheld.

## Verification and open gates

The isolated offline budget tests ran on Linux/Python:
**10 passed, 0 failed**, covering default denial, pricing, one-use
approvals/requests, concurrent budget exhaustion, cross-month holds and
settlement overcharge. This is not full BCC regression, provider compatibility,
Graphify/Work integration, or Windows Qt E2E.

Remaining gates: verify the callable Graphify route, Kie.ai endpoint and
Claude Sonnet model ID; authenticate one-time host approvals; bind sanitized
request hashes; enforce transport token ceilings and provider receipt
reconciliation; re-run security/budget/concurrency QA before even proposing
a paid smoke test. No live bot installation, trading or service control.
