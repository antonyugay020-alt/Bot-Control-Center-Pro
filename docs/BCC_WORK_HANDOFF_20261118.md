# BCC / GPT Work handoff — Terra connectivity confirmed, coding agent not yet authorized

**Scope:** Bot Control Center Pro only. **NOT RELEASE / NO LIVE TRADING.**
**Updated:** 2026-11-18. This document is a Work handoff, not a claim that
Work or Graphify is currently running or that Claude is connected.

## Source boundaries (read from GitHub)

- Latest BCC application / public WS integration branch:
  `codex/ws-transport-isolated-20261113`, source HEAD
  `7bba4909235cc56e0777075422c705a948eb44c2`.
- Optional BCC agent/gateway development branch:
  `agent/bcc-work-claude-gateway-20261117`, descended from the above BCC
  SHA. Contains only agent scripts/tests/docs; never merge it blindly
  into production.
- Claude's independent branch: `agent/bcc-claude-20261117` at the same
  7bba490 starting SHA. Do not write to it until a real, authorized
  provider agent has been confirmed.
- Main, PR #11, working Windows bot installations, paper databases,
  monitor processes, trading accounts and other repositories are out
  of scope.

## Confirmed latest BCC application evidence

See `docs/BYBIT_OFFLINE_BOUNDARY_CHECKPOINT_20261117.md` on the BCC app
branch. Its Windows evidence had 3,892 cases, 3,886 PASS, one failure and
five allowed platform skips. That single replay boundary failure was
subsequently corrected in a unified test fixture and an explicit guard
against unintended public WS Hub startup. Linux sealed-copy retest then
had 3,893 PASS with zero failures/skips, plus 1,200/1,200 public Bybit
WS event checks and Linux offscreen Qt PASS. **These latter results do
NOT replace full Windows Qt acceptance on the corrected source.**

Next real BCC product milestone, as stated by the checkpoint:
- In a NEW isolated Windows source directory, verify the SHA/inventory
  and execute `CHECK_BYBIT_WINDOWS.bat` against the corrected immutable
  snapshot. Never reuse/overwrite live BCC checkout.
- Complete native tests / public WS / Modern Qt flows, loaded portfolio
  mark and Cross valuation continuity, held-position Stop/Restart and GUI
  reopen, owned transport loss/recovery, GRID and Pump & Dump PAPER,
  funding workflow, independent review.
- No Windows reboot, no stopping running bots/monitors; no real orders,
  GitHub Actions spending or intermediate release.
- If full native results fail, group root causes and correct them as an
  integrated block, then rerun the complete acceptance. Avoid
  isolated patches/reports after each micro-fix.

## Claude/Kie state (separate optional development dependency)

- One Kie.ai Sonnet 5.5 test was approved on 2026-11-17 with one-time
  local reservation USD 0.01. HTTPS 502, Kie log `api_error` and
  `Internal error, please try again later`; provider displayed 0 credits.
- The authorization is CONSUMED. Never use the same test again and
  never delete/replace the local ledger to force a retry. Support has
  been contacted; wait for their response before any new paid test.
- Key exists in the Windows Credential Manager; never copy it into
  prompts, GitHub, reports or cloud Work. No shared remote credential.
- `tools/bcc_kie_ledger_review.py` is a read-only local status of the
  exact one-time attempt. It does not settle/reconcile the provider bill.
- The historical one-shot launcher is READ-ONLY from this checkpoint,
  and the Python live entrypoint is explicitly retired.


## Developer gateway update: Kie.ai Terra, 18 November 2026

This section supersedes the historical Claude-only status below.

- Kie.ai dashboard Logs showed a successful GPT-6.1 Terra request
  (`gpt-6-1-terra`) at 14:27:03, **0.01 provider credits**, result preview
  exactly `BCC_TERRA_CONNECTED`. A provider credit is NOT necessarily
  equivalent to USD 0.01, and a token receipt is not a provider invoice.
- The local Python one-shot had `BLOCKED/UNKNOWN` exit 1 despite
  provider success. The complete response envelope was not captured.
  **The exact local exception is not established**. The Kie preview
  proves correct provider-generated text, but not clean client parsing.
- The user-confirmed Windows offline run of the old gateway at source
  `56d1a87c9171d7e257a9f19881d6a72b8944e9c7` had 218/218 tests,
  0 failures/errors, 2 skipped. Later gateway changes have not received
  the same native Windows acceptance.
- New offline-only stages separate safe text from token accounting,
  categorize CONNECT/POST/STATUS/READ/JSON errors without secrets, and
  allow replay of a PREVIOUSLY SAVED JSON body via stdin. The expanded
  acceptance inventory is **18 modules / 256 tests**.
- The one-time authorization is **CONSUMED**. The old Terra and Claude
  launchers are read-only and all other live Kie calls remain disabled.
  Keep the original SQLite holds/attempt markers. No retry, fallback,
  billing settlement, API key forwarding or Graphify/model claims.
- Terra is a candidate to program BCC when a separate, explicit
  paid request is authorized and the coding workflow is accepted.
  Kimi remains chat-only on Kie.ai as documented; do not run it as
  an autonomous tool-using agent. Astra has no confirmed billable cap.

**Work should continue the BCC PRODUCT independently of model-provider
readiness**, using normal GitHub source inspection and isolated test
branches. Do not wait for Terra to become an autonomous coding agent.
The app branch `codex/ws-transport-isolated-20261113` and the native
Windows Modern Qt E2E requirement remain authoritative. The model gateway
branch is NOT a product release.

## Work / Graphify task contract

If Work is available, use the BCC **application** SHA above as the
authoritative baseline for analysis and Windows acceptance; do not
mistake the agent-gateway branch for the product release.

Graphify can inform dependency and impact analysis only if callable in
the actual Work session; otherwise ordinary repository reads must be
used. No unsupported claim of direct integration. Proposed changes
must be tied to a concrete issue, expected system behavior and
regression evidence. Claude, if later authorized, must work on its own
branch with reviewed sanitized input and cannot execute shell commands
against the live Windows bot installation.

The operator must not be asked to redo identical Windows tests without
a source change that justifies the run, and reports must cite the exact
source SHA and corresponding QA archive/results. Do not initiate an
unbounded recurring status workflow.
