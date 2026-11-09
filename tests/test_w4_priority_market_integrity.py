"""End-to-end priority and fail-closed checks for W3 live load failure.

No external exchanges or LIVE orders. Read-only market request simulation only.
"""
import sqlite3
import time

import pytest

from bcc.market_hub import HubRegistry
from bcc.market import PublicData
from bcc.scheduler import MarketScheduler
from bcc.market_health import display_market_status

URL = 'https://api.bybit.com/v5/market/orderbook'


def test_priority_migrates_existing_w3_hub_schema(tmp_path):
    path = tmp_path / 'market_cache.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE hub_subscriptions(
            k TEXT PRIMARY KEY, url TEXT NOT NULL, params TEXT NOT NULL,
            active_until REAL NOT NULL, next_due REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '')''')
    reg = HubRegistry(path)
    with reg._connect() as db:
        cols = {x[1] for x in db.execute('PRAGMA table_info(hub_subscriptions)')}
    assert 'priority_until' in cols
    key = reg.subscribe(URL, {'symbol': 'BTCUSDT'}, priority=True)
    with reg._connect() as db:
        urgency, expiry = db.execute('SELECT priority_until,active_until FROM hub_subscriptions WHERE k=?', (key,)).fetchone()
    assert time.time() < urgency < expiry
    reg.subscribe(URL, {'symbol': 'BTCUSDT'}, priority=False)
    with reg._connect() as db:
        assert db.execute('SELECT priority_until FROM hub_subscriptions WHERE k=?', (key,)).fetchone()[0] == urgency
    with pytest.raises(ValueError):
        reg.subscribe(URL, {'symbol': 'ETHUSDT'}, priority=1)


def test_active_positions_precede_scans_but_regular_subscription_not_starved(tmp_path):
    reg = HubRegistry(tmp_path / 'market_cache.sqlite')
    urgent = {reg.subscribe(URL, {'symbol': f'OPEN{i}USDT'}, priority=True) for i in range(12)}
    normal = {reg.subscribe(URL, {'symbol': f'SCAN{i}USDT'}) for i in range(200)}
    due = reg.due(limit=6, reserve_regular=1)
    selected = {row[0] for row in due}
    assert len(selected) == 6
    assert len(selected & urgent) == 5
    assert len(selected & normal) == 1
    assert reg.due(limit=6)[0][0] in urgent
    # A stopped bot cannot permanently monopolize the urgent lane.
    with reg._connect() as db:
        db.execute('UPDATE hub_subscriptions SET priority_until=?', (time.time()-1,))
    assert not ({row[0] for row in reg.due(limit=6, reserve_regular=1)} - (urgent | normal))
    with reg._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_subscriptions WHERE priority_until>?', (time.time(),)).fetchone()[0] == 0


def test_missing_position_marks_pause_wide_scans_and_deliver_book_before_candles():
    calls = []
    class Source:
        def position_orderbook(self, symbol):
            calls.append(('priority_book', symbol))
            return {'at': time.time(), 'bids': [[100, 1]], 'asks': [[101, 1]]}
        def orderbook(self, symbol):
            calls.append(('normal_book', symbol))
            return {'at': time.time(), 'bids': [[100, 1]], 'asks': [[101, 1]]}
        def candles(self, symbol, required):
            calls.append(('candles', symbol))
            return []
        def funding(self, symbol):
            calls.append(('funding', symbol))
            return []

    scheduler = MarketScheduler(Source(),
                                {'symbols': [f'COIN{i}USDT' for i in range(815)]},
                                lambda *_: 50, workers=4, period=2)
    state = {'positions': {'p': {'symbol': 'BTCUSDT', 'opened': 1}},
             'pending': {}, 'funding_archive': {}, 'marks': {}}
    try:
        results = []
        for _ in range(15):
            results.extend(scheduler.collect(state, timeout=.05))
            if results:
                break
        priority_results = [tick for symbol, tick, error in results if symbol == 'BTCUSDT']
        assert priority_results and priority_results[0].book is not None
        assert priority_results[0].bars == []
        assert ('priority_book', 'BTCUSDT') in calls
        assert sum(c[0] == 'normal_book' for c in calls) <= 1
        assert not any(c == ('candles', 'BTCUSDT') or c == ('funding', 'BTCUSDT') for c in calls)
        assert scheduler.last_pressure['configured_symbols'] == 815
        assert scheduler.last_pressure['priority_symbols'] == 1
        assert scheduler.last_pressure['entry_admission_paused'] is True
        assert scheduler.last_pressure['scan_throttled'] is True
    finally:
        scheduler.close()


def test_fresh_positions_keep_indicator_work_without_disabling_scan():
    calls = []
    class Source:
        def position_orderbook(self, symbol):
            calls.append(('book', symbol))
            return {'at': time.time(), 'bids': [[10, 1]], 'asks': [[11, 1]]}
        def orderbook(self, symbol):
            calls.append(('normal', symbol))
            return {'at': time.time(), 'bids': [[10, 1]], 'asks': [[11, 1]]}
        def candles(self, symbol, required):
            calls.append(('bars', symbol))
            return [{'t': 1}]
        def funding(self, symbol):
            calls.append(('funding', symbol))
            return []
    source = Source()
    sched = MarketScheduler(source, {'symbols': ['BTCUSDT', 'ETHUSDT']},
                            lambda *_: 10, workers=2, period=1)
    state = {'positions': {'p': {'symbol': 'BTCUSDT', 'opened': 1}},
             'pending': {}, 'funding_archive': {},
             'marks': {'BTCUSDT': {'at': time.time()}}}
    try:
        got = []
        for _ in range(20):
            got.extend(sched.collect(state, timeout=.05))
            if ({x[0] for x in got} == {'BTCUSDT', 'ETHUSDT'}
                    and any(symbol == 'BTCUSDT' and tick and tick.bars for symbol, tick, error in got)):
                break
        assert {x[0] for x in got} == {'BTCUSDT', 'ETHUSDT'}
        assert calls.index(('book', 'BTCUSDT')) < calls.index(('bars', 'BTCUSDT'))
        assert sched.last_pressure['entry_admission_paused'] is False
    finally:
        sched.close()


def test_actual_public_market_client_upgrades_existing_subscription_priority(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv('BCC_PUBLIC_CACHE', str(tmp_path/'shared.sqlite'))
    monkeypatch.setenv('BCC_MARKET_HUB_MODE', 'client')
    client = PublicData()
    reg = HubRegistry(client.shared.path)
    params = {'symbol': 'BTCUSDT'}
    key, owner, _ = client.shared.begin(URL, params, 'api.bybit.com')
    assert owner
    client.shared.finish(key, owner, 'api.bybit.com', {'ok': 1}, ttl=30)
    # Verify urgency for a real transport GET made under position context, not
    # only an artificially invoked registry call.
    try:
        assert client.get(URL, params) == {'ok': 1}
        subscription_key = HubRegistry.identity(URL, params)[0]
        with reg._connect() as db:
            assert db.execute('SELECT priority_until FROM hub_subscriptions WHERE k=?', (subscription_key,)).fetchone()[0] == 0
        client.orderbook = lambda symbol: client.get(URL, params)
        assert client.position_orderbook('BTCUSDT') == {'ok': 1}
        with reg._connect() as db:
            assert db.execute('SELECT priority_until FROM hub_subscriptions WHERE k=?', (subscription_key,)).fetchone()[0] > time.time()
        assert getattr(client._thread, 'position_mark', False) is False
    finally:
        client.close()


def test_monitor_reports_unmarked_open_positions_not_as_healthy():
    health = {'market_health': {
        'scheduler_pressure': {'configured_symbols': 815, 'unmarked_priority': 12, 'entry_admission_paused': True},
        'market_errors': 0,
    }}
    assert 'нет свежих цен' in display_market_status('Работает', health)
    assert display_market_status('Остановлен', health) == 'Остановлен'


def test_hub_batches_honor_position_mark_priority_at_2500_subscription_scale(tmp_path):
    from bcc.market_hub import MarketDataHub
    path = tmp_path / 'market_cache.sqlite'
    registry = HubRegistry(path)
    urgent = set()
    for i in range(12):
        urgent.add(registry.subscribe(URL, {'symbol': f'OPEN{i}USDT'}, priority=True))
    for i in range(2500):
        registry.subscribe(URL, {'symbol': f'OTHER{i}USDT'})
    calls = []
    class Source:
        shared = object()
        def get(self, url, params=None):
            calls.append(params['symbol'])
        def close(self):
            pass
    hub = MarketDataHub(path, Source(), max_workers=6)
    try:
        assert len(hub.run_once(limit=32)) == 6
        assert sum(c.startswith('OPEN') for c in calls) == 5
        assert sum(c.startswith('OTHER') for c in calls) == 1
        assert len(hub.run_once(limit=32)) == 6
        assert sum(c.startswith('OPEN') for c in calls) >= 10
        assert sum(c.startswith('OTHER') for c in calls) >= 2
        with registry._connect() as db:
            assert db.execute('SELECT COUNT(*) FROM hub_subscriptions').fetchone()[0] == 2512
    finally:
        hub.close()


def test_urgent_mark_only_falls_back_to_fail_closed_if_public_book_is_missing():
    from bcc.market import DataUnavailable
    class Source:
        def position_orderbook(self, symbol):
            raise DataUnavailable('book unavailable')
        def candles(self, symbol, required):
            assert symbol != 'BTCUSDT', 'History cannot hold the failed position quote'
            return []
        def orderbook(self, symbol):
            raise DataUnavailable('scanner book unavailable')
    scheduler = MarketScheduler(Source(), {'symbols': ['BTCUSDT', 'ETHUSDT']}, lambda *_: 30, workers=2)
    state = {'positions': {'p': {'symbol': 'BTCUSDT', 'opened': 1}},
             'pending': {}, 'funding_archive': {}, 'marks': {}}
    try:
        result = []
        for _ in range(10):
            result.extend(scheduler.collect(state, timeout=.05))
            if result:
                break
        position_ticks = [tick for symbol, tick, error in result if symbol == 'BTCUSDT' and error is None]
        assert len(position_ticks) == 1
        assert position_ticks[0].book is None
        assert position_ticks[0].quote_error == 'book unavailable'
        assert scheduler.last_pressure['entry_admission_paused']
    finally:
        scheduler.close()


def test_exposure_exit_history_survives_draining_822_scanned_symbols():
    from bcc.scheduler import MarketScheduler
    calls = []
    class Source:
        def position_orderbook(self, symbol):
            calls.append(('book', symbol))
            return dict(at=time.time(), bids=[[10, 10]], asks=[[11, 10]])
        def position_candles(self, symbol, required):
            calls.append(('history', symbol))
            return [{'t': int(time.time()*1000), 'symbol': symbol}]
        def candles(self, symbol, required):
            calls.append(('scanner', symbol))
            return []
        def orderbook(self, symbol):
            calls.append(('scanner_book', symbol))
            return dict(at=time.time(), bids=[[10, 10]], asks=[[11, 10]])
        def funding(self, symbol):
            return []
    state = dict(positions={f'p{i}':dict(symbol=f'OPEN{i}USDT', opened=1)
                            for i in range(8)},
                 pending={}, funding_archive={}, marks={}, strategy=dict(draining=True))
    scheduler = MarketScheduler(Source(), {'symbols':[f'SCAN{i}USDT' for i in range(822)]},
                                lambda *_:100, workers=8, period=.05)
    try:
        served = set()
        for _ in range(100):
            for symbol, tick, error in scheduler.collect(state, timeout=.02):
                if error is None and tick.book is not None and tick.bars:
                    served.add(symbol)
            if len(served) == 8:
                break
        assert served == {f'OPEN{i}USDT' for i in range(8)}
        assert {s for kind, s in calls if kind=='history'} == served
        assert all(s.startswith('OPEN') for kind, s in calls)
        assert scheduler.last_pressure['scan_paused']
    finally:
        scheduler.close()


def test_position_candles_are_fenced_by_shared_hub_priority(tmp_path, monkeypatch):
    monkeypatch.setenv('BCC_PUBLIC_CACHE', str(tmp_path / 'market.sqlite'))
    monkeypatch.setenv('BCC_MARKET_HUB_MODE', 'client')
    client = PublicData()
    url = 'https://api.bybit.com/v5/market/kline'
    params = dict(category='linear', symbol='BTCUSDT', interval='1', limit=100)
    key, owner, _ = client.shared.begin(url, params, 'api.bybit.com')
    client.shared.finish(key, owner, 'api.bybit.com', {'result':{'list':[]}}, ttl=20)
    client.candles = lambda symbol, required: client.get(url, params)
    reg = HubRegistry(client.shared.path)
    try:
        client.get(url, params)
        subkey = HubRegistry.identity(url, params)[0]
        with reg._connect() as db:
            assert db.execute('SELECT priority_until FROM hub_subscriptions WHERE k=?',
                              (subkey,)).fetchone()[0] == 0
        client.position_candles('BTCUSDT', 100)
        with reg._connect() as db:
            assert db.execute('SELECT priority_until FROM hub_subscriptions WHERE k=?',
                              (subkey,)).fetchone()[0] > time.time()
        assert not getattr(client._thread, 'position_mark', False)
    finally:
        client.close()
