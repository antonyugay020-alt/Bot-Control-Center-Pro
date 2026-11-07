# Controlled integration checkpoint — 2026-11-07

**DEVELOPMENT / NOT RELEASE.** Same branch `codex/architecture-consolidation-20261107`, draft PR #11. Session baseline `b316457d5205cb886aeb530672414a61108d0781`; resulting milestone is the commit containing this checkpoint. Reread remote HEAD before resuming. Earlier evidence remains in `docs/REGRESSION_20261107.json`.

## Integrated portfolio recovery milestone

A fresh 4×4 strategy matrix reproduced sixteen unsafe admissions: a published owner with a missing paper.sqlite disappeared from the global risk snapshot, allowing VELES/GRID/Pump & Dump/Polymarket to allocate against unknown exposure. Monitor/dashboard/support also treated it as an empty/new account. Shared storage now distinguishes never-started owners from lost initialized ledgers, using durable account/process witnesses; every observer and risk consumer applies that boundary. No blank DB or fresh balance is created. Incomplete fleet totals display unknown values instead of zero/full cash. A published-account iterator observes one import commit snapshot, excludes staging/deleted/quarantined/uncommitted owners, and includes financial folders even if configuration was lost.

New offline tests execute real engines/modern perpetual margin, cross-strategy allocation, verified SQLite backup/restore, official Polymarket settlement despite another lost owner, concurrent import publication, support diagnostics and actual Qt close/reopen. **59 linked checks passed**, then an expanded **103 passed** (25.72s), including legacy recovery and Qt regressions. Four initial Qt total assertions reproduced incomplete totals before the display fix.

Full checkout cycle1: `QT_QPA_PLATFORM=offscreen /workspace/work/venv/bin/python -m pytest tests -q --timeout=180 --junitxml=/workspace/work/portfolio-full1.xml`: **3393 passed / 1 failed**, 227.53s. The sole failure was backward-compatible error wording for a legacy process witness; fixed without changing the fail-closed behavior and verified in the expanded 103-test group. Complete final regression is still required after this milestone.

## Independent Hub / real adapter / strategy slice

`QT_QPA_PLATFORM=offscreen /workspace/work/venv/bin/python -m pytest -q tests/test_market_fleet_recovery_e2e.py --timeout=180 --junitxml=/workspace/work/market-fleet-green.xml`: **4 passed**, 8.96s. Each core strategy reads through production PublicData/PolymarketData and an independent production Hub service with synthetic HTTP only, opens a real audited SQLite position, blocks a competing allocation, survives an abrupt producer kill and generation-fenced replacement without account changes, and commits a closed cycle with exact cash/event reconciliation. VELES exits on a new closed candle; GRID/Pump use fresh depth, and Polymarket settles on a synthetic official response. Initial test development exposed two fixture errors (cached pump frame and VELES signal-vs-TP expectations), corrected by waiting for verified new frames and supplying a new closed exit candle; no production freshness rule was weakened. This is not live-market or native Windows acceptance.

## Environment / blockers / next exact milestone

Linux, Python 3.12.14 / SQLite 3.53.1 / PySide6 6.12.0 / pytest 9.2.0, dependencies in `/workspace/work/venv`. `python -m tools.verify_windows --output /workspace/work/native-preflight-session2` returned **2 / BLOCKED**, explicitly requiring native Windows Qt. No Windows executor is attached; no paid Actions, interim release/archive, user accounts, model APIs or real orders used.

Next: strengthen native acceptance to require each actual cross-strategy recovery and real-adapter Hub cycle (including every parametrized identity), rerun full checkout plus GUI and clean source verification. Then execute clean-unpack native Windows Qt E2E and independent review; keep PR draft and do not merge/release. Durable commands/results/fingerprints: `docs/PORTFOLIO_REGRESSION_20261107.json`. Temporary raw JUnit/logs live outside the checkout in `/workspace/work/`.
