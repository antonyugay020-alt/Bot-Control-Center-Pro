# BCC WebSocket integration — isolated branch checkpoint

**NOT RELEASE. NOT MERGED. NO LIVE BOT PROCESSES OR DATA TOUCHED.**

Development branch: `codex/ws-transport-isolated-20261113`, based on consolidated PR #11 at `7dace5b70b487d5dc5bf714f4989f55cabc307e5`.

## User-supplied files incorporated

- `bootstrap_stage2.py`: Bybit public linear `orderbook.50.SYMBOL` subscriptions, batches of 10, root-level `ts`, full snapshot + delta level insertion, price/quantity replacement/deletion, sorted bids/asks, API to retrieve an orderbook.
- `bootstrap_stage3b.py`: adaptation of WS orderbook to the existing `PublicData.orderbook()` response structure. Metadata, risk parameters, validation and existing strategy-facing fields are retained.
- `bootstrap_stage4.py`: multi-symbol subscription intent is provided by Hub registration and dynamic producer subscription, **not** by constructing an independent WS connection in every worker.
- `bootstrap_stage1.py` creates a separate desktop copy and is **not executed**: this GitHub branch already provides a safe independent integration; no existing directory is overwritten.
- `bootstrap_stage3.py` copies clock logic but changes book freshness to 15 s. That relaxation is deliberately **not adopted**, because it is unsafe for trade/exit decisions; existing clock authority stays intact.
- `test_ws.py` tests five live symbols but is not an offline acceptance test; no public Bybit network connection was attempted.

## Integrated implementation

- `bcc/ws_orderbooks.py`: orderbook depth50 snapshot/delta state, dynamic multi-symbol subscriptions, batch size 10, generation fencing, reconnect, monotonic/market-time freshness, rejection of out-of-order and malformed updates, cross-book/inverted-price protection, cleanup on failure.
- `bcc/ws_quotes.py`: independent per-symbol public tickers, generation-fenced short-lived quotes and automatic transport reconnect.
- `bcc/market.py`: opt-in `PublicData.get()` intercept for Bybit depth50 and ticker stream; strict WebSocket origin checks at the Hub consumer; existing `orderbook()` adapter can request depth50 without changing strategy-facing output. Missing WebSocket data blocks instead of silently using REST.
- `bcc/data_cache.py`: only exact public Bybit WS ticker or depth50 cache publications may bypass REST request-budget accounting. SQLite leases and provider access denial remain in force.
- `tools/market_hub_service.py` and `bcc/hub_supervisor.py`: opt-in producer flags `BCC_BYBIT_WS_QUOTES=1` / `BCC_BYBIT_WS_BOOKS=1`; existing Hub and workers remain unchanged unless a clean isolated producer is explicitly started with the flags.
- Tests added: `tests/test_ws_orderbooks.py`, `test_ws_orderbook_hub.py`, and prior `test_ws_quote_*.py` cases. This code is **not** enabled on a live installation.

## Executed and unexecuted verification

- **Executed locally on exact uploaded `bootstrap_stage2.py` code:** Python parse PASS; synthetic snapshot + delta update PASS; reproduced stale-book-after-disconnect and delta-before-snapshot hazards.
- **Not executed on the actual integrated GitHub branch:** complete pytest suite, integrated tests, full source integrity seal, shared-cache concurrency/load, real Bybit WebSocket connection, Windows Qt E2E. No CI or paid Actions started.
- Bybit streaming depth50 is **not equivalent** to the previous REST depth200; insufficient executable liquidity must remain an explicit denial. Historical candles, instruments, risk limit, portfolio marks and funding still use REST. Binance and Polymarket WS integration not done.

## Release blockers

Test real integrated source in an isolated checkout; fix failures as a group; validate Hub producer/worker lifecycle across process death, stale SQLite entries and 500+ symbol subscriptions without touching trading accounts. Follow with full Windows Qt clean-unpack acceptance. Do not merge or release on source inspection alone.

## 2026-11-13 continuation — multi-symbol scheduling and producer fencing
- Added `HubRegistry.active_bybit_symbols()`: read-only scan of validated live per-symbol ticker and depth50 requests.
- `MarketDataHub.subscribe_active_websockets()` now pre-registers all actively requested symbols at most once per second, independently from serialized REST-shaped due queue.
- Bybit ticker reconnection batches topics in groups of 10, matching the user-supplied depth50 subscription approach.
- The Hub service no longer opens Bybit WebSockets before winning its durable producer lease. Losing leadership terminates the WS-enabled service instead of leaving a zombie connection.
- Added `tests/test_ws_hub_bulk_subscription.py` with synthetic 120-symbol workload and route filtering.
- These changes are committed; local full-checkout tests and Windows Qt E2E remain **UNRUN** in this session; do not deploy or flip flags on live bot fleets.

