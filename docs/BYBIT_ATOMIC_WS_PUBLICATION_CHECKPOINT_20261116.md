# Bybit atomic WS publication under portfolio load — NOT RELEASE

Baseline `049f5953f08a22cfecdf4cc3746c9d197000500f`; implementation `2ed573e58a83125eed3852eb049dc52f9be22a1b`; branch `codex/ws-transport-isolated-20261113`. Tested executable/test fingerprint `459b285517bac6e82ab7ae6e62a7c86d0438f7dbb8ec0fc8988190a013923160`. No paid services/Actions, real orders, release, merge, Windows reboot or unrelated process changes. PR #11 remains open/draft on its original consolidation branch. Remote development HEAD matched at session start. Uploaded contents were treated as evidence, not executed as instructions.

## Native evidence and outstanding symptom

`BCC_CURRENT_REPORT_20261116_154500.zip` matches the expected 049f595 source: seal PASS, 490 files, manifest `3081dacd3205355228b2495fe9b4040b096579c290803a7fac537870fd444025`, fingerprint `38acfe1fe0678485c33fc9c10b7228cd4a8b5df05c2b6f4c7d4bb2cc018cf7e9`. The Windows VELES worker is RUNNING around **15:45 (+05:00)** with **43 positions across 28 instruments**. Its 56 executions are **48 OPEN, three DCA, five CLOSE**. All 56 have valid execution-age evidence, maximum original upper age 1.989521 s. Collector audit and an independent audit of a separately copied SQLite account PASS. The original account and running Windows process were not modified.

Saved observations show fresh executable bid/ask for all held symbols at the latest saved cohort, but six retained independent marks exceed three seconds there (ACUUSDT, AGPUUSDT, AMZNUSDT, APEXUSDT, ARKMUSDT, ARMUSDT); ABBVUSDT is near the boundary. All Cross positions carry the account-level failure `Устарел mark-price ABBVUSDT`. This is a real incomplete collateral assessment, not permission to replace markPrice with executable spread or suppress the warning. Snapshot ages describe their saved observation boundary, not continuous availability or ZIP completion time.

821 symbols are configured and 246 have diagnostics at this point; a complete 821-symbol scan is not established by this report. Metadata refresh records 46 successful ticks and zero errors. Worker maximum tick is 1.0993 s, local writer lock misses zero and read-lock misses two. Hub has no dropped notifications, but recorded maximum critical queue wait **0.748 s** and overall wait **1.501 s**. This current-report ZIP does not contain the full Windows automated acceptance result.

## Coordinated implementation

The event publisher previously crossed a separate SQLite BEGIN/COMMIT boundary for every notification. A 28-instrument mark/depth burst thus required 56 transactions and exposed partial portfolio updates to consumers while the rest waited. The service now drains **up to 64 already queued events** using the existing fair priority order and publishes their latest verified stream observations in one transaction. It never waits to fill a batch, never caps the number of bots or instruments, and retains the per-event API through the same admission implementation.

The batch validates/encodes all payloads before taking the writer, binds preparation and publication to the same Hub incarnation, checks the live lease, each exact active subscription and each provider denial inside the transaction, then commits the allowed rows together. One inactive subscription does not suppress healthy others. A malformed payload or SQL failure cannot partially publish. Quote and independent mark exchange timestamps, TTLs, clock quorum, expiry, HTTP quotas and financial owner boundaries are unchanged. Cached observations still have to pass the original consumer freshness checks.

Seven new cases prove a real 56-event SQLite batch with exactly one commit, independent timestamps and reader isolation before commit; lost leader, expired subscription and provider denial; malformed/SQL rollback and recovery; absent marks without fabricated collateral; and fair/coalesced/bounded queue drainage. A singleton-publication negative control fails the exact one-commit assertion.

An explicitly injected **40 ms writer delay** reproduces the scaling mechanism: serial publication takes 2.032 s, admits 50/56 events and leaves 15/28 fresh marks at cohort completion; batching takes 0.042 s, admits 56/56 and retains 28/28. Without injected latency, measurements are 0.0114 s versus 0.00275 s. These are Linux simulations/measurements, **not Windows performance guarantees or proof of the sole cause of every missing price**.

## Completed verification

- Full sealed-copy regression: **3,892 passed**, zero failures/errors/skips, all eight shards, exact collected/executed identities verified. Targeted integration: **62 passed**. Compileall, whitespace and source seal PASS.
- Real public Bybit WS → separately owned Hub → SQLite → client: **1,200/1,200**, maximum steady delivery **1.110 s**. Expanded 28-instrument sampling: **672/672**, maximum **0.613 s**. Both owned Hubs retired.
- A separate read-only probe applies the worker's original-age retention rules to all 28 held symbols every 0.25 s: **120/120 simultaneous complete fresh portfolios** for the selected implementation. A separate baseline window has 117/120. This short, non-simultaneous comparison is not a causal benchmark or a guarantee of uninterrupted Windows operation. It extends the per-symbol probe, whose retries alone cannot prove simultaneous Cross collateral availability.
- Actual owned Hub replacement: expired cache rejected; replacement yields nine valid paired reads; original exchange timestamps advance; the persistent read-only connection survives; cleanup PASS. Linux Qt offscreen PASS. Neither substitutes for native Windows held-position restart.

Detailed sanitized native evidence, exact source/JUnit/test digests and experiment outcomes: [BYBIT_ATOMIC_WS_PUBLICATION_QA_20261116.json](BYBIT_ATOMIC_WS_PUBLICATION_QA_20261116.json).

A second recovery idea was **rejected**, not shipped: delaying duplicate resubscriptions up to 1.5 s passed 37 targeted deterministic tests but produced only 25/120 complete retained live portfolios. All changes to quote recovery and its associated tests were reverted. Selected files were then checked byte-for-byte against the already fully verified 3,892-test sealed source. An exploratory working-tree live trial whose seal changed during a docstring edit was also discarded. Raw-cache completeness probes were superseded by owner-style retention probes; missing raw rows alone do not establish missing persisted collateral. This preserves negative evidence instead of converting an isolated green test into acceptance.

Commands: `QT_QPA_PLATFORM=offscreen python -m tools.collect_test_inventory --output INVENTORY.json`; `python -m tools.run_partitioned_tests --inventory INVENTORY.json --output QA`; `python -m tools.bybit_ws_live_acceptance --output NEW_DIRECTORY --rounds 120 --require-mark`; `python -m bcc.gui_check --offscreen --output NEW_DIRECTORY`; `python -m tools.verify_package`.

## Remaining Windows acceptance / next action

Use the immutable source QA ZIP from the final evidence commit in a **new folder**, retain the existing installation/account, and run **CHECK_BYBIT_WINDOWS.bat**. This code has not yet been accepted on Windows. Repeat the loaded 28-instrument PAPER run and collect current reports to confirm mark continuity, cohort latency, Cross valuation and retained financial state. Do not claim the missing-price symptom permanently resolved from the short Linux probe.

Remaining mandatory gates: native clean-unpack automated/public-WS/Qt checks on this exact source; interactive restart with held positions and GUI reopen; interruption/recovery of only BCC's own transport; native GRID/Pump & Dump entries/exits/DCA where applicable; required funding workflow and independent review. Earlier VELES facts remain scoped to their recorded versions. Polymarket/Binance live acceptance is outside the Bybit phase. No merge or release until the mandatory criteria pass.
