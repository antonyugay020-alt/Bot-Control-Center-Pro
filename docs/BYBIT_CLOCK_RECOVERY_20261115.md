# Windows report integration — NOT RELEASE

Baseline `429398fcd2e9925dbb0c517d49d0a74b1749fd96`; implementation `6fcd61ad7e0292770fe547a9cf195f9cd697e210`.

The submitted clean-unpack Windows 11 / Python 3.14.8 report verified 445 source files. Native Qt (eight editors, 20-row monitor) PASS. Full regression: 3,721 passed, five expected platform skips, no failures/errors. Live WS: four of 192 passed; 187 book freshness/symbol failures and one stale mark. Owned Hub cleanup PASS. REST validation and interactive PAPER were not reached. Sanitized evidence is in WINDOWS_BYBIT_RECEIPT_20261115.json; private operator paths and raw databases are not committed.

## Confirmed causes and coordinated fixes

1. The producer checked exchange timestamps against independently verified MarketClock UTC. The raw-cache QA reader never initialized its own MarketClock and used local wall time for both acceptance and reported ages. Persisted book publication deadlines minus the known 0.75 s residence placed local publication roughly 0.85 s before the exchange timestamps. The reader therefore rejected fresh data as future-dated. QA now initializes and maintains its own vetted UTC reference, records its evidence and reports both age bounds. No PC clock settings, freshness tolerance or exchange timestamps are modified.
2. Waiting for an independent mark could expire the previously read book. QA now re-reads the genuine WS cache and rechecks both original timestamps after the wait. Expired data and missing clock quorum still fail closed; error rows preserve measured age bounds. Repeated evidence errors are coalesced.
3. A one-second subscription discovery scan also throttled quiet-mark recovery. A deterministic production-Hub regression showed zero recovery messages inside this interval while the stream correctly needed one snapshot. Recovery now checks cached validated demand every 0.25 s; SQLite discovery remains once per second, per-symbol outbound throttling and generation/leadership fences remain intact. This avoids an additional database scan per recovery tick.

The clock-only intermediate cycle passed 3,733 local tests but real WS was 191/192; that result was not accepted. It led to the Hub scheduling correction above. New regressions cover negative/positive PC offsets, stale data, time-quorum failure, books expiring during mark acquisition and the between-scan recovery window without extra SQLite discovery.

## Final implementation evidence

```json
{
  "implementation_commit": "6fcd61ad7e0292770fe547a9cf195f9cd697e210",
  "baseline_commit": "429398fcd2e9925dbb0c517d49d0a74b1749fd96",
  "executable_fingerprint": "d18b1bc55b5f95b3fb094ce9960f628c7a19cb33237695180d7f627ca093005b",
  "tested_source_integrity": {
    "status": "PASS",
    "verified_files": 446,
    "checkpoint": "BCC_BYBIT_FINAL_PAPER_QA_20261114_NOT_RELEASE",
    "manifest_sha256": "a8c26c2cee9b0318e052f4329da88edab1e4bad76b9391bcb608373c7afe2cd4"
  },
  "platform": "Linux, Python 3.12.14; Qt regressions use offscreen",
  "regression": {
    "tests": 3734,
    "failures": 0,
    "errors": 0,
    "skipped": 0
  },
  "inventory_count": 3734,
  "junit_sha256": "683f0d08623916eada07f9877c104df2f762f454bcca72050780cb0ca1b8c85b",
  "inventory_sha256": "f0b9f519d9069e5ab657735851ae6f6a826c6bb37a75319b54cb59279da2f8d8",
  "live_ws_status": "PUBLIC_WS_HUB_CLIENT_PASS",
  "live_ws_success": 192,
  "live_ws_rounds": 24,
  "live_ws_report_sha256": "f591f5ab7c63d2c8fbbd285bc83ac7e20e2b9fee05ac69e3def08f3567420dd0",
  "live_ws_max_steady_seconds": 0.93,
  "owned_hub_retired": true,
  "clock_selected_sources": [
    "Bitget",
    "OKX"
  ],
  "final_windows_rerun": "UNRUN; submitted native Qt and full inventory passed on baseline 429398f, live WS did not pass",
  "release_ready": false
}
```

Commands: QT_QPA_PLATFORM=offscreen python -m tools.collect_test_inventory --output INVENTORY.json; python -m tools.run_partitioned_tests --inventory INVENTORY.json --output QA; python -m tools.bybit_ws_live_acceptance --output NEW_DIRECTORY --require-mark. Eight full production shards, exact identity inventory; public WS probe uses 24 rounds / eight symbols including ADAUSDT, independent mark and book age bounds.

The evidence commit updates documentation and the source manifest only; executable/test fingerprint remains identical to implementation_commit.

## Remaining acceptance

Run CHECK_BYBIT_WINDOWS.bat once from the updated immutable clean archive. New native full regression/live WS, reachable public REST and actual PAPER VELES/GRID/Pump & Dump entry/DCA/exit, financial accounting, Stop/Restart, window reopen/network recovery and independent review remain mandatory. Prior native GUI evidence is preserved but does not certify the changed whole system. No real orders, paid services/Actions, releases, merge, reboot or unrelated process changes.
