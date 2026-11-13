"""MarketClock producer alignment: market data uses trading UTC, not OS wall."""
import time
from unittest.mock import patch

from bcc.market import PublicData
from bcc.market_hub import MarketDataHub


class ClockShiftedBinance:
    def quote(self, symbol):
        assert symbol == "BTCUSDT"
        return 100.0, time.time() + 12.0


class ClockShiftedClob:
    def get_book(self, symbol):
        return {
            "asset_id": symbol,
            "timestamp": str(int((time.time() + 12.0) * 1000)),
            "bids": [{"price": "0.4", "size": "1"}],
            "asks": [{"price": "0.5", "size": "1"}],
        }


def test_vetted_utc_allows_synchronized_exchange_time_when_os_clock_is_skewed(tmp_path):
    source = PublicData(
        polymarket_stream=ClockShiftedClob(),
        binance_stream=ClockShiftedBinance(),
    )
    hub = MarketDataHub(tmp_path / "clock-offset.sqlite", source=source)
    try:
        hub.registry.subscribe(
            "https://clob.polymarket.com/book", {"token_id": "123"})
        hub.registry.subscribe(
            "https://api.binance.com/api/v3/ticker/price",
            {"symbol": "BTCUSDT"})
        assert hub.acquire_leadership()
        with patch("bcc.market_clock.market_now",
                   side_effect=lambda: time.time() + 12.0):
            assert hub.publish_ws_event("polybook", "123")
            assert hub.publish_ws_event("binance_trade", "BTCUSDT")
    finally:
        hub.close()
