"""Cross-component admission checks, with controlled offline sources only."""
import threading
import time

import pytest

from bcc.data_cache import CacheBusy, PublicCache
from bcc.market_hub import HubRegistry, MarketDataHub

URL = 'https://api.bybit.com/v5/market/orderbook'
HOST = 'api.bybit.com'


def test_new_position_gets_reserved_worker_when_scans_are_already_stalled(tmp_path):
    path = tmp_path / 'market_cache.sqlite'
    reg = HubRegistry(path)
    for i in range(20):
        reg.subscribe(URL, {'symbol': f'SCAN{i}'})
    entered = threading.Event()
    unlock = threading.Event()
    done = threading.Event()
    active_regular = set()
    class Producer:
        shared = object()
        def get(self, url, params=None):
            symbol = params['symbol']
            if symbol.startswith('SCAN'):
                active_regular.add(symbol)
                if len(active_regular) >= 5:
                    entered.set()
                unlock.wait(1.5)
            else:
                done.set()
        def close(self):
            pass
    hub = MarketDataHub(path, Producer(), max_workers=6)
    try:
        hub.poll_nonblocking()
        assert entered.wait(.5)
        assert len(active_regular) == 5, 'Reserve one executor slot before urgency appears'
        reg.subscribe(URL, {'symbol': 'OPEN'}, priority=True)
        deadline = time.monotonic() + .5
        while not done.is_set() and time.monotonic() < deadline:
            hub.poll_nonblocking()
            time.sleep(.01)
        assert done.is_set(), 'Urgent mark waited for already-running background scans'
    finally:
        unlock.set()
        hub.close()


def test_regular_bybit_load_cannot_consume_position_mark_budget(tmp_path, monkeypatch):
    monkeypatch.setattr('bcc.data_cache.time.time', lambda: 1000.0)
    cache = PublicCache(tmp_path / 'market_cache.sqlite')
    try:
        regular_count = 0
        for i in range(60):
            try:
                cache.begin(URL, {'symbol': f'SCAN{i}'}, HOST)
                regular_count += 1
            except CacheBusy:
                break
        assert regular_count == 48, 'Reserve twelve of sixty Bybit GETs for position marks'
        for i in range(12):
            assert cache.begin(URL, {'symbol': f'OPEN{i}'}, HOST, priority=True)[1]
        with pytest.raises(CacheBusy, match='бюджет'):
            cache.begin(URL, {'symbol': 'OPEN13'}, HOST, priority=True)
    finally:
        cache.close()


def test_budget_reservation_does_not_weaken_global_hard_cap(tmp_path, monkeypatch):
    monkeypatch.setattr('bcc.data_cache.time.time', lambda: 1000.0)
    cache = PublicCache(tmp_path / 'market_cache.sqlite')
    try:
        for i in range(48):
            assert cache.begin(URL, {'symbol': f'SCAN{i}'}, HOST)[1]
        for i in range(12):
            assert cache.begin(URL, {'symbol': f'OPEN{i}'}, HOST, priority=True)[1]
        with pytest.raises(CacheBusy):
            cache.begin(URL, {'symbol': 'LAST'}, HOST, priority=True)
    finally:
        cache.close()


def test_priority_budget_flows_through_actual_hub_publicdata_and_cache(tmp_path, monkeypatch):
    """Regression for a loaded shared budget, not just a scheduler unit stub."""
    import requests
    from bcc.market import PublicData
    path = tmp_path / 'public.sqlite'
    monkeypatch.setattr('bcc.data_cache.time.time', lambda: 1000.0)
    reg = HubRegistry(path)
    cache = PublicCache(path)
    for i in range(48):
        assert cache.begin(URL, {'symbol': f'PRELOAD{i}'}, HOST)[1]
    requests_seen = []
    class Response:
        status_code = 200
        headers = {}
        def raise_for_status(self):
            pass
        def json(self):
            return {'result': 'book', 'symbol': 'OPEN'}
    def get(self, url, params=None, timeout=None):
        requests_seen.append(params['symbol'])
        return Response()
    monkeypatch.setattr(requests.Session, 'get', get)
    reg.subscribe(URL, {'symbol': 'OPEN'}, priority=True)
    source = PublicData()
    source.shared = PublicCache(path)
    hub = MarketDataHub(path, source, max_workers=6)
    try:
        deadline = time.monotonic() + .8
        while time.monotonic() < deadline:
            outcomes = hub.poll_nonblocking()
            if any(success for _key, success in outcomes):
                break
            time.sleep(.015)
        assert source.shared.read_fresh(URL, {'symbol': 'OPEN'}, HOST)['symbol'] == 'OPEN'
        assert requests_seen == ['OPEN']
        with reg._connect() as db:
            count = db.execute('SELECT count FROM request_budget WHERE host=?', (HOST,)).fetchone()[0]
        assert count == 49
    finally:
        hub.close()
        cache.close()


def test_position_metadata_dependency_has_priority_before_orderbook(tmp_path, monkeypatch):
    from bcc.market import PublicData
    url_meta = 'https://api.bybit.com/v5/market/instruments-info'
    path = tmp_path / 'public.sqlite'
    monkeypatch.setenv('BCC_PUBLIC_CACHE', str(path))
    monkeypatch.setenv('BCC_MARKET_HUB_MODE', 'client')
    client = PublicData()
    registry = HubRegistry(path)
    try:
        for url in (url_meta, URL):
            params = {'category': 'linear', 'symbol': 'BTCUSDT'}
            key, owner, _ = client.shared.begin(url, params, HOST)
            assert owner
            client.shared.finish(key, owner, HOST, {'ok': True}, ttl=60)
            client._thread.position_mark = True
            assert client.get(url, params)['ok']
            client._thread.position_mark = False
            subscription_key = HubRegistry.identity(url, params)[0]
            with registry._connect() as db:
                priority_until = db.execute(
                    'SELECT priority_until FROM hub_subscriptions WHERE k=?',
                    (subscription_key,)).fetchone()[0]
            assert priority_until > time.time(), f'Position prerequisite not marked urgent: {url}'
    finally:
        client.close()
