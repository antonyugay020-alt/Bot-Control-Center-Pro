"""Synthetic Hub load: priority positions and 824 background histories.

This tests queue progress only. It does not represent live WebSockets, public
exchange limits, signal timing, or a full 824-symbol trading acceptance.
"""
import threading
import time

from bcc.market_hub import HubRegistry, MarketDataHub

BOOK = 'https://api.bybit.com/v5/market/orderbook'
KLINE = 'https://api.bybit.com/v5/market/kline'


def test_824_background_requests_and_twelve_urgent_subscribers_make_progress(tmp_path):
    seen = set()
    urgent = set()
    lock = threading.Lock()

    class Source:
        shared = object()

        def get(self, url, params=None):
            time.sleep(.015)
            with lock:
                (urgent if url == BOOK else seen).add(params['symbol'])

        def close(self):
            pass

    path = tmp_path / 'isolated-public.sqlite'
    registry = HubRegistry(path)
    scan_keys, urgent_keys = set(), set()
    for i in range(824):
        scan_keys.add(registry.subscribe(KLINE, {
            'category': 'linear', 'symbol': f'SCAN{i}USDT',
            'interval': '1', 'limit': 100,
        }, lifetime=120))
    for i in range(12):
        urgent_keys.add(registry.subscribe(BOOK, {
            'category': 'linear', 'symbol': f'OPEN{i}USDT', 'limit': 50,
        }, lifetime=120, priority=True))
    hub = MarketDataHub(path, Source(), max_workers=6)
    try:
        deadline = time.monotonic() + 90
        completed = set()
        while time.monotonic() < deadline:
            completed.update(key for key, ok in hub.poll_nonblocking() if ok)
            with lock:
                if len(seen) == 824 and len(urgent) == 12 and scan_keys | urgent_keys <= completed:
                    break
            time.sleep(.005)
        with lock:
            assert len(urgent) == 12
            assert len(seen) == 824
            assert scan_keys | urgent_keys <= completed
    finally:
        hub.close()
