"""Architecture-level checks reproducing large subscriber backlog and lock contention."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from bcc.data_cache import CacheBusy, PublicCache
from bcc.market import DataUnavailable, PublicData
from bcc.market_health import display_market_status
from bcc.market_hub import HubRegistry, MarketDataHub
from bcc.storage import Repository
from bcc.support import export_current_report

URL = 'https://api.bybit.com/v5/market/orderbook'


def test_bounded_parallel_hub_producer_uses_multiple_upstream_workers(tmp_path):
    path = tmp_path / 'market_cache.sqlite'
    registry = HubRegistry(path)
    for i in range(12):
        registry.subscribe(URL, {'symbol': f'TEST{i}USDT'})
    lock = threading.Lock()
    observed = {'active': 0, 'peak': 0, 'calls': 0}

    class SlowProducer:
        shared = object()
        def get(self, url, params=None):
            with lock:
                observed['active'] += 1
                observed['calls'] += 1
                observed['peak'] = max(observed['active'], observed['peak'])
            time.sleep(.055)
            with lock:
                observed['active'] -= 1
        def close(self):
            pass

    hub = MarketDataHub(path, SlowProducer(), max_workers=6)
    try:
        first = hub.run_once(limit=32)
        second = hub.run_once(limit=32)
        assert len(first) == len(second) == 6
        assert all(ok for _, ok in first + second)
        assert observed['calls'] == 12
        assert 2 <= observed['peak'] <= 6
        with registry._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM hub_subscriptions WHERE next_due>0").fetchone()[0] == 12
    finally:
        hub.close()


def test_concurrent_producer_does_not_ack_after_leader_is_revoked(tmp_path):
    path = tmp_path / 'market_cache.sqlite'
    registry = HubRegistry(path)
    for i in range(4):
        registry.subscribe(URL, {'symbol': f'TEST{i}USDT'})
    gate = threading.Event()
    entered = threading.Event()

    class PausedSource:
        shared = object()
        def get(self, url, params=None):
            entered.set()
            assert gate.wait(4)
        def close(self):
            pass

    hub = MarketDataHub(path, PausedSource(), max_workers=4)
    results = []
    try:
        worker = threading.Thread(target=lambda: results.extend(hub.run_once(limit=4)))
        worker.start()
        assert entered.wait(3)
        registry.release_leader(hub.owner, token=registry.leader_token(hub.owner))
        assert registry.claim_leader('new-owner', lifetime=120)
        gate.set()
        worker.join(5)
        assert not worker.is_alive()
        assert results == []
        with registry._connect() as db:
            assert db.execute('SELECT COUNT(*) FROM hub_subscriptions WHERE next_due>0').fetchone()[0] == 0
    finally:
        gate.set()
        hub.close()


def test_client_reuses_hub_registry_and_throttles_subscription_writes(tmp_path, monkeypatch):
    path = tmp_path / 'market.sqlite'
    monkeypatch.setenv('BCC_PUBLIC_CACHE', str(path))
    monkeypatch.setenv('BCC_MARKET_HUB_MODE', 'client')
    client = PublicData()
    reg = HubRegistry(path)
    key, owner, _ = client.shared.begin(URL, {'symbol': 'BTCUSDT'}, 'api.bybit.com')
    assert owner
    client.shared.finish(key, owner, 'api.bybit.com', {'ok': 1}, ttl=60)
    calls = []
    original = HubRegistry.subscribe
    def counted(self, *a, **kw):
        calls.append(a)
        return original(self, *a, **kw)
    monkeypatch.setattr(HubRegistry, 'subscribe', counted)
    try:
        for _ in range(10):
            assert client.get(URL, {'symbol': 'BTCUSDT'}) == {'ok': 1}
        assert len(calls) == 1
        assert client._hub_registry is not None
        with reg._connect() as db:
            assert db.execute('SELECT COUNT(*) FROM hub_subscriptions').fetchone()[0] == 1
    finally:
        client.close()


def test_client_retries_sqlite_busy_but_never_bypasses_hub(tmp_path, monkeypatch):
    path = tmp_path / 'market.sqlite'
    monkeypatch.setenv('BCC_PUBLIC_CACHE', str(path))
    monkeypatch.setenv('BCC_MARKET_HUB_MODE', 'client')
    client = PublicData()
    key, owner, _ = client.shared.begin(URL, {'symbol': 'BTCUSDT'}, 'api.bybit.com')
    client.shared.finish(key, owner, 'api.bybit.com', {'ok': 1}, ttl=30)
    initial = client.shared.read_fresh
    calls = {'n': 0}
    def one_busy(*args):
        calls['n'] += 1
        if calls['n'] == 1:
            raise CacheBusy('Общий кэш публичных данных недоступен: database is locked')
        return initial(*args)
    monkeypatch.setattr(client.shared, 'read_fresh', one_busy)
    client._thread.session = type('ForbiddenHTTP', (), {'get': lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('worker HTTP'))})()
    try:
        assert client.get(URL, {'symbol': 'BTCUSDT'}) == {'ok': 1}
        assert calls['n'] >= 2
    finally:
        client.close()


def test_worker_status_does_not_misrepresent_market_unavailable_as_healthy():
    assert display_market_status('Работает', {'market_health': {'market_errors': 3, 'last_completed_utc': None}}, now=100) == 'Работает · нет свежих рыночных данных'
    assert display_market_status('Работает', {'market_health': {'market_errors': 3, 'last_completed_utc': 99}}, now=100) == 'Работает · ошибки рыночных данных'
    assert display_market_status('Работает', {'market_health': {'market_errors': 0}}, now=100) == 'Работает'
    assert display_market_status('Остановлен', {'market_health': {'market_errors': 100}}, now=100) == 'Остановлен'


def test_report_includes_queue_backlog_not_private_market_payload(tmp_path):
    from bcc.config import defaults
    root = tmp_path / 'bots'
    repo = Repository(root)
    repo.create({**defaults('VELES'), 'name': 'diagnostic-test'})
    reg = HubRegistry(root / 'market_cache.sqlite')
    for i in range(30):
        reg.subscribe(URL, {'symbol': f'TEST{i}USDT'})
    report = tmp_path / 'report.zip'
    export_current_report(root, report)
    import zipfile
    with zipfile.ZipFile(report) as z:
        diagnostics = json.loads(z.read('current_bots_report/diagnostics/market_cache_diagnostics.json'))
        assert diagnostics['subscriptions_overdue'] == 30
        assert diagnostics['subscriptions_active'] == 30
        assert diagnostics['oldest_overdue_seconds'] >= 0
        assert 'TEST1USDT' not in json.dumps(diagnostics)


def test_invalid_concurrency_is_rejected(tmp_path):
    for value in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            MarketDataHub(tmp_path / 'market_cache.sqlite', max_workers=value)


def test_hub_publishes_successful_fetch_after_transient_cache_writer_lock(tmp_path, monkeypatch):
    import requests
    monkeypatch.setenv('BCC_PUBLIC_CACHE', str(tmp_path / 'public.sqlite'))
    monkeypatch.delenv('BCC_MARKET_HUB_MODE', raising=False)
    public = PublicData()
    failures = {'left': 2, 'attempts': 0}
    original = public.shared.finish

    def busy_twice(*args, **kw):
        failures['attempts'] += 1
        if failures['left']:
            failures['left'] -= 1
            raise CacheBusy('database is locked')
        return original(*args, **kw)

    monkeypatch.setattr(public.shared, 'finish', busy_twice)
    class FakeResponse:
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def json(self): return {'ok': True}
    class FakeSession:
        def get(self, *args, **kw): return FakeResponse()
        def close(self): pass
    public._thread.session = FakeSession()
    try:
        assert public.get(URL, {'symbol': 'BTCUSDT'}) == {'ok': True}
        assert failures['attempts'] == 3
        assert public.shared.read_fresh(URL, {'symbol': 'BTCUSDT'}, 'api.bybit.com') == {'ok': True}
    finally:
        public.close()