## 2026-11-13 event-driven follow-up (same development branch)
- WebSocket receive callbacks notify one bounded/coalescing event publisher, instead of waiting for every symbol to reach the REST-shaped Hub due scheduler.
- PublicCache.publish_websocket() commits a verified per-symbol quote/depth50 under a single SQLite transaction, requiring live leader token, live verified subscription and active provider-denial policy. Does not debit REST request budget.
- Hub due queue excludes only stream-covered Bybit per-symbol ticker and depth50 shapes; broad mark-price ticker packets and depth200 instrument books remain REST (important for protective margin).
- Bybit subscription fanout now pre-registers active symbols and sends 10 topics per message, including on reconnect. Unit regression includes 500 simultaneous symbol subscriptions.
- Synthetic test modules added: test_ws_hub_push_integration.py, test_ws_event_publication.py, test_ws_due_filter.py, test_ws_500_subscriptions.py. **Not yet executed against the full repository**.
- SQL predicate for filtering non-symbol ticker packs was checked in isolated SQLite locally: the broad ticker and depth200 request remain while symbol ticker and depth50 are excluded.
- Required before deploying: full checkout regressions, real Bybit protocol/connection and many-worker latency tests, native Windows Qt E2E. Binance, Polymarket and Bybit live candle streams are still not migrated.

## 2026-11-13 late continuation: Bybit 1-minute candle and partial-delta bridge

- The Bybit ticker receiver merges partial `delta` updates while independently expiring the bid and ask fields; a one-sided delta never renews the other side's timestamps.
- Added `bcc/ws_klines.py` for the **current 1-minute bar only**, with generation fencing, message-time checks, price/volume validation, confirmation monotonicity, and bounded reconnect.
- Streaming `kline.1.SYMBOL` updates are published by the same event-driven Hub to the REST-compatible `/v5/market/kline?category=linear&symbol=...&interval=1&limit=10` cache identity. `PublicData.candles()` retains its historical REST backfill (`limit=100..1000`) and continuity validation.
- New transport is **OFF by default**. Enable only on isolated copies using `BCC_BYBIT_WS_KLINES=1`; old running processes and databases are never migrated automatically. Explicit mode fails closed when the producer is absent.
- Local verification on an isolated faithful reproduction of the three WebSocket receiver modules: Python compileall PASS and **27/27 synthetic unit tests PASS**, including 500-subscription batching for quote/depth50/kline. This is not a full GitHub checkout regression.
- Additional real SQLite in-memory checks: strict due-filter selected only the three stream-covered per-symbol routes, preserving broad ticker marks, depth200 and historical kline/time-range REST calls. This is not live-exchange evidence.
- **Blocking acceptance still missing**: full repository test suite, network Bybit V5 WS exchange verification and subscription limits, load tests on a real multi-process fleet, native Windows Qt E2E, independent clean-unpack release QA. Binance and Polymarket market streams remain unmigrated. No release and no deployment to live bots.

## 2026-11-13 Polymarket PAPER streaming integration

Source branch: `codex/ws-transport-isolated-20261113`. This is an **isolated, OFF-by-default development branch**, not a release.

- Added `bcc/ws_polymarket.py` public CLOB market stream: snapshot + price_change depth merging, UP/DOWN token subscriptions, market rotation/unsubscribe, application PING, epoch/freshness and depth validation.
- Added `bcc/ws_binance.py` public spot-trade stream for the current price in `PolymarketData.impulse()`. Historical market-start OHLCV remains REST.
- Shared Hub dynamically subscribes to CLOB token IDs and Binance spot symbols, coalesces incoming events and publishes **without per-coin REST polls** into the existing SQLite cache. Cache publication is fenced by live Hub leader lease, active subscriber, and host policy denial; it consumes no REST request budget.
- `PolymarketData.book()` and `outcome_quote()` keep their existing validated public `get("/book")` contract. Their WS cache rows carry `_bcc_ws_polybook`, Binance spot carries `_bcc_ws_binance`. Explicit WS mode will NOT accept an old REST row as a WS snapshot.
- Experimental flags for independent clean checkout only: `BCC_POLY_WS_BOOKS=1`, `BCC_BINANCE_WS_PRICE=1`; both default to disabled, and no running bots or databases are changed.
- Offline isolated local harness (reproduction of new transport modules, not a full checkout) **28 PASS / 0 FAIL**: Polymarket CLOB snapshot, ASK/BID deltas, 2-asset batches, stale expiry, epoch/fail-close, token rotation, malformed asset identifiers, subscription batching; Binance seven-coin prices, invalid/expired/out-of-order trades, reconnection, 60-symbol subscription batching. Additional local SQLite query check confirms CLOB book requests are filtered from periodic REST scheduling while Gamma and Binance historical requests remain.
- Committed more synthetic test modules `tests/test_ws_polymarket.py`, `test_ws_polymarket_hub.py`, `test_ws_polymarket_rotation.py`, `test_ws_binance.py`, `test_ws_poly_binance_hub.py` but **the full GitHub checkout regression suite has NOT been executed**.
- **Release blockers:** live WS CLOB/Binance protocol and subscription limits must be verified against provider documentation and real responses; producer/worker multiprocess integration and latency/load tests on independent checkout; complete regression including VELES/GRID/Pump/Polymarket and real Windows Qt clean-unpack E2E. Web research is disabled for this session. Do not activate flags for actual PAPER operations until those gates pass.

