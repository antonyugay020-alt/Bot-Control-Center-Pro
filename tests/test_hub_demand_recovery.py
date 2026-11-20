"""Bounded Hub demand and source-time priority under a large VELES fleet.

No network or live account is accessed. Scanner demand must not retain
long-lived WS topics, while existing-position mark requests remain urgent.
"""
from types import SimpleNamespace
from pathlib import Path

import pytest

from bcc.market import PublicData
from bcc.market_hub import HubRegistry
from bcc.scheduler import MarketScheduler
from bcc.ws_quotes import BybitQuoteStream


TICKER = "https://api.bybit.com/v5/market/tickers"
BOOK = "https://api.bybit.com/v5/market/orderbook"
KLINE = "https://api.bybit.com/v5/market/kline"
RISK = "https://api.bybit.com/v5/market/risk-limit"


class SharedView:
    def __init__(self, path):
        self.path = Path(path)

    def read_fresh(self, url, params, host):
        # Synthetic cache only. Real producer policies are tested separately.
        return dict(retCode=0, _bcc_ws_quote=True, _bcc_ws_mark=True,
                    _bcc_ws_book=True, _bcc_ws_kline=True)

    def close(self):
        pass


class RecordingRegistry:
    def __init__(self):
        self.calls = []

    def subscribe(self, url, params, *, lifetime, priority):
        self.calls.append((url, params, lifetime, priority))
        return HubRegistry.identity(url, params)[0]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
    monkeypatch.setenv("BCC_BYBIT_WS_QUOTES", "1")
    monkeypatch.setenv("BCC_BYBIT_WS_BOOKS", "1")
    monkeypatch.setenv("BCC_BYBIT_WS_KLINES", "1")
    source = PublicData()
    source.shared = SharedView(tmp_path / "public_cache.sqlite")
    registry = RecordingRegistry()
    source._hub_registry = registry
    yield source, registry
    source.close()


@pytest.mark.parametrize("route,params", [
    (TICKER, {"category": "linear", "symbol": "AAAUSDT"}),
    (BOOK, {"category": "linear", "symbol": "AAAUSDT", "limit": 50}),
    (KLINE, {"category": "linear", "symbol": "AAAUSDT", "interval": "1", "limit": 10}),
])
def test_scanner_ws_demand_has_short_renewable_lease(client, route, params):
    source, registry = client
    assert source.get(route, params)["retCode"] == 0
    assert registry.calls[-1] == (route, params, 12, False)
    key = HubRegistry.identity(route, params)[0]
    assert source._hub_renew_at[key] > 0


@pytest.mark.parametrize("route,params", [
    (TICKER, {"category": "linear", "symbol": "OPENUSDT"}),
    (BOOK, {"category": "linear", "symbol": "OPENUSDT", "limit": 50}),
    (RISK, {"category": "linear", "symbol": "OPENUSDT"}),
])
def test_open_position_retains_long_urgent_lease(client, route, params):
    source, registry = client
    source._thread.position_mark = True
    assert source.get(route, params)["retCode"] == 0
    assert registry.calls[-1] == (route, params, 60, True)


@pytest.mark.parametrize('ws_enabled', [True, False])
@pytest.mark.parametrize('limit', [10, 100, 150, 1000])
def test_historical_rest_demand_is_short_and_renewable(client, monkeypatch, ws_enabled, limit):
    source, registry = client
    monkeypatch.setenv('BCC_BYBIT_WS_KLINES', '1' if ws_enabled else '0')
    params = {"category": "linear", "symbol": "COLDUSDT", "limit": limit, "interval": "1"}
    assert source.get(KLINE, params)["retCode"] == 0
    assert registry.calls[-1] == (KLINE, params, 12, False)
    key = HubRegistry.identity(KLINE, params)[0]
    source._hub_renew_at[key] = 0
    assert source.get(KLINE, params)["retCode"] == 0
    assert registry.calls[-1] == (KLINE, params, 12, False)


def test_risk_rest_demand_remains_long(client):
    source, registry = client
    assert source.get(RISK, {"category": "linear", "symbol": "COLDUSDT"})["retCode"] == 0
    assert registry.calls[-1][2:] == (60, False)


def test_824_completed_backfills_stop_polling_and_live_demand_renews(client, tmp_path, monkeypatch):
    import time
    source, _ = client
    registry = HubRegistry(tmp_path / 'leases.sqlite')
    source._hub_registry = registry
    monkeypatch.setenv('BCC_BYBIT_WS_KLINES', '0')
    wall = [1_795_000_000.]
    mono = [1000.]
    monkeypatch.setattr(time, 'time', lambda: wall[0])
    monkeypatch.setattr(time, 'monotonic', lambda: mono[0])
    def params(index):
        return {'category': 'linear', 'symbol': f'COIN{index}USDT', 'interval': '1', 'limit': 150}
    for index in range(824):
        assert source.get(KLINE, params(index))['retCode'] == 0
    with registry._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_subscriptions WHERE active_until>?', (wall[0],)).fetchone()[0] == 824
    wall[0] += 13
    mono[0] += 13
    assert registry.due(limit=32) == []
    assert source.get(KLINE, params(7))['retCode'] == 0
    due = registry.due(limit=32)
    assert len(due) == 1 and due[0][2]['symbol'] == 'COIN7USDT'


def test_missing_and_old_risk_evidence_renew_before_recent():
    positions = ["NEWUSDT", "OLDESTUSDT", "NANUSDT", "MISSINGUSDT"]
    state = {"margin_data": {
        "NEWUSDT": {"risk_at": 200.},
        "OLDESTUSDT": {"risk_at": 100.},
        "NANUSDT": {"risk_at": float("nan")},
    }}
    ordered = MarketScheduler._oldest_tier_first(positions, state)
    assert ordered == ["NANUSDT", "MISSINGUSDT", "OLDESTUSDT", "NEWUSDT"]


def test_priority_marks_bypass_background_fleet_cap():
    wall = [1763000000.]
    mono = [100.]
    class Socket:
        def __init__(self):
            self.sent = []
        def send(self, payload):
            import json
            self.sent.append(json.loads(payload))
        def close(self):
            pass
    socket = Socket()
    stream = BybitQuoteStream(wall=lambda: wall[0], monotonic=lambda: mono[0])
    priority = [f"OPEN{i}USDT" for i in range(25)]
    scans = [f"SCAN{i}USDT" for i in range(120)]
    stream.subscribe_many(priority + scans)
    stream.set_priority_symbols(priority)
    stream.connect(socket)
    mono[0] += 3
    wall[0] += 3
    assert stream.refresh_stale_marks(priority + scans) == 45
    topics = [topic for op in socket.sent if op["op"] == "subscribe"
              for topic in op["args"]]
    assert all("tickers." + sym in topics for sym in priority)
    assert sum("tickers." + sym in topics for sym in scans) == 20
    assert stream.refresh_stale_marks(scans) == 20
