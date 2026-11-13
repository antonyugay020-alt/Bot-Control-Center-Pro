"""Isolated subscription fan-out: 120 coins must not await due REST polls."""
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from bcc.market_hub import HubRegistry, MarketDataHub


class DummyStream:
    def __init__(self):
        self.items = []

    def subscribe(self, symbols):
        if isinstance(symbols, str):
            symbols = [symbols]
        self.items.extend(symbols)


class DemandFanoutTests(unittest.TestCase):
    def test_bulk_book_subscriptions_from_registry(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "hub.sqlite"
            registry = HubRegistry(path)
            for i in range(120):
                registry.subscribe(
                    "https://api.bybit.com/v5/market/orderbook",
                    {"category": "linear", "symbol": f"COIN{i}USDT", "limit": 50})
            tickers, books = registry.active_bybit_symbols()
            self.assertFalse(tickers)
            self.assertEqual(len(books), 120)

            source = type("Source", (), {})()
            source.shared = None
            source._quote_stream = None
            source._orderbook_stream = DummyStream()
            source.close = lambda: None
            hub = MarketDataHub(path, source=source, max_workers=1)
            try:
                self.assertTrue(hub.acquire_leadership())
                hub.subscribe_active_websockets()
                self.assertEqual(len(source._orderbook_stream.items), 120)
            finally:
                hub.close()

    def test_only_verified_routes_are_ws_subscribed(self):
        with TemporaryDirectory() as root:
            registry = HubRegistry(Path(root) / "hub.sqlite")
            registry.subscribe(
                "https://api.bybit.com/v5/market/tickers",
                {"category": "linear", "symbol": "BTCUSDT"})
            registry.subscribe(
                "https://api.bybit.com/v5/market/orderbook",
                {"category": "linear", "symbol": "BTCUSDT", "limit": 200})
            registry.subscribe(
                "https://api.bybit.com/v5/market/orderbook",
                {"category": "linear", "symbol": "ETHUSDT", "limit": 50})
            ticker, book = registry.active_bybit_symbols()
            self.assertEqual(ticker, {"BTCUSDT"})
            self.assertEqual(book, {"ETHUSDT"})


if __name__ == "__main__":
    unittest.main()
