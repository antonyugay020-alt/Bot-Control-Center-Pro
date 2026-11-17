# Native launch replay isolation — NOT RELEASE

Baseline `782a92955b02559d8b986f77e6d4a2e7c2d590d8`; implementation `90dc70a1180189c456fb338658b707b262739f38`; branch `codex/ws-transport-isolated-20261113`. Tested executable/test fingerprint `cc02c5fec8302e2530d749f3ecf53b72464e11f3d5b6de0f2c05e958fa6c8cda`. Production `bcc/` and `tools/` bytes are unchanged. No paid Actions/services, real orders, release, merge, Windows reboot or unrelated process changes. PR #11 remains open/draft on its original consolidation branch.

## Received Windows evidence

The two uploads named `BCC_VALIDATION_RESULTS_20261116_203458_548689.zip` are byte-identical, SHA-256 `6dade923bc2a387511b1a6fa703e512d6642859dd6c38a66c62df13a75f2319a`. Native Windows 11 / Python 3.14.8 verified the expected source before and after execution: **493 files**, manifest `6b7d7018d5260e6c0ff5703f85eca60bf3e5c04e14894dc30ffed48409c4fe0f`, fingerprint `459b285517bac6e82ab7ae6e62a7c86d0438f7dbb8ec0fc8988190a013923160`.

The exact complete inventory is **3,892 cases: 3,886 passed, one failure, five allowed platform skips**. All **seven atomic publication regressions PASS on Windows**, including the real 56-event SQLite batch, independent clocks, admission fences, rollback and queue fairness. The failure is `test_worker_constructor_error_is_reported_and_retry_is_safe`: before its intentionally broken replay worker could start, Lifecycle launched an unused public WS Hub, which correctly refused readiness because no independent clock quorum was established.

The earlier replay isolation change had not covered the two launch-handshake replay cases. This was an incomplete test-boundary correction. The other case happened to pass in the uploaded run, but had the same external dependency. The report does not establish which Windows clock provider or network condition caused the quorum failure. Freshness and independent-clock admission remain mandatory.

The launcher correctly stopped before the separate **public WS, GUI acceptance and public health** stages. Missing reports from those stages mean NOT RUN, not additional implementation failures and not acceptance. This ZIP contains no new running-account/long-duration price evidence. Manual Windows acceptance remains NOT_CONFIRMED.

## Coordinated correction

1. A shared explicit `replay_hub_boundary` fixture now serves the startup Stop replay and both launch-handshake replay cases. It asserts the exact repository root and `bybit_ws=True` capability request. Real OS worker processes, launch tokens/claims, constructor error publication, retry, Stop/Restart, persisted positions and ledger audit still execute. Both handshake cases verify two actual lifecycle requests and absence of an unused Hub process record.
2. An autouse test-only guard rejects an uncontrolled real Hub spawn carrying any current public WS transport flag. It fails before creating the external child, instead of relying on successful public clock access. Explicit injected transport tests and actual non-WS Hub lifecycle tests continue to run. The separate genuine public acceptance tool does not load pytest fixtures and remains mandatory. This guard is narrowly scoped to public Hub startup; it is not a production network policy or a claim that every possible socket API is sandboxed.
3. A new real lifecycle case proves that the guard blocks WS startup before a process receipt exists, then verifies actual no-stream Hub start, reuse, owned cleanup and replacement. No test was skipped, expected failure hidden, production readiness faked or clock/freshness bound changed.

On the previous immutable source, both affected replay cases fail immediately under the new guard. The corrected targeted group passes **22 tests**, including OS launch/recovery and four strategy adapter/Hub/ledger replay cycles. Full sealed-copy regression with the guard active across all eight shards: **3,893 passed**, zero failures/errors/skips, exact collected/executed identities PASS. Compileall, whitespace and source seal PASS.

Separate public Bybit WS → owned Hub → SQLite → client: **1,200/1,200**, maximum steady delivery **0.909 s**, owned Hub retired. Linux offscreen Qt PASS. These are new source-bound Linux checks, not native Windows acceptance or proof of uninterrupted marks under the user's long-running portfolio. Source identities, sanitized native receipt, test/JUnit and live report digests: [BYBIT_OFFLINE_BOUNDARY_QA_20261117.json](BYBIT_OFFLINE_BOUNDARY_QA_20261117.json).

Commands: `QT_QPA_PLATFORM=offscreen python -m tools.collect_test_inventory --output INVENTORY.json`; `python -m tools.run_partitioned_tests --inventory INVENTORY.json --output QA`; `python -m tools.bybit_ws_live_acceptance --output NEW_DIRECTORY --rounds 120 --require-mark`; `python -m bcc.gui_check --offscreen --output NEW_DIRECTORY`; `python -m tools.verify_package`.

## Next Windows gate

Use the immutable source ZIP for the final evidence commit in a **new folder**, retain the existing PAPER installation/account, and run **CHECK_BYBIT_WINDOWS.bat**. The production trading code matches the previous packet; this update repairs the regression boundary so the separate native public-data and GUI checks can actually be reached.

Remaining criteria from [BYBIT_ATOMIC_WS_PUBLICATION_CHECKPOINT_20261116.md](BYBIT_ATOMIC_WS_PUBLICATION_CHECKPOINT_20261116.md) remain open: full native automated/public-WS/Qt acceptance on the packaged source, loaded portfolio mark continuity and Cross valuation, native held-position Stop/Restart/GUI reopen, own-transport interruption/recovery, GRID/Pump & Dump PAPER cycles, required funding workflow and independent review. Polymarket/Binance live acceptance is outside the Bybit phase. No merge or release until mandatory criteria pass.
