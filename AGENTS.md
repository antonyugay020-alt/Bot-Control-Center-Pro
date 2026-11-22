# BCC cloud controller override — 2026-11-22

This branch is exclusively for an offline-first GitHub Codespaces control-plane pilot. GPT Work and local LLMs (Ollama/Qwen) are prohibited. The target for initial review is the existing Bybit market-data recovery PR #12, inspected in a separate detached checkout; do not merge this diverged gateway branch with PR #12. Use tools/bcc_cloud_control.py and docs/BCC_CLOUD_CONTROL.md. No paid OpenAI/Kie.ai calls, background agents, GitHub Actions, live-trading operations, local PC access, merges or releases. All provider transport and autonomous GitHub writes remain disabled until separate implementation, independent review and explicit approval. The user-approved Kie.ai monthly limit is not API spending authorization. Historical guidance below referring to ChatGPT Work, Claude or PR #11 does not authorize those tools or supersede this pilot boundary.

---

# Instructions for long-running Codex / ChatGPT Work development

## Objective
Produce an actually usable, integrated Windows application, not a stream of commits or progress notes. Keep working through multiple coherent implementation, integration, regression and root-cause-fix cycles in the *same available work session*. One modified file, one test or one commit is not a completion criterion.

## Mandatory session workflow
1. Check live branch HEAD, draft PR and `docs/DEVELOPMENT_STATE.md` (if present), `docs/CONTROLLED_INTEGRATION.md`, actual tests and known blockers.
2. Preserve existing user work and prior architectural decisions. Work on this branch; never modify `main`, silently create a new competing PR, merge, or release.
3. Prioritize an end-to-end vertical slice across implementation, persistence, lifecycle, UI and failure paths. Detect common root causes rather than patching each repeated symptom.
4. Write/modify real code and meaningful regression tests. Run full available local regression, then integration, restart, concurrency and failure recovery scenarios. Fix and rerun. Clearly distinguish isolated tests from full checkout regression and actual Windows E2E.
5. Keep a short durable source-SHA-linked checkpoint with commands/results, integrated capabilities, unresolved blockers and the next exact step. On resumed sessions, use it rather than restarting analysis.
6. Use remaining session capacity for the next linked development step. Do not stop after creating a note, PR comment or a single commit.
7. Do not spend on GitHub Actions, API providers or other services without explicit authorization. Do not commit credentials. If a particular test runner/provider is unavailable, keep making independently verifiable progress and mark unrun tests honestly.
8. No interim builds, fake successful acceptance or routine reports. Final release requires comprehensive Windows clean-unpack GUI E2E and project-specific acceptance gates.

## Security and independence
The software's hard safety boundaries are not configurable by AI. Maintain independent projects and avoid changes to unrelated repositories. Disable potentially dangerous production actions until separately verified and explicitly authorized.

## Current development target: Bot Control Center Pro
Complete the Modern Qt Windows application with fully integrated common Market Data Hub/worker lifecycle, VELES multi-coin RSI bands, composable entry and exit, GRID, Pump & Dump, Polymarket PAPER, per-bot accounting and cross-bot risk, MY_BOTS inventory, trading history, exports/imports and recovery. Scale by actual machine resources rather than a fixed numeric bot ceiling.
Start with current consolidated draft PR #11 and its checkpoint; stabilize architectural boundaries and verify actual integration and clean-unpack Windows Qt E2E. Prioritize workflows creating/editing/starting/stopping bots, market data, trades, persistence, restart, and global monitoring. Randomized nonduplicative regression tasks and systemic defects collection. No release until mandatory end-to-end acceptance; no real trading unless separately authorized.

## Optional Kie.ai model gateway — one paid Terra attempt consumed; automated agent disabled
This branch includes tools/bcc_kie_models.py, tools/bcc_kie_task_router.py,
and tools/bcc_kie_model_budget.py. They provide only a no-network
model catalog, source-SHA-bound task handoffs, synthetic response checks
and a shared admission ledger. No real Kie.ai paid transport is enabled.

- Terra is the documented coding/implementation candidate. Kimi's
  2026-11-18 Kie.ai API page warns CHAT ONLY / not adapted for agents:
  Kimi may be used for non-executing human-reviewed textual critique,
  never for automated programming, tool calls or GitHub writes. The
  independent reviewer must see the pinned NEW implementation commit.
  No automatic fallback, provider calls or GitHub writes by a model.
