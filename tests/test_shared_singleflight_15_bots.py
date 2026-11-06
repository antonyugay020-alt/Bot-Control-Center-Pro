"""Regression: 15 VELES workers reading identical public data must share one fetch."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from bcc.market import PublicData, DataUnavailable
from bcc.data_cache import CacheBusy


class LeaseCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.owner = False
        self.value = None
        self.acquisitions = 0

    def begin(self, url, params, host, *, priority=False):
        assert priority is False, 'Ordinary candle requests must not claim position budget'
        with self.lock:
            if self.value is not None:
                return "shared-key", None, self.value
            if self.owner:
                raise CacheBusy("Данные уже запрашиваются другим ботом")
            self.owner = True
            self.acquisitions += 1
            return "shared-key", "owner", None

    def finish(self, key, owner, host, value=None, ttl=0, cooldown=0):
        with self.lock:
            assert owner == "owner"
            self.value = value
            self.owner = False

    def close(self):
        pass


class FakeResponse:
    status_code = 200
    headers = {}
    def raise_for_status(self):
        pass
    def json(self):
        return {"result": "validated public fixture"}


def test_fifteen_concurrent_clients_reuse_singleflight_data(monkeypatch):
    cache = LeaseCache()
    ready = threading.Barrier(15)
    calls = []

    class FakeSession:
        def get(self, url, params=None, timeout=None):
            calls.append((url, params))
            time.sleep(.18)
            return FakeResponse()
        def close(self):
            pass

    def worker(_):
        source = PublicData()
        source.shared = cache
        monkeypatch.setattr(source, 'session', FakeSession()) if False else None
        # PublicData.session is a read-only property; set thread-local session.
        source._thread.session = FakeSession()
        ready.wait(timeout=5)
        try:
            return source.get('https://api.bybit.com/v5/market/kline', {'symbol': 'BTCUSDT'})
        finally:
            source.close()

    with ThreadPoolExecutor(max_workers=15) as pool:
        results = list(pool.map(worker, range(15)))
    assert all(x == {"result": "validated public fixture"} for x in results)
    assert len(calls) == 1
    assert cache.acquisitions == 1


def test_other_cache_denials_remain_fail_closed():
    source = PublicData()
    class Deny:
        def begin(self, *args, **kwargs):
            assert kwargs.get('priority') is False
            raise CacheBusy("Общий бюджет публичных запросов исчерпан; ожидание")
    source.shared = Deny()
    with pytest.raises(DataUnavailable, match='Общий бюджет'):
        source.get('https://api.bybit.com/v5/market/kline', {'symbol': 'BTCUSDT'})


def test_sqlite_cache_fifteen_clients_share_public_response(tmp_path):
    """Use the production SQLite lease, not a stub, for one burst of workers."""
    from bcc.data_cache import PublicCache

    path = tmp_path / 'market_cache.sqlite'
    count = 0
    count_lock = threading.Lock()
    barrier = threading.Barrier(15)

    class SlowSession:
        def get(self, url, params=None, timeout=None):
            nonlocal count
            with count_lock:
                count += 1
            time.sleep(.2)
            return FakeResponse()
        def close(self):
            pass

    def run(_):
        source = PublicData()
        source.shared = PublicCache(path)
        source._thread.session = SlowSession()
        barrier.wait(timeout=8)
        try:
            return source.get('https://api.bybit.com/v5/market/kline', {'symbol': 'BTCUSDT'})
        finally:
            source.close()

    with ThreadPoolExecutor(max_workers=15) as executor:
        results = list(executor.map(run, range(15)))
    assert count == 1
    assert all(value == {"result": "validated public fixture"} for value in results)
