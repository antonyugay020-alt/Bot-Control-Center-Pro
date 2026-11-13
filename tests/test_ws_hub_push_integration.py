"""Integration: real SQLite Hub accepts push events without REST fetch cycles."""
import time

from bcc.market import PublicData
from bcc.market_hub import MarketDataHub


class QuoteStream:
    def quote(self, symbol):
        assert symbol == "BTCUSDT"
        return 100.0, 101.0, time.time()


class BookStream:
    def get_orderbook(self, symbol):
        assert symbol == "BTCUSDT"
        return {
            "bids": [[100.0, 1.0]], "asks": [[101.0, 2.0]],
            "ts": time.time(), "u": 10, "seq": 20,
        }


def test_hub_pushes_2_market_feeds_with_one_leased_producer(tmp_path):
    source = PublicData(quote_stream=QuoteStream(), orderbook_stream=BookStream())
    hub = MarketDataHub(tmp_path / "market.sqlite", source=source)
    try:
        ticker_url = "https://api.bybit.com/v5/market/tickers"
        book_url = "https://api.bybit.com/v5/market/orderbook"
        ticker_params = {"category": "linear", "symbol": "BTCUSDT"}
        book_params = {"category": "linear", "symbol": "BTCUSDT", "limit": 50}
        hub.registry.subscribe(ticker_url, ticker_params)
        hub.registry.subscribe(book_url, book_params)
        assert hub.acquire_leadership()
        assert hub.publish_ws_event("tickers", "BTCUSDT")
        assert hub.publish_ws_event("depth50", "BTCUSDT")
        quote = source.shared.read_fresh(ticker_url, ticker_params, "api.bybit.com")
        book = source.shared.read_fresh(book_url, book_params, "api.bybit.com")
        assert quote["_bcc_ws_quote"] is True
        assert quote["result"]["list"][0]["symbol"] == "BTCUSDT"
        assert book["_bcc_ws_book"] is True
        assert book["result"]["seq"] == 20
        with source.shared.db() as db:
            assert db.execute(
                "SELECT count FROM request_budget WHERE host='api.bybit.com'"
            ).fetchone() is None
    finally:
        hub.close()
