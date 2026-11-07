# Bot Control Center Pro — controlled integration checkpoint

Status: **DEVELOPMENT / NOT RELEASE**. This file records an integration milestone, not release acceptance.

## Source of truth

- Branch: `codex/architecture-consolidation-20261107`, Draft PR #11.
- Baseline verified before this milestone: `0c40f3a9089358e91be6166242be0368db916388` (writer-free ready-schema admission).
- Result: the current PR #11 HEAD after this checkpoint and manifest-sealing commits; **always reread HEAD** on next run.
- Protocol: `docs/CONTROLLED_INTEGRATION.md`. No alternate architecture branch, no user archive.

## Integrated milestone — authoritative SQLite schema contract (2026-11-07)

The read-only startup admission now verifies the actual ordered columns and
owning table of `hub_due` and `hub_expiry`, not only their names. It also
verifies the two identity primary keys and required NOT NULL constraints.
Under the existing atomic writer-locked migration, malformed same-table
indexes are rebuilt together while preserving subscriptions; a foreign-owned
index name or incompatible table identity/constraint fails closed and rolls
back rather than advertising a ready producer. This protects both fleet
query performance and transactional lease uniqueness.

Regression cases added in `tests/test_shared_sqlite_schema_migration.py`:
wrong-column queue/expiry indexes repaired without losing subscriptions;
a subscription table missing its primary key rejected with no partial DDL;
a foreign table's colliding index preserved with an explicit startup error.
Existing legacy migration, independent-process convergence and ready-schema
reader-under-writer tests remain in the same suite.

Verification: isolated Linux Python/SQLite transaction reproduction for
wrong-column index detection and rebuilding **PASS**; this was a standalone
SQLite reproduction, **not** repository pytest. The GitHub connector permits
source read/write but does not execute Python; the private repository remains
unavailable to the isolated checkout. Exact repo pytest command pending:
`python -m pytest -q tests/test_shared_sqlite_schema_migration.py
tests/test_hub_boot_epoch_recovery.py tests/test_hub_exact_generation_recovery.py
tests/test_hub_supervisor_lifecycle.py`. Full regression, native Windows
Modern Qt E2E and independent all-source digest audit remain **NOT RUN**.
No paid CI rerun, no user archive, no real orders or credentials.

Defect inventory: **H5** (index-name-only and column-name-only schema
admission) addressed in source with atomic repair/fail-closed boundary;
repository regression **pending**. H1–H4 remain unaccepted end-to-end.
Next milestone: execute these linked SQLite/Hub lifecycle regressions in an
executable checkout, then native Windows Qt multi-worker Stop/Restart and
transactional recovery. The source manifest must match the exact resulting
HEAD; successful commits are not release acceptance.

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
