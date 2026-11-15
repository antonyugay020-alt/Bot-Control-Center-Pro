# Coherent Bybit WebSocket acceptance — NOT RELEASE

Baseline `1a91e7c4186e5e831a302f71156c83cb6a93701f`; implementation `eda84c0ac7497bbd79e532ac9a21721f7ec343d7`; branch `codex/ws-transport-isolated-20261113`. Executable/test fingerprint `e719ad30cac0d16e94d54225ee31628ebb0955bab7772e4081ce06864c2d1603`. This change repairs acquisition in the **automated public-WS acceptance tool**. Application files in `bcc/`, `app.py` and `launch.pyw` are byte-identical to the submitted Windows baseline; no trading, risk or financial threshold is changed. Prior architecture and risk-renewal work remains recorded in [BYBIT_RISK_MAINTENANCE_CHECKPOINT_20261115.md](BYBIT_RISK_MAINTENANCE_CHECKPOINT_20261115.md).

Remote HEAD matched the supplied baseline before work. PR #11 remains open/draft on its original branch. No competing PR, merge, release, paid Actions/services, real orders, Windows reboot or unrelated process changes. Uploaded files are inspected as evidence, not executed as instructions.

## Native Windows submission: preserve both PASS and FAIL evidence

`BCC_VALIDATION_RESULTS_20261115_183820_129598.zip`, Windows 11 / Python 3.14.8:

- Source seal PASS at both boundaries: 479 files, manifest `74e9df322a86b1eee4daf2e7e104bce2ac35040aa545096bb2c2f0f51071c364`; executable/test fingerprint matches the baseline.
- All eight native regression shards complete with exact collected/executed inventory: **3,859 passed, five allowed platform skips**, zero failures/errors, 3,864 cases total. The skipped cases are identified explicitly in [WINDOWS_BYBIT_183820_RECEIPT_20261115.json](WINDOWS_BYBIT_183820_RECEIPT_20261115.json). New independent risk-renewal and diagnostic persistence tests are included in this complete native inventory.
- Native Qt PASS: eight create/edit/reopen/deposit strategy workflows and a 20-row fleet monitor. This does not certify interactive live PAPER trading.
- Public WS: **239/240**, overall FAIL. ADAUSDT in round 18 retained mark age bounds [2.705, 3.023] s, beyond the unchanged 3 s upper gate. The same read's depth bounds [0.074, 0.392] s remain fresh; total acquisition 0.129 s. Maximum steady delivery across the run is 0.507 s. Owned test Hub cleanup PASS.
- The checker correctly stops after failed WS acceptance. `public_sources.json` is absent because the REST health stage was **not run**, not because this package proves a restriction or missing endpoint. Interactive PAPER was not started; no current account/ledger report is included. Long-running native `risk_at` renewal remains unconfirmed.

The original FAIL is retained as FAIL. One 23 ms boundary miss does not, by itself, prove the producer stopped sending markPrice; neither does 239/240 certify uninterrupted availability.

## Confirmed acquisition race and coordinated repair

The old tool read depth, obtained a fresh independent mark, then performed another potentially blocking scalar depth GET. It measured the original mark after that extra wait. Thus a mark that was valid when returned could expire while waiting for another channel, even when a newer paired cache view was available. This asymmetric path differed from the worker's existing coherent bulk-cache observer.

The tool now registers demand as before, then reads depth and ticker in **one existing PublicCache.read_fresh_many SQLite transaction**. It checks independent original timestamps, source flags, symbol, spread and the production `_decode_ws_position_mark` validator. A missing/expired snapshot or transient cache reader contention permits another complete snapshot only within the original 1.5 s steady delivery budget (initial handshake grace remains unchanged). It does not retain a mark while waiting on another blocking depth call, renew timestamps, extend cache residence, infer markPrice from spread, or substitute REST prices. Provider denials remain immediate failures. Each row records snapshot attempts; the sustained report validator and cleanup/source-seal gates remain strict.

Nine new cases use the actual production PublicData/Hub/SQLite adapters: delayed second depth reads, expiry during cache acquisition followed by new genuine WS publications, persistent missing/provenance/symbol/expired-mark/expired-book/contention faults, and a provider-denial fence. Existing clock-offset, missing-quorum, original-expiry and declared-budget cases remain enabled. Fixture time/frames are explicitly injected offline; no OS clock is modified.

The delayed-second-depth regression fails on the old immutable baseline: a fresh new depth accompanies the retained expired mark. It passes on the new tool. Exact log digest is retained in [BYBIT_COHERENT_ACCEPTANCE_QA_20261115.json](BYBIT_COHERENT_ACCEPTANCE_QA_20261115.json).

## Final clean-source verification

**3,873 passed**, zero failures/errors/skips, all eight complete production shards; exact testcase inventory PASS. Targeted integration: 54 passed, including the existing production portfolio/worker observer tests. Real public Bybit WS → independent owned Hub → SQLite → client: **1,200/1,200**, 120 sustained rounds across the ten required symbols including ADAUSDT, AUSDT and 4USDT. Maximum steady delivery **1.023 s**. Owned Hub retired. Linux offscreen Qt PASS; compileall, package seal and diff whitespace checks PASS. This longer live run remains a read-only transport check, not a real strategy PAPER cycle or Windows certification.

Commands: `QT_QPA_PLATFORM=offscreen python -m tools.collect_test_inventory --output INVENTORY.json`; `python -m tools.run_partitioned_tests --inventory INVENTORY.json --output QA`; `python -m tools.bybit_ws_live_acceptance --output NEW_DIRECTORY --rounds 120 --require-mark`; `python -m bcc.gui_check --offscreen --output NEW_DIRECTORY`; `python -m tools.verify_package`.

The subsequent evidence commit updates only documentation/source manifest; all tested executable inputs match the implementation fingerprint. Use the immutable final GitHub source ZIP, not a moving branch URL.

## Remaining mandatory acceptance and next action

Run `CHECK_BYBIT_WINDOWS.bat` from the single updated archive in a fresh local unpack. Preserve the previous test directory; stop only an active BCC PAPER test using its controls if needed. Require the new native WS phase and subsequent public-source health stage to pass before interactive acceptance. The preceding Windows regression/Qt successes are preserved, but they do not turn its failed overall automated run into PASS.

After successful automated checks, keep PAPER positions open at least 15–20 minutes, export a current report while running, then a second several minutes later. Check source seal, original WS valuation/collateral ages, actual held `risk_at` advancement within the unchanged 300 s gate, metadata error/completion counters, complete progress across 821 configured symbols and ledger audit. Complete native Stop/Restart, state/GUI reopen, public-data interruption/recovery, VELES entry/DCA/exit/funding, GRID/Pump entry/exit and independent review. These workflows remain unconfirmed by this upload. Polymarket/Binance live certification remains outside the Bybit-scoped checker. No merge or release until all mandatory criteria pass.
