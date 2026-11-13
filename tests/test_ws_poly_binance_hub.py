"""Offline combined CLOB ASK + Binance impulse shared Market Hub integration."""
import time
from bcc.market import PublicData
from bcc.market_hub import MarketDataHub

POLY = "https://clob.polymarket.com/book"
BINANCE = "https://api.binance.com/api/v3/ticker/price"


class Poly:
    def __init__(self):
        self.tokens = []
    def sync_assets(self, assets):
        self.tokens = list(assets)
    def get_book(self, asset):
        return {
            "asset_id": asset, "timestamp": str(int(time.time() * 1000)),
            "bids": [{"price": "0.42", "size": "5"}],
            "asks": [{"price": "0.48", "size": "9"}],
        }


class Binance:
    def __init__(self):
        self.coins = []
    def subscribe(self, coins):
        self.coins.extend(coins if isinstance(coins, list) else [coins])
    def quote(self, symbol):
        return 100.0, time.time()


def test_paper_up_down_and_impulse_share_one_hub_without_http(tmp_path):
    poly = Poly()
    binance = Binance()
    source = PublicData(polymarket_stream=poly, binance_stream=binance)
    hub = MarketDataHub(tmp_path / "paper.sqlite", source=source)
    try:
        up, down = "1001", "1002"
        for token in (up, down):
            hub.registry.subscribe(POLY, {"token_id": token})
        hub.registry.subscribe(BINANCE, {"symbol": "BTCUSDT"})
        assert hub.acquire_leadership()
        hub.subscribe_active_websockets()
        assert poly.tokens == [up, down]
        assert binance.coins == ["BTCUSDT"]
        for token in (up, down):
            assert hub.publish_ws_event("polybook", token)
            book = source.shared.read_fresh(
                POLY, {"token_id": token}, "clob.polymarket.com")
            assert book["_bcc_ws_polybook"]
            assert book["asks"][0]["price"] == "0.48"
        assert hub.publish_ws_event("binance_trade", "BTCUSDT")
        trade = source.shared.read_fresh(
            BINANCE, {"symbol": "BTCUSDT"}, "api.binance.com")
        assert trade["_bcc_ws_binance"]
        assert trade["symbol"] == "BTCUSDT"
        assert trade["price"] == "100.0"
        assert hub.registry.due(
            10, skip_ws_routes=hub._websocket_queue_exclusions()) == []
        with source.shared.db() as db:
            assert db.execute("SELECT count(*) FROM request_budget").fetchone()[0] == 0
    finally:
        hub.close()


def test_binance_historical_signal_is_preserved_as_rest(tmp_path):
    hub = MarketDataHub(tmp_path / "paper.sqlite",
                        source=PublicData(binance_stream=Binance()))
    try:
        historical = {
            "symbol": "BTCUSDT", "interval": "1m",
            "startTime": 1763000000000, "limit": 1,
        }
        hub.registry.subscribe(
            "https://api.binance.com/api/v3/klines", historical)
        hub.registry.subscribe(BINANCE, {"symbol": "BTCUSDT"})
        assert hub.acquire_leadership()
        due = hub.registry.due(
            10, skip_ws_routes=hub._websocket_queue_exclusions())
        assert len(due) == 1
        assert due[0][1] == "https://api.binance.com/api/v3/klines"
    finally:
        hub.close()
