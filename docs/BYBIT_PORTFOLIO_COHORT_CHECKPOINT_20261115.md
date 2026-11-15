# Coherent portfolio WS valuation — NOT RELEASE

Baseline `8ada7975ea52e46b44e7265d6a9e7fe754b2e920`; final implementation `7ae3ccfabb97c2150a8c9abbf5485cecca887b2d`. Code commits acea89b and 7ae3ccf form one integration package.

## Accepted operator evidence and remaining failure

The Windows report verified the 453-file source seal before and after checking; all 3,764 collected cases completed (3,759 passed, five allowed platform skips), native Qt PASS, public Bybit REST PASS, and 240/240 book plus independent WS mark reads PASS (maximum steady latency 0.989 s). The real VELES PAPER ledger recorded 22 OPEN, 22 CLOSE and one PARTIAL close. Four closes were by SIGNAL_EXIT; 18 final closes plus one partial fill were requested by MANUAL_PAPER_CLOSE. BIRBUSDT's remaining quantity was subsequently closed. There were no positions/pending orders left. Cash, realized result, fees and position ledger reconcile; an independent audit of a separate copied SQLite ledger passed. Every one of 45 fills carried valid book-age evidence, maximum upper age 2.603307 s. The owned worker/services were stopped and state preserved. No DCA fill, operator Restart, or real GRID/Pump cycle is proved by this report.

However, the saved state contains 792 stale-mark diagnostic rows across 198 scanned symbols, often naming a large part of the open portfolio. Scanning 821 configured coins inspected 258 symbol diagnostics during the session. Six symbols retained candle-cache errors (see the sanitized receipt). A healthy ten-symbol transport probe does not certify price continuity for an entire simultaneously held portfolio. Candle error causes are not fully identified from the original archive because final CLI collection omitted shared-Hub diagnostics.

## Coordinated implementation

- PublicCache.read_fresh_many reads admitted public keys from ONE SQLite read snapshot, with host-denial fencing and TTL checks. Queries are chunked for SQLite variable limits; this is not an asset/position ceiling. It neither sends HTTP nor claims leases/request budget. Tests exercise 1,001 identities.
- The scheduler supplies the account's actual exposure set to PublicData. Concurrent readers coalesce a nonblocking portfolio cache snapshot for 0.1 s while retaining original exchange and receipt ages. Each row independently validates WS mark and executable bid/ask provenance, symbol, finite positive prices and vetted UTC freshness. REST rows, expired data, missing sides and wrong symbols cannot authorize another channel. Missing data remain missing.
- The serial owning worker commits all available fresh portfolio collateral/valuation observations in its existing financial transaction before risk checks. Only held-position valuations are refreshed, and auxiliary quotes are never treated as executable orderbook depth. The per-symbol price/depth lane and real scalar ticker calls still renew their admitted/urgent subscription leases. No other account's financial state is changed.
- Slow risk metadata cannot erase a newer valid price, and older price evidence cannot roll it back. Scanner admission in strict WS mode now uses actual WS marks, never the broad REST ticker response time to refresh collateral. History-lane risk-tier refresh is independent of BOTH price transports. New scanner symbols get bounded subscription grace on the slow lane; protective position reads retain their short budget.
- Cached execution books retain depth for manual exits, not a separate full portfolio map per asset. A lifetime/garbage-collection regression prevents quadratic retention of cohort observations. No timestamps, price freshness limits or exposure safety gates are relaxed.
- Windows final CLI collection now includes the same sanitized Hub log/metadata evidence as the UI exporter. It does not export raw market cache databases or stop/restart processes. This supports investigation of the six remaining candle errors.

## Executed final-source verification

```json
{
  "implementation_commit": "7ae3ccfabb97c2150a8c9abbf5485cecca887b2d",
  "baseline_commit": "8ada7975ea52e46b44e7265d6a9e7fe754b2e920",
  "source_integrity": {
    "status": "PASS",
    "verified_files": 455,
    "checkpoint": "BCC_BYBIT_SHORT_ASSET_DEPTH_RECOVERY_20261115_NOT_RELEASE",
    "manifest_sha256": "9fbc4de44ac62e20cf7445f0d2c8f20508b0f828c5ae248d09f71de22de57c7b"
  },
  "executable_fingerprint": "55de15b72f2eb9025fbe0cdba0487443ad71e3bef6b3bb082c44fb59c0ffa81a",
  "platform": "Linux Python 3.12.14; Qt regressions offscreen",
  "regression": {
    "tests": 3781,
    "failures": 0,
    "errors": 0,
    "skipped": 0
  },
  "inventory_count": 3781,
  "junit_sha256": "cf5e409ed00ccb2f67ca30489f7975c9b842ef53054d6041ccafcac5fd7f7c8b",
  "live_ws_status": "PUBLIC_WS_HUB_CLIENT_PASS",
  "live_ws_readings": 240,
  "live_ws_max_steady_seconds": 1.31,
  "live_ws_report_sha256": "adae8091116fa6fa236dc8dd56f4717fa8ce833f6f9c750c84247d672f9926f5",
  "owned_hub_retired": true,
  "new_windows_acceptance": "UNRUN",
  "release_ready": false
}
```

Commands: QT_QPA_PLATFORM=offscreen python -m tools.collect_test_inventory --output INVENTORY.json; python -m tools.run_partitioned_tests --inventory INVENTORY.json --output QA; python -m tools.bybit_ws_live_acceptance --output NEW_DIRECTORY --require-mark. Full runs use a clean sealed copy and the exact collection/JUnit identity inventory.

New coverage uses 20 open positions, actual fenced Hub publications and client SQLite reads, a real owner transaction and persisted ledger audit. One tick restores all available valid valuations without extra executions. Other tests cover independent mark/quote failures, local and cross-process provider denial including cached cohorts, concurrent-read coalescing without retimestamp, strict scanner provenance, tier-only recovery, cold subscription grace, urgency/lease renewal, retained-memory bounds, large SQL batches and safe CLI evidence. This is injected offline load coverage; it is NOT a completed native live-portfolio acceptance.

Documentation/evidence commits only reseal the manifest; executable and test fingerprints remain the tested implementation's. Earlier full-source regression found an implicit encoding in the new diagnostic test; explicit UTF-8 was corrected before the final complete rerun.

## Required next Windows milestone

Use the updated immutable package and CHECK_BYBIT_WINDOWS.bat. With approximately 15–20 PAPER positions still OPEN, use the UI Save Report action before closing them: live Hub diagnostics and financial snapshots are needed to verify coherent valuation under load and locate candle errors. Complete DCA, operator Stop/Restart/reopen/network recovery, real GRID/Pump cycles and a complete large-universe scan. Preserve the accepted VELES entry/exit/partial-close evidence. Independent review and full new-source native acceptance remain required before merge/release. No paid Actions/services, real orders, release, merge, reboot or unrelated process changes.
