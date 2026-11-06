import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from bcc.market_hub import HubRegistry, MarketDataHub
from bcc.market import PublicData, DataUnavailable
from bcc.data_cache import PublicCache

URL = 'https://api.bybit.com/v5/market/orderbook'


def test_hub_fanout_eight_clients_one_upstream(tmp_path, monkeypatch):
    path = tmp_path / 'hub.sqlite'
    monkeypatch.setenv('BCC_MARKET_HUB_MODE', 'client')
    monkeypatch.setenv('BCC_PUBLIC_CACHE', str(path))
    calls = []
    class Producer:
        def __init__(self):
            self.shared = PublicCache(path)
        def get(self, url, params=None):
            calls.append((url, params))
            key, owner, cached = self.shared.begin(url, params, 'api.bybit.com')
            if owner:
                self.shared.finish(key, owner, 'api.bybit.com', {'ok': 1}, ttl=10)
        def close(self):
            self.shared.close()
    hub = MarketDataHub(path, Producer())
    clients = [PublicData() for _ in range(8)]
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            pending = [pool.submit(client.get, URL, {'symbol': 'BTCUSDT'}) for client in clients]
            for _ in range(30):
                if hub.registry.due():
                    break
                threading.Event().wait(.02)
            assert len(hub.run_once()) == 1
            assert [job.result(timeout=3) for job in pending] == [{'ok': 1}] * 8
            assert len(calls) == 1
    finally:
        for client in clients: client.close()
        hub.close()


def test_hub_client_no_http_fallback_on_missing_producer(tmp_path, monkeypatch):
    monkeypatch.setenv('BCC_MARKET_HUB_MODE', 'client')
    monkeypatch.setenv('BCC_PUBLIC_CACHE', str(tmp_path / 'hub.sqlite'))
    client = PublicData()
    class Forbidden:
        def get(self, *args, **kwargs):
            raise AssertionError('Unexpected worker HTTP')
    client._thread.session = Forbidden()
    try:
        with pytest.raises(DataUnavailable, match='Market Data Hub'):
            client.get(URL, {'symbol': 'SOLUSDT'})
    finally:
        client.close()


def test_hub_rejects_private_url(tmp_path):
    registry = HubRegistry(tmp_path / 'hub.sqlite')
    with pytest.raises(ValueError):
        registry.subscribe('http://localhost/secret', {})
