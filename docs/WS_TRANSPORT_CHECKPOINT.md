# BCC WebSocket transport integration — isolated development checkpoint

**NOT RELEASE / NOT MERGED / NO LIVE BOTS TOUCHED.**

Source baseline: consolidated PR #11 HEAD `7dace5b70b487d5dc5bf714f4989f55cabc307e5`.
Development branch: `codex/ws-transport-isolated-20261113`.

## Implemented together
- One opt-in public Bybit linear ticker WebSocket reader with dynamic subscriptions, generation fencing, bounded freshness, disconnect invalidation and close-on-stop.
- Explicit `PublicData` quote adapter and producer-side `/v5/market/tickers?category=linear&symbol=...` interception. Existing Hub workers still subscribe and consume via the shared cache; no direct WebSocket per bot.
- Cache publication carries an explicit origin marker; a stale REST cache row does not masquerade as a WebSocket snapshot.
- Shared publication retains SQLite ownership/lease fencing and host denial checks but does not consume the HTTP provider request rate budget.
- Hub service has explicit `--bybit-ws-quotes` mode; ordinary supervisor launches accept only `BCC_BYBIT_WS_QUOTES=1` as opt-in. Off by default; no running producer replaced.
- Quote transport never silently falls back to HTTP if the stream is absent or stale. Funding, history, instrument lookup, broad mark-price and orderbook HTTP routes remain unchanged.

## Evidence and limits
- Independent minimal local recreation of the WebSocket reader: Python compileall PASS; 8 / 8 behavioral unit tests PASS (freshness, invalid quote, disconnect, epoch, runtime subscribe, shutdown, failed send). This is **not** an exact checkout full-suite result.
- Committed regression modules: `tests/test_ws_quotes.py`, `test_ws_quote_lifecycle.py`, `test_ws_quote_transport.py`, `test_ws_quote_public_data.py`, `test_ws_quote_hub_cache.py`, `test_ws_quote_cache_budget.py`. The repository tests were **not executed** in this session.
- No public network WebSocket connection, live exchange data, multi-process load test, clean Windows Qt E2E, full source seal audit, or full application regression. Do not claim release readiness or activate the flag on any live installation.

## Next linked acceptance gate
Execute complete source regression on an isolated checkout. Repair all failures as a coherent group. Extend WS to Bybit orderbook/kline, Binance and Polymarket with verified independent per-provider message contracts and restart/recovery. Test many workers, exchange outages and policy denial without touching running bot DBs. Only then native Windows GUI clean-unpack acceptance.