- Additional offline lifecycle mocks passed: CLOB first public market subscription/snapshot/shutdown and Binance first spot trade/subscription/shutdown. Total isolated reproduced local transport tests: **28 PASS / 0 FAIL**. These are not equivalent to real CLOB/Binance network protocol acceptance or full Windows QA.

## 2026-11-13 integration hardening (no live deployment)

- All five WS receivers (Bybit ticker/depth50/kline1, Polymarket public CLOB, Binance public trade) now fence delayed socket-close/error callbacks by connection generation. A stale old socket cannot revoke a new connection.
- CLOB snapshots accept either `bids/asks` or `buys/sells`, but reject ambiguous or incomplete mixtures; event snapshots and deltas remain fail-closed.
- Invalid Binance spot-trade payloads revoke the cached price rather than leaving an old PAPER impulse price available.
- Added bounded `bcc/ws_dispatch.py`: urgent CLOB/depth changes, regular quote/trade/kline updates, per-asset notification coalescing, fair access for regular streams, bounded backlog and dropped-notification counters. A surge of Binance ticks cannot indefinitely hide CLOB ASK updates.
- Added shared `ReconnectBackoff` policy with exponential 1..30-second retry and reset after a 30-second healthy session; this prevents reconnection storms during provider outages.
- Hub service logs only connection status and dispatcher aggregate counters every 30 seconds; does not log tokens, keys or quote payloads.
- Added `tools/ws_offline_acceptance.py`, a no-paid-actions local isolated-checkout gate. It rejects live runtime bot state/cache, syntax-checks core modules, runs every `tests/test_ws_*.py` through pytest with explicit WS runtime flags removed, supports up to 150 separate Python executions and optional full Python regression. It deliberately reports `BLOCKED_NATIVE_WINDOWS_QT_E2E_UNRUN` even if offline tests pass.
- In this session, an isolated reproduction of the new fair dispatcher and shared retry policy was Python-compiled and executed: **8/8 PASS**. Five-stream generation-regression test module and other new integration test modules were saved to GitHub but **have not been executed against a full checkout**.
- **Next mandatory release gates:** run unified WS QA and full Python suite in an independent clean checkout; resolve all failures systematically; verify real venue WS handshakes/subscription limits and measure latency under load; perform native Windows Qt full-system E2E without touching 24/7 live bots or monitors. Do not merge or release until all mandatory gates pass.

## Vetted clock and cross-venue timeout fence

- The production Hub now supplies `market_now` from BCC's independent `MarketClock` as the wall-clock source for **all five** exchange WebSocket decoders. The producer calls `clock.ensure()` before announcing readiness and refreshes every 10 seconds; a failed clock cannot authorize new streaming publication.
- The Hub's five exchange-time freshness predicates now compare with the same vetted trading UTC instead of `time.time()`. SQLite lease/cache expiry intentionally remains on the shared local wall clock because this is a cross-process implementation detail, not the exchange timestamp.
- `PolymarketData.impulse()` validates the Binance WS timestamp against freshly verified trading UTC when and only when the WS provenance marker is present. REST-only operation retains its preexisting behavior.
- Added `tests/test_ws_market_clock_alignment.py` for an offset OS clock; no result from full-repository test execution is claimed.
- Real exchange handshake/snapshot replay and 100/500/1000 active-symbol latency are **not** verified in this session: the network browser and native Windows Qt runtime are unavailable here. Do not release or activate on 24/7 running bots.
