"""The Hub must advance REST candle discovery despite urgent position work.

Synthetic provider calls, isolated SQLite, no network or account state.
"""
import threading
import time

from bcc.market_hub import HubRegistry, MarketDataHub


BOOK = "https://api.bybit.com/v5/market/orderbook"
KLINE = "https://api.bybit.com/v5/market/kline"


def test_two_background_history_lanes_progress_under_continuous_position_marks(tmp_path):
    path = tmp_path / "public-hub.sqlite"
    registry = HubRegistry(path)
    for i in range(12):
        registry.subscribe(
            BOOK, {"category": "linear", "symbol": f"OPEN{i}USDT", "limit": 50},
            priority=True,
        )
    scan_names = [f"SCAN{i}USDT" for i in range(2)]
    for symbol in scan_names:
        registry.subscribe(KLINE, {
            "category": "linear", "symbol": symbol, "interval": "1", "limit": 100
        })
    release = threading.Event()
    calls_lock = threading.Lock()
    started_history = set()
    urgent_completed = set()

    class Source:
        shared = object()

        def get(self, url, params=None):
            symbol = params["symbol"]
            if url == KLINE:
                with calls_lock:
                    started_history.add(symbol)
                assert release.wait(5), "Synthetic background request was not released"
            else:
                with calls_lock:
                    urgent_completed.add(symbol)

        def close(self):
            pass

    hub = MarketDataHub(path, Source(), max_workers=6)
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            hub.poll_nonblocking()
            with calls_lock:
                if started_history == set(scan_names) and len(urgent_completed) >= 8:
                    break
            time.sleep(.01)
        with calls_lock:
            assert started_history == set(scan_names), (
                "Both scanner candle lanes must be admitted without waiting for "
                "the urgent queue to become empty"
            )
            assert len(urgent_completed) >= 8, (
                "Two candle lanes must not block position mark progress"
            )
        assert len(hub._inflight) <= hub.max_workers
    finally:
        release.set()
        hub.close()