- Astra is only a user-selected exceptional architecture candidate, and
  is NOT price-qualified: provider minimum rates starting 'from' are not
  an invoice maximum.
- Kie.ai Terra and Kimi routes are screenshot-documented, not live
  verified: api.kie.ai/codex/v1/responses (Terra gpt-6-1-terra) versus
  api.kie.ai/openai/v1/responses (Kimi kimi-k3). Responses may include
  harmless reasoning blocks; tool/web-search requests remain disabled.
  tools/bcc_kie_responses_protocol.py and bcc_kie_responses_https.py
  never authorize a paid request. Kimi chat-only warning is mandatory.
- Kie.ai Terra historical email rates conflict with a later Pricing
  screenshot. Every quote is unverified until actual model contract,
  pricing, endpoint, usage receipt and billing are confirmed.
- The old Claude authorization was consumed on an HTTP 502 request;
  support reports Claude unavailable. Never reuse approval or delete
  or reset its unresolved local spending hold.
- Never share credentials, raw trading logs, or entire private code trees.
  Reviewer context is explicit paths and immutable source commit; actual
  transmission of any code requires independent scope/consent review.
- The $20 monthly limit, $18 admission threshold and $2 reserve are not
  permission to spend. Each paid request requires a NEW trusted user
  approval bound to exact task digest, source SHA, model, and amount.
  All model reservations share the existing Claude spending database.
- No GitHub Actions spending, no unattended model requests, no live bot
  changes, no stop/reboot and no intermediate releases.
  The only Terra one-shot was approved and attempted on Windows 2026-11-18.
  The Kie.ai Logs screenshot shows GPT-6.1 Terra SUCCESS at 14:27:03,
  duration 9 and 0.01 credits used, whereas the BCC local client returned
  BLOCKED/UNKNOWN. A later Kie.ai result preview confirms exact model
  output BCC_TERRA_CONNECTED. This proves provider-generated correct text,
  NOT successful local response parsing or autonomous agent readiness.
  Treat the precise BLOCKED/UNKNOWN source as unproven until full sanitized
  response JSON or stage evidence is available. Keep text validation and
  provider token/billing validation independent; an unknown usage schema
  must not erase correct text, settle a hold, or grant an extra request.
  The one-shot authorization is CONSUMED; BCC_KIE_TERRA_ONE_TEST.cmd
  is retired and read-only.
  Do not rerun, reset reservations/markers or assume the credits amount is
  a verified USD bill. Preserve ledger and use read-only diagnostics.
  Model agent/tool calling and all other paid routes remain disabled.
- The Windows 2026-11-18 QA at 3c8aa241 ran 186 cases and had 10
  WinError 32 fixture cleanup errors (2 skips). Cause: plain
  'with sqlite3.connect(...)' commits but does not close SQLite handles.
  All gateway connections MUST use tools/bcc_kie_sqlite_lifecycle.py
  managed_connection() or contextlib.closing; never add transaction-only
  SQLite context managers. Regression gate includes an AST safety test.
  That source SHA passed 192/192 Windows offline tests (2 skips).
  The Terra one-shot source SHA 56d1a87c passed 218/218 Windows offline
  tests (2 skips), but the subsequent local client outcome was UNKNOWN.
  Since then read-only incident tools were added; no new Windows PASS or
  full Modern Qt E2E is claimed for that updated source.
- Provider Kie.ai later showed the exact requested Terra result text
  BCC_TERRA_CONNECTED. The historical local BLOCKED/UNKNOWN root cause
  remains unproven without the full response envelope. Do not invent it.
  Use bcc_kie_terra_response_replay.py only on an ALREADY-SAVED response
  from stdin; it cannot read credentials, invoice, ledger, bots or network.
  Validate text independently of optional usage; never confuse a provider
  response with a verified paid invoice. No unexpected tools/model aliases.
  The retired Terra one-shot transport now categorizes CONNECT/POST/STATUS/
  READ/JSON/ENVELOPE for future distinct authorizations, but does not
  retroactively classify the previous request. Never reset its spent flag.
  Offline acceptance inventory now requires 18 modules and 257 tests;
  the new code has NOT yet passed native Windows regression.
- Run BCC_KIE_AGENT_CHECK.cmd from an isolated Windows agent checkout
  for offline regression. Do not claim a Windows PASS until actual output
  from the revised SHA proves it. No Windows Qt E2E acceptance is implied.
- These instructions apply automatically only in checkouts including
  this AGENTS.md revision; other BCC branches do not inherit them.
