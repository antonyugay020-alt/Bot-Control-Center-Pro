# Controlled integration checkpoint — 2026-11-12

**DEVELOPMENT / NOT RELEASE.** Same branch `codex/architecture-consolidation-20261107`, draft PR #11. Resumed baseline: `343c3b3c619ca68f7fc120c6072b4953dc6e1de8`. Preserve the three resource-scaling commits after prior milestone `5516271`; no branch reset or competing PR. Result is the commit containing this checkpoint; reread remote HEAD on resume.

## Implemented / verified this cycle

- Coordinator's SQLite context previously committed/rolled back but retained open connections, including command and financial-authority fences. Explicit `finally: close()` now owns every handle deterministically. Tests retain strong references, check both commit/rollback, exercise STOP revocation of a real ledger transaction, and move/delete the closed registry. Production AST inventory rejects future bare sqlite3 connection context managers.
- The resource-adjustable Hub class still met a hard `--concurrency <= 12` in its actual service CLI. Removed that inconsistent cap, preserving positive integer validation, provider quotas and financial guards. Independent real service processes accept 13/29 chosen threads, release exact ownership on exit, and reject nonpositive settings without creating a cache.
- Native acceptance requires every exact identity: four real-adapter/independent-Hub/closed-PAPER cycles; four Qt account-recovery workflows; all sixteen cross-strategy loss/restore cases; three control-handle/ledger-fence cases; two resource-CLI cases. Omitted, failed, skipped and misidentified cases are each rejected. Real collection validates all mandatory identities.
- New resource replay filename now retains its isolated heavy shard/budget in the Windows runner and fixtures. No production fleet count ceiling restored. Varied dispatch workloads are separate pytest parametrizations: pytest 9.2 counted three unittest subtests in the JUnit total but emitted no corresponding testcase rows, making strict native inventory impossible. Exact collected/executed JUnit verification now covers those same workloads; the verifier was not relaxed.
- Concurrent import recovery now covers a two-bot mixed-strategy batch. Hub/adapter replay also traverses the real funding-history read (no published rates), alongside orders, marks, risk tiers and closed cycles.
- The source manifest inherited stale/deleted entries from the resource commits. Resealed complete current inventory, preserving those commits and excluding transient test data.

## Evidence / pending full clean checkout

Linux / Python 3.12.14 / SQLite 3.53.1 / PySide6 6.12.0 / pytest 9.2.0; virtualenv and raw evidence outside checkout at `/workspace/work/`.

`QT_QPA_PLATFORM=offscreen /workspace/work/venv/bin/python -m pytest -q tests/test_coordinator_connection_lifetime.py tests/test_hub_resource_service_boundary.py --timeout=180 --junitxml=/workspace/work/resumed-red.xml`: **6 failed / 2 passed** before fixes.

`resumed-target.xml`: **146 passed** (4.57s), including native negative evidence, real CLI, lifetime and partitioning tests. `resumed-integration.xml`: **47 passed / 3 subtests** before converting resource workloads; XML declared 50 but contained only 47 testcases, confirming native-evidence rejection. Expanded `resumed-integrated-final.xml`: **155 passed** (12.69s), no subtests, covering all core Hub/strategy cycles, mixed import, mandatory identities and exact JUnit accounting. These are targeted runs, not the final full suite.

The previous session's 3495-test pass and Qt/public-source checks predate this resumed source and are historical only. Full clean checkout verification is the next exact step: clone this committed branch locally into `/workspace/work/clean-acceptance`, verify the entire manifest, execute the complete pytest inventory and GUI check there, then persist exact results and source hashes.

Native Windows clean-unpack Modern Qt E2E, live market cycles and independent review remain **NOT CERTIFIED**. Only Linux is attached. No paid Actions, releases/builds, model-provider spending, credentials or real trading. Commits use `[skip ci]`. Prior detailed portfolio evidence remains in `docs/PORTFOLIO_REGRESSION_20261107.json` and `docs/REGRESSION_20261107.json`.

## Clean checkout / resource-contract follow-up

Source commit `0dc030006a08308707bb06979dc28e80426bdef4` was cloned locally with independent Git objects into `/workspace/work/clean-acceptance`. All **394 source files** verified against the sealed manifest. Complete inventory collected **3532 cases**. Full pytest (`clean-full.xml`) completed **3531 passed / 1 failed**, 227.97s. Failure was an older W2 negative test still rejecting concurrency 13 despite the preserved resource-scaling contract. Replaced that obsolete input with -1; explicit real class/service tests still require positive 13/29 to succeed. The acceptance checker was not weakened.

Clean-checkout GUI check **PASS**, Qt offscreen / native_windows=false, eight strategy editor/deposit workflows, four operator cycles and 20-row monitor. Read-only live provider probe: **Bybit HTTP 403 RESTRICTED**, **Binance HTTP 451 RESTRICTED**, Gamma/CLOB **PASS**. No source bypass or real orders. Native Windows verifier returned **2 / BLOCKED** on Linux.

Next exact step after this test-contract commit: fresh local clone, collect all tests, execute the production partitioned QA runner without headless exclusions, verify every case appears exactly once across all eight shards, including the renamed resource replay in its own heavy-budget shard. Persist those final results; Windows GUI/live-cycle/independent-review gates remain pending.
