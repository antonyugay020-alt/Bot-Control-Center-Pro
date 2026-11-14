# Bybit integrated QA candidate — NOT RELEASE

Baseline branch: `codex/ws-transport-isolated-20261113`.
Initial remote HEAD: `753f906c6e30f2785ff1707d3eeabce66e5496fa`.
User's sealed reference: `65f22ba1dcd2f6bc336010757cdffc6530acc43f`.
The initial working tree was clean, but the manifest had stale Hub and ticker hashes after the three subsequent commits. User changes are retained.

## Coordinated implementation

- Quiet-symbol recovery requests genuine exchange snapshots before the two-second independent observation TTL expires. Recovery is batched (ten symbols per command), has initial handshake grace and a per-symbol one-second retry bound. No cached timestamp or mark value is fabricated. Lost/delayed responses still fail closed.
- Mark ordering is independent of executable quote ordering. A valid mark is published even when an unchanged quote side expires. Duplicate mark timestamps cannot renew receipt evidence.
- Expired independently timestamped quote sides remain unavailable to consumers but can recover via subsequent partial deltas. Reconnect/generation fences still prevent mixing connections.
- The first genuine mark ends handshake grace before its own TTL expires.
- Reconnect callbacks capture their original session instead of aliasing the next loop iteration. Actual two-connection tests reproduced stale-close/error revocation in all five decoders; the common binding was corrected consistently, including the CLOB heartbeat. Deferred venue protocols and activation settings are unchanged.
- Failed recovery closes the owned socket and revokes its observations instead of leaving an unreachable live socket.
- Actual quote decoder -> fenced Hub SQLite -> mark reader -> worker transaction -> per-owner SQLite -> Qt integration covers BTCUSDT and ADAUSDT, independent collateral marks, outage revocation and recovery, ledger audit and reopened state.
- `CHECK_BYBIT_WINDOWS.bat` runs one scoped native QA session. Sustained source-bound WS evidence (24 x eight symbols, all independent marks, bounded steady latency, owned cleanup) is mandatory in addition to full regression, native Qt and public Bybit REST checks. Invalid/incomplete evidence cannot certify acceptance. Other venues are deferred, not deleted or certified.

## Executed evidence and boundaries

- Five regression scenarios independently reproduced defects against the exact baseline ticker source; all passed after the shared fix.
- First complete integration cycle: 3,703 tests PASS, zero failures/errors/skips; exact collection/JUnit identity inventory, eight production shards. Qt offscreen acceptance PASS, all eight strategy editors and 20-row monitor. This predates the final sustained Windows gate and extended BTC/ADA collateral slice.
- Second complete clean-copy integration cycle: 3,719 tests PASS, zero failures/errors/skips, and 192/192 actual public WS book + independent mark observations. A subsequent first-snapshot grace regression and actual callback-level reconnect reproduction prompted the final refinement; these results are not reused as final-source acceptance.
- Final candidate regression/live results are recorded in the session evidence update below. Native Windows is unavailable in this Linux session.
- Public Bybit WS was reachable. Public Bybit REST health returned HTTP 403 from this environment; no proxy, alternate market venue or trading-data bypass was used. Public independent clock references do not substitute for denied market data.
- Offline strategy/accounting/recovery regressions do not prove actual real-data PAPER fills on Windows. VELES, GRID and Pump & Dump live entry, DCA, exit, Stop/Restart, financial ledger, monitoring and network recovery still require the clean-unpack native manual session and independent review.

Next operator action: follow `BYBIT_WINDOWS_QA_RU.md` once using the final immutable source archive. No Actions, paid service, release, merge, real orders, reboot or unrelated process changes are authorized or performed.

## Final implementation evidence

Implementation commit: `14ef8723a8ad29947eece5af5d195d1f39e70916`.
Clean-copy production eight-shard regression: **3,726 passed, zero failures/errors/skips**, exact collected/executed case inventory.
Real public Bybit WS: **192/192 successful reads**, 24 rounds including ADAUSDT and independent markPrice; maximum steady read latency **0.957 s**; owned Hub cleanup PASS.
Final Qt offscreen acceptance PASS (eight editors and 20-row monitor). Syntax compilation PASS.
Final Bybit REST health: HTTP 403, correctly RESTRICTED. Native Windows and actual strategy PAPER cycles remain unconfirmed.

Commands, platform, source fingerprint, per-symbol results, shard totals and raw-evidence digests are retained in `BYBIT_QA_EVIDENCE_20261114.json`.
This evidence checkpoint changes documentation and the integrity manifest only. Executable and test bytes remain identical to the tested implementation commit. The final GitHub archive must verify the complete seal and this same executable fingerprint before handoff.
