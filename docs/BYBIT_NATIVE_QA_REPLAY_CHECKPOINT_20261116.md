# Windows regression isolation and concurrency acceptance — NOT RELEASE

Baseline `52c6ac6461757ce38d19021f48130b0f770d6d1f`; verified implementation `fb1a0fbd2cedd1722a51dba702938676f852530e`; branch `codex/ws-transport-isolated-20261113`. Executable/test fingerprint `38acfe1fe0678485c33fc9c10b7228cd4a8b5df05c2b6f4c7d4bb2cc018cf7e9`. Production modules are byte-identical to the baseline. No paid Actions/services, real orders, release, merge, Windows reboot or unrelated process changes. PR #11 remains draft on its original consolidation branch.

## Received Windows result

`BCC_VALIDATION_RESULTS_20261115_213916_212228.zip` is the expected baseline: both source checks PASS, 488 files, manifest `913d7a3863350387f2d91ac2e40986de2aed207a0995fde124c9ebbccc12d5fa`. Native Windows 11 / Python 3.14.8 ran the exact full 3,884-case inventory: **3,877 passed, two failed, five allowed platform skips**. All eleven preceding WAL reader/owner-validation/health recovery regressions PASS on Windows. This accepts those test cases, not the entire application.

The launcher correctly stopped before its separate public WS, GUI and public health stages. Those missing reports are **NOT RUN**, not three additional transport/UI failures and not PASS. This archive has no new live bot account evidence. It cannot establish that the operator's missing-price symptom has disappeared.

The first failure is the offline startup/Stop replay launching an unused real WS Hub. Its worker already uses ReplayPublicData, but Lifecycle.start requested a live producer; that producer correctly refused readiness when public clock probes could not form an independent quorum. The log does not establish which Windows provider or network condition caused this particular quorum failure. The second failure collects only five urgent completions within a fixed 0.42-second test window, against an expectation of six, while a background request sleeps 0.9 s. That wall-clock race does not by itself demonstrate broken priority scheduling.

## Coordinated test corrections

- Scope the replay's producer boundary to an asserted Bybit WS capability request. Its real OS worker, launch claims, Stop controls, Store, positions, journal and audit remain enabled. Verify actual restart through Lifecycle.start with a different launch token, one persisted OPEN and one held PAPER position after clean Stop. No production clock bypass or configurable safety exception was added.
- Replace fixed-duration background sleeps with explicit release barriers in both urgent-batch and real Hub/cache/client tests. The urgent test now requires **all eight distinct keys**, spanning multiple dispatch waves, while background completion is still forbidden; it also asserts bounded concurrency and no duplicate background request. Run normal and deliberately delayed owner variants. The real SQLite producer/client test must deliver the urgent snapshot before its background metadata barrier is released. Deadlines remain hard failures; owned threads are released/joined during cleanup.
- No test was removed or skipped; no price age, independent mark, time quorum, cache expiry, quota, worker budget or production deadline changed.

Negative controls: the previous replay fails when external Hub spawning is forbidden; the entire corrected six-case real-process module passes under the same prohibition. A deliberately serialized scheduler fails **both** new urgent variants with the expected stalled-batch assertion. These changes preserve detection of the actual scheduling defect rather than simply extending the old sleep.

## Verified result and provenance

Targeted integration: **29 passed**. Final full regression on a separate sealed source copy: **3,885 passed**, zero errors/failures/skips, all eight shards; collected/executed identities match exactly. The extra case is the slow-owner variant. The real process restart is part of the strengthened existing case. Linux Qt offscreen PASS, compileall, whitespace and source seal PASS.

Separate genuine public Bybit WS → owned Hub → SQLite → client: **1,200/1,200 paired book + independent mark observations**, 120 rounds across ten symbols including ADAUSDT, maximum steady delivery **0.728 s**, owned Hub cleanup PASS. The independent time quorum uses the existing approved OKX/Bitget time-only fallback; Bybit market prices still come from Bybit WS. This Linux check does not certify Windows connectivity or uninterrupted future freshness.

Full sanitized native receipt, source/test/JUnit/live digests and negative controls: [BYBIT_NATIVE_QA_REPLAY_20261116.json](BYBIT_NATIVE_QA_REPLAY_20261116.json). Local completed results were recovered after environment reconnection; no interrupted or merely launched task was counted as PASS. The evidence-only follow-up reseals docs while preserving tested executable/test bytes.

Commands: `QT_QPA_PLATFORM=offscreen python -m tools.collect_test_inventory --output INVENTORY.json`; `python -m tools.run_partitioned_tests --inventory INVENTORY.json --output QA`; `python -m tools.bybit_ws_live_acceptance --output NEW_DIRECTORY --rounds 120 --require-mark`; `python -m bcc.gui_check --offscreen --output NEW_DIRECTORY`; `python -m tools.verify_package`.

## Exact next action and remaining gates

Use the immutable GitHub source ZIP for this checkpoint in a fresh folder and run **CHECK_BYBIT_WINDOWS.bat**. Keep the existing working folder and PAPER state intact. The corrected complete native run, separate public WS/Qt/health stages and manual clean-unpack acceptance are still pending.

Continue the remaining interactive Windows checks from [BYBIT_WAL_OWNER_CHECKPOINT_20261115.md](BYBIT_WAL_OWNER_CHECKPOINT_20261115.md): restart with held positions and GUI reopen, own-transport interruption/recovery, GRID and Pump & Dump PAPER entries/exits/DCA where supported, mandatory funding workflow and independent review. Prior native VELES long-run acceptance remains valid only for its recorded source/scope. Polymarket/Binance live acceptance is outside this Bybit phase. Do not merge or release until mandatory criteria pass.
