# Bot Control Center Pro — controlled integration checkpoint

Status: **DEVELOPMENT / NOT RELEASE**. This file records an integration milestone, not release acceptance.

## Source of truth

- Branch: `codex/architecture-consolidation-20261107`, Draft PR #11.
- Baseline verified before this milestone: `18e06c58defc27f24cdbb0255d7c3b44f4b18031` (boot-aware Hub leadership).
- Result: the current PR #11 HEAD after this checkpoint and manifest-sealing commits; **always reread HEAD** on next run.
- Protocol: `docs/CONTROLLED_INTEGRATION.md`. No alternate architecture branch, no user archive.

## Integrated milestone — writer-free ready-schema admission

The shared Market Data Hub SQLite schema migration remains atomic under `BEGIN IMMEDIATE` for new, legacy or incomplete databases. A new read-only schema inventory checks both tables, all required columns, both queue/expiry indexes and the completion of subscription-token backfill. **Only fully migrated databases skip the writer lock.** This removes the serialized startup bottleneck when many independent PAPER workers initialize their HubRegistry while a producer holds a normal SQLite writer transaction. A partial migration (including a missing index or empty generation token) still takes the protected migration path. Provider-denial, leader-epoch and subscription-generation contracts are unchanged.

Regression coverage added to `tests/test_shared_sqlite_schema_migration.py`: initialize a second HubRegistry while another SQLite connection holds `BEGIN IMMEDIATE` in WAL mode; verify that incomplete index/token state triggers repair; retain independent-process legacy migration and transactional DDL/backfill checks. These tests make no HTTP requests, account changes or orders.

## Verification and defect inventory

- **H4 (new):** every HubRegistry initialization took `BEGIN IMMEDIATE` even on a complete schema; fleet startup could queue behind unrelated producer writes and exceed the one-second SQLite timeout. Fixed in source with a read-only fast path and guarded fallback; repository pytest execution **pending**.
- **H1–H3:** exact owner/readiness, subscription generations and boot-aware leader recovery remain integrated; whole-system execution **pending**.
- **Executed isolated reproduction:** Linux SQLite WAL: one connection holds `BEGIN IMMEDIATE`, a separate connection successfully reads `PRAGMA journal_mode`, `sqlite_master` and subscription-token metadata. This validates the SQLite mechanism, **not** the repository implementation or pytest suite.
- **Not executed:** `python -m pytest -q tests/test_shared_sqlite_schema_migration.py tests/test_hub_boot_epoch_recovery.py tests/test_hub_exact_generation_recovery.py tests/test_hub_supervisor_lifecycle.py`, full project regression, native Windows Qt E2E.
- **Runtime blocker:** isolated container cannot resolve github.com to clone the private repository; GitHub connector permits source read/write but not Python execution. GitHub Actions are not treated as code failures when jobs have no steps. No paid rerun.
- **Release gate:** all-source SHA-256 verification, independent review, complete regressions and clean-unpack Windows Modern Qt E2E remain **NOT RUN / NOT CERTIFIED**. No release.

## Next coherent milestone

Execute the Hub schema, boot-epoch, exact-generation and Windows supervisor regressions together in an executable checkout. Integrate any cross-component failures as one package. Then exercise multi-worker PAPER Stop/Restart, independent Hub recovery, dashboard monitoring and transactional financial recovery in native Windows Qt E2E. Revalidate the exact-source manifest and record results against a specific HEAD.
