"""Untested integration boundaries: producer scheduling, lease fencing and throughput.

Offline deterministic sources replace exchange HTTP; never treat as Windows/live verification.
"""
import threading
import time
from collections import Counter

from bcc.market_hub import HubRegistry, MarketDataHub

URL = 'https://api.bybit.com/v5/market/orderbook'


def _pump(hub, until, interval=.015):
    results = []
    deadline = time.monotonic() + until
    while time.monotonic() < deadline:
        results.extend(hub.poll_nonblocking())
        time.sleep(interval)
    return results


def test_slow_regular_request_cannot_hold_urgent_position_mark_batch(tmp_path):
    path = tmp_path / 'cache.sqlite'
    registry = HubRegistry(path)
    for i in range(8):
        registry.subscribe(URL, {'symbol': f'OPEN{i}'}, priority=True)
    registry.subscribe(URL, {'symbol': 'SLOW_SCAN'})
    started = threading.Event()
    completed = threading.Event()
    calls = []
    class Source:
        shared = object()
        def get(self, url, params=None):
            symbol = params['symbol']
            calls.append(symbol)
            if symbol == 'SLOW_SCAN':
                started.set()
                time.sleep(.9)
                completed.set()
            else:
                time.sleep(.02)
        def close(self):
            pass
    hub = MarketDataHub(path, Source(), max_workers=6)
    try:
        hub.poll_nonblocking()
        assert started.wait(.3)
        observed = _pump(hub, .42)
        assert not completed.is_set(), 'Slow background source should still be running'
        # At least six urgent completions (five in first batch, another one
        # using freed capacity), even while the background job is blocked.
        urgent = {registry.identity(URL, {'symbol': f'OPEN{i}'})[0] for i in range(8)}
        assert len({key for key, ok in observed if key in urgent and ok}) >= 6
        assert calls.count('SLOW_SCAN') == 1, 'Do not duplicate in-flight request'
    finally:
        hub.close()
    assert completed.is_set()


def test_due_exclusion_does_not_hide_fresh_work_behind_inflight_rows(tmp_path):
    path = tmp_path / 'cache.sqlite'
    reg = HubRegistry(path)
    keys = [reg.subscribe(URL, {'symbol': f'SYM{i}'}) for i in range(160)]
    selected = reg.due(limit=6, exclude=keys[:6])
    assert len(selected) == 6
    assert not {row[0] for row in selected} & set(keys[:6])
    assert all(row[0] in keys[6:] for row in selected)


def test_background_capacity_survives_continuous_urgent_renewals(tmp_path):
    path = tmp_path / 'cache.sqlite'
    reg = HubRegistry(path)
    for i in range(24):
        reg.subscribe(URL, {'symbol': f'URGENT{i}'}, priority=True)
    for i in range(8):
        reg.subscribe(URL, {'symbol': f'REGULAR{i}'})
    calls = []
    class Source:
        shared = object()
        def get(self, url, params=None):
            calls.append(params['symbol'])
            time.sleep(.005)
        def close(self):
            pass
    hub = MarketDataHub(path, Source(), max_workers=6)
    try:
        _pump(hub, .35, interval=.01)
        assert any(sym.startswith('REGULAR') for sym in calls)
        assert any(sym.startswith('URGENT') for sym in calls)
        assert len(hub._inflight) <= 6
    finally:
        hub.close()


def test_stale_leader_cannot_acknowledge_after_lease_transfer(tmp_path):
    path = tmp_path / 'cache.sqlite'
    reg = HubRegistry(path)
    key = reg.subscribe(URL, {'symbol': 'BTCUSDT'})
    started = threading.Event()
    release = threading.Event()
    class Source:
        shared = object()
        def get(self, url, params=None):
            started.set()
            release.wait(1)
        def close(self):
            pass
    hub = MarketDataHub(path, Source(), max_workers=2)
    try:
        hub.poll_nonblocking()
        assert started.wait(.5)
        # Simulate a verified handover to a new leader after a long stall.
        with reg._connect() as db:
            db.execute('UPDATE hub_leader SET owner=?, expires=? WHERE singleton=1',
                       ('replacement', time.time()+120))
        release.set()
        _pump(hub, .15)
        with reg._connect() as db:
            due = db.execute('SELECT next_due FROM hub_subscriptions WHERE k=?', (key,)).fetchone()[0]
        assert due == 0, 'Old owner must not acknowledge work after handover'
    finally:
        release.set()
        hub.close()


def test_actual_hub_client_receives_urgent_snapshot_during_slow_background_get(tmp_path, monkeypatch):
    """Hub registry -> producer -> PublicCache -> real PublicData consumer.

    Offline fabricated public response, *not* a live Bybit quote. Ensure
    latency separation across real SQLite producer/consumer instances.
    """
    from bcc.data_cache import PublicCache
    from bcc.market import PublicData
    path = tmp_path / 'cache.sqlite'
    monkeypatch.setenv('BCC_PUBLIC_CACHE', str(path))
    monkeypatch.setenv('BCC_MARKET_HUB_MODE', 'client')
    URL_META = 'https://api.bybit.com/v5/market/instruments-info'
    client = PublicData()
    producer_cache = PublicCache(path)
    slow_started = threading.Event()
    slow_finished = threading.Event()
    class Source:
        shared = producer_cache
        def get(self, url, params=None):
            if url == URL_META:
                slow_started.set()
                time.sleep(.8)
                slow_finished.set()
            else:
                time.sleep(.018)
            key, owner, cached = producer_cache.begin(url, params, 'api.bybit.com')
            if owner:
                producer_cache.finish(key, owner, 'api.bybit.com',
                                      {'book_at': time.time(), 'symbol': params['symbol']}, ttl=1)
            return cached
        def close(self):
            producer_cache.close()
    registry = HubRegistry(path)
    registry.subscribe(URL_META, {'symbol': 'SLOW_SCAN'})
    hub = MarketDataHub(path, Source(), max_workers=6)
    errors = []
    values = []
    def consume():
        try:
            # Mark client context explicitly; the same mechanism used by
            # PublicData.position_orderbook marks its orderbook GET.
            client._thread.position_mark = True
            values.append(client.get(URL, {'symbol': 'OPEN1'}))
        except Exception as exc:
            errors.append(exc)
    consumer = threading.Thread(target=consume)
    try:
        hub.poll_nonblocking()
        assert slow_started.wait(.4)
        consumer.start()
        started = time.monotonic()
        while consumer.is_alive() and time.monotonic() - started < .65:
            hub.poll_nonblocking()
            time.sleep(.015)
        consumer.join(timeout=.1)
        assert not errors
        assert values and values[0]['symbol'] == 'OPEN1'
        assert not slow_finished.is_set()
    finally:
        client.close()
        hub.close()
        consumer.join(timeout=1)
