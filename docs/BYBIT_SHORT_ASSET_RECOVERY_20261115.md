# Bybit short asset and quiet depth integration — NOT RELEASE

Baseline `5d0ac68afd0797ad5a6376c7d4672f058fc881b2`; implementation `2d3b6229e45703394b2148bf684d0d96a1cf71bd`.

## Accepted operator evidence

The submitted Windows report passed the exact source seal (448 files), all 3,734 collected native cases (3,729 passed, five permitted platform skips), native Qt, 192/192 public WS book + independent mark reads, and public Bybit REST validation. The screenshot shows an earlier zero-position state; the final collected PAPER ledger contains two genuine OPEN fills, AMZNUSDT and ASMLUSDT. Fees/balance and position quantities reconcile. An independent audit on a separate copied ledger passed. Both positions had fresh executable valuations at the final committed snapshot; the owned test session stopped its worker/services and preserved both positions. DCA, CLOSE, operator Restart and GRID/Pump live cycles are not proved by these two entries. Sanitized evidence is retained in WINDOWS_BYBIT_PAPER_RECEIPT_20261115.json; account identifiers, paths, full configuration and financial balances are excluded.

## Root causes and coordinated correction

- The scanner selected 821 symbols, but the four duplicated WS regexes required at least TWO characters before USDT. AUSDT and 4USDT were silently filtered during Hub subscription discovery, producing cache timeouts. Thirteen configured assets matched this same defect (A, B, C, F, H, M, O, Q, S, T, V, W and 4). One shared topic contract now admits 1..24 uppercase/digit asset characters. Topic separators, trailing newlines, empty assets and excessive lengths remain rejected. This is syntax admission only; venue policy, instruments and executable data checks remain mandatory.
- Quiet depth can stop producing changes while the short cache residence expires. Fresh observations were eventually returned, but a ten-symbol probe exposed read latencies up to 4.833 s, so that run was not accepted despite 240 valid samples. Depth now requests genuine snapshots before expiry, using the same batched request primitive as independent ticker marks. Initial grace, per-symbol retry bounds, generation revocation and owned-socket close on failure are retained. Both depth and mark recovery use cached validated demand at 0.25 s cadence; SQLite discovery remains once per second. Requests alone do not renew observations; original timestamps still expire at the unchanged gates.
- The raw QA mark wait used a one-second cutoff despite its declared 1.5 s total steady-read budget. The wait now uses that same overall deadline, with final freshness validation of both original observations. A deterministic 1.2-second arrival regression verifies the contract; actual end-to-end latency is still independently rejected above 1.5 s. An intermediate 239/240 run was not accepted.
- The mandatory live probe now includes AUSDT and 4USDT alongside the prior eight symbols: 24 rounds / 240 reads. Actual quote/depth/kline decoders and production Hub/SQLite consumers are covered for both short assets with REST forbidden. New tests cover malformed inputs across all four readers, quiet-depth expiry/no retimestamp, batching, send failure and recovery between subscription scans.

## Final implementation evidence

```json
{
  "implementation_commit": "2d3b6229e45703394b2148bf684d0d96a1cf71bd",
  "baseline_commit": "5d0ac68afd0797ad5a6376c7d4672f058fc881b2",
  "source_integrity": {
    "status": "PASS",
    "verified_files": 451,
    "checkpoint": "BCC_BYBIT_CLOCK_RECOVERY_20261115_NOT_RELEASE",
    "manifest_sha256": "e9bb870c3c7433287e50d2753a13810d498f9c4a491d1ff0eafde4c51767266f"
  },
  "executable_fingerprint": "8dfa832607b30c486eaef5888b8c3e6f9175a656b509f480f4d83af4fe8a8c2d",
  "platform": "Linux Python 3.12.14; Qt regressions offscreen",
  "regression": {
    "tests": 3764,
    "failures": 0,
    "errors": 0,
    "skipped": 0
  },
  "inventory_count": 3764,
  "merged_junit_sha256": "3d75526887b73eb4d2eb07bdfbeba5b6b57a6d32bfbb32e87b537a2adb085143",
  "live_ws_status": "PUBLIC_WS_HUB_CLIENT_PASS",
  "live_ws_readings": 240,
  "live_ws_max_steady_seconds": 0.808,
  "live_ws_report_sha256": "95cb3e114da4a484bba6bfefb050e002f26e2ce106027418121796314e5ebda2",
  "owned_hub_retired": true,
  "new_windows_acceptance": "UNRUN",
  "release_ready": false
}
```

Commands: QT_QPA_PLATFORM=offscreen python -m tools.collect_test_inventory --output INVENTORY.json; python -m tools.run_partitioned_tests --inventory INVENTORY.json --output QA; python -m tools.bybit_ws_live_acceptance --output NEW_DIRECTORY --require-mark. Full regression runs from a clean sealed copy. Documentation/manifest updates do not change the tested executable fingerprint.

## Remaining acceptance

Use the updated immutable clean archive and CHECK_BYBIT_WINDOWS.bat for the changed ten-symbol native gate. Preserve prior accepted Windows evidence. Complete real-data PAPER DCA, exits, Stop/Restart/reopen/network recovery and financial reconciliation for VELES/GRID/Pump & Dump, a full large-universe scan, and independent review before merge/release. The operator evidence proves entry and preserved state, not the full trading lifecycle. No paid Actions/services, releases, merge, real orders, reboot or unrelated process changes.
