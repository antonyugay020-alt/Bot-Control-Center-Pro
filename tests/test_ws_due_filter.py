"""WS event routing must never hide REST margin marks or other book depths."""
from bcc.market_hub import HubRegistry

TICKER = "https://api.bybit.com/v5/market/tickers"
BOOK = "https://api.bybit.com/v5/market/orderbook"


def test_due_filters_only_exact_websocket_request_shapes(tmp_path):
    hub = HubRegistry(tmp_path / "hub.sqlite")
    hub.subscribe(TICKER, {"category": "linear", "symbol": "BTCUSDT"})
    hub.subscribe(TICKER, {"category": "linear"})
    hub.subscribe(BOOK, {"category": "linear", "symbol": "BTCUSDT", "limit": 50})
    hub.subscribe(BOOK, {"category": "linear", "symbol": "BTCUSDT", "limit": 200})
    all_requests = hub.due(10)
    assert len(all_requests) == 4
    remaining = hub.due(10, skip_ws_routes=(TICKER, BOOK))
    assert len(remaining) == 2
    assert {(url, frozenset(params.items())) for _, url, params in remaining} == {
        (TICKER, frozenset({"category": "linear"}.items())),
        (BOOK, frozenset({"category": "linear",
                          "symbol": "BTCUSDT", "limit": 200}.items())),
    }


def test_due_filter_keeps_unmigrated_routes(tmp_path):
    hub = HubRegistry(tmp_path / "hub.sqlite")
    hub.subscribe(TICKER, {"category": "linear"})
    # Broad mark-price packs are still served by Bybit REST.
    result = hub.due(10, skip_ws_routes=(TICKER,))
    assert len(result) == 1
