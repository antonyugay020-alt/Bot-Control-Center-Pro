# Controlled integration checkpoint — 2026-11-12

**DEVELOPMENT / NOT RELEASE.** Branch `codex/architecture-consolidation-20261107`, draft PR #11. No merge, releases, paid Actions or real orders.

## Source / resume

Resumed baseline: `343c3b3c619ca68f7fc120c6072b4953dc6e1de8`. Preserved the user's resource-scaling commits after `5516271`. Final executable/test source verified: `613fb7cadb32b2f73bbda823e8ace40ba99eabc7`. The commit containing this final checkpoint updates only evidence documents and the source manifest; executable bytes match the verified source. Always reread remote HEAD before resuming.

## Completed integration

- Closed Coordinator SQLite handles deterministically after commit, rollback and control/ledger fences. Strong-reference regressions prove no reliance on GC and successful file move/delete for Windows cleanup. Source-wide AST check prevents repeating bare SQLite connection-context ownership mistakes.
- Removed the stale Hub service CLI cap of 12, preserving positive-integer validation, provider quotas and financial safety. Real child services accept operator-selected 13/29 thread budgets and relinquish leadership. These test values are not fleet targets or ceilings.
- Restored isolation/heavy budget for the renamed resource-scaled VELES replay. Dispatch workload variations now have independent pytest identities: pytest 9.2's unittest subtests had inflated JUnit totals without testcase rows. Exact collected/executed evidence remains strict.
- Native evidence requires all 29 exact mandatory identities: four real-adapter/independent-Hub/closed-PAPER strategy cycles, four Qt recovery workflows, sixteen cross-strategy account-loss/recovery combinations, three control-handle/fence cases and two resource-CLI cases. Negative tests reject omission, failure, skipping and wrong identity; collection checks prevent misspelled mandatory cases.
- Mixed-strategy transfer regression now proves two-account publication visibility. Hub replay also traverses funding-history admission. Replay drives bounded next-tick retries of recognized unavailable data across a minute rollover, reacquiring depth; persistent failures still fail. VELES explicitly tests an expired first candle. Production clock/cache/book/candle freshness was not relaxed.
- Fixed nested Windows watchdog budgets: eight sequential shard limits total 3120s, exceeding the old 2850s parent timeout. Shared constants now give the test stage 3210s (including report margin) and overall automated checks 3690s, without extending individual shard limits.
- Resealed the complete source inventory, including renamed tests and resource-policy changes that had left the inherited manifest stale. No release archive generated; clean local Git clones were used for isolation.

## Final verification — clean checkout

Linux / Python 3.12.14 / SQLite 3.53.1 / PySide6 6.12.0 / pytest 9.2.0. Dependencies and artifacts outside the source checkout at `/workspace/work/`.

`QT_QPA_PLATFORM=offscreen /workspace/work/venv/bin/python -m tools.collect_test_inventory --output /workspace/work/verified-inventory.json` followed by `python -m tools.run_partitioned_tests --inventory /workspace/work/verified-inventory.json --output /workspace/work/verified-shards` on a fresh clone of `613fb7cadb32b2f73bbda823e8ace40ba99eabc7`: **3533 passed, 0 failures/errors/skips**, all eight shards PASS. Exact JUnit inventory equals all 3533 collected cases, with every mandatory identity executed. Separate OS-process and resource replay shards retained. Sum of shard elapsed times: 239.491s. No remaining Hub/runtime service processes after teardown.

`python -m tools.verify_package`: **394 files PASS**. Compileall and git diff --check PASS. Final evidence-only edits are resealed separately; full tested manifest and JUnit/inventory SHA-256 recorded in `docs/ACCEPTANCE_REGRESSION_20261112.json`.

`python -m bcc.gui_check --offscreen --output /workspace/work/verified-gui`: **PASS** on the same clean source: eight actual strategy wizard create/edit/reopen/deposit workflows, four operator cycles, background 20-row fleet monitor and theme/catalog checks. GUI command checks are explicitly intent-only; separate real-worker replay tests run in the full suite. Linux offscreen does not certify native Windows.

Earlier runs remain honestly recorded in the machine evidence: six reproduced handle/CLI failures; strict JUnit subtest mismatch; 3531/3532 monolithic pass with one stale concurrency-rejection test; first complete sharded run with one expired replay-candle failure. Both were fixed and superseded by the final full PASS. Targeted groups: 146, 155, 16, 17 and 125 passed as recorded; do not sum them as unique coverage.

## Outstanding acceptance / next exact action

`python -m tools.verify_windows --output /workspace/work/verified-native` returned **2 / BLOCKED**: attached executor is Linux. Native Windows clean-unpack Modern Qt E2E and independent review remain **NOT RUN / NOT CERTIFIED**. Live read-only provider check observed **Bybit HTTP 403 RESTRICTED**, **Binance HTTP 451 RESTRICTED**, Gamma/CLOB PASS. This is not a complete live PAPER cycle; no restriction bypass or repeated provider retry was attempted.

Next: use a fresh source snapshot of this branch HEAD on actual Windows with permitted access to all required public providers. Run `python -m tools.verify_package`, then `python -m tools.verify_windows --output <new-directory-outside-checkout>`. Inspect full exact inventory, native Qt screenshots/scaling, independent workers, Stop/Restart/forced recovery, shared risk and ledger audits; complete every live strategy workflow in `ACCEPTANCE_WINDOWS_RU.md` and independent review. Keep PR #11 draft and block merge/release until every gate passes. No paid Actions or interim builds.
