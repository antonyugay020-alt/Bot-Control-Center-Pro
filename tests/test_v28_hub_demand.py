"""The public subscription boundary must never crash a PAPER worker on SQLite."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from bcc import market_hub
from bcc.market import PublicData, DataUnavailable

URL = 'https://api.bybit.com/v5/market/orderbook'
PARAMS = dict(category='linear', symbol='BTCUSDT', limit=50)


@pytest.mark.parametrize('failure', ['init', 'subscribe'])
def test_client_registry_failure_is_typed_and_next_request_recovers(tmp_path, monkeypatch, failure):
    monkeypatch.setenv('BCC_MARKET_HUB_MODE', 'client')
    monkeypatch.setenv('BCC_BYBIT_WS_BOOKS', '1')
    source = PublicData()
    calls = []
    class Registry:
        identity = staticmethod(market_hub.HubRegistry.identity)
        def __init__(self, path):
            if failure == 'init' and not calls:
                calls.append('failed init')
                raise sqlite3.OperationalError('database is locked (writer admission)')
        def subscribe(self, *args, **kwargs):
            if failure == 'subscribe' and not calls:
                calls.append('failed subscribe')
                raise sqlite3.OperationalError('database is locked (writer admission)')
            calls.append('committed')
    source.shared = SimpleNamespace(path=tmp_path/'cache.sqlite',
        read_fresh=lambda *args: {'_bcc_ws_book': True}, close=lambda:None)
    monkeypatch.setattr(market_hub, 'HubRegistry', Registry)
    source._thread.session = SimpleNamespace(get=lambda *a,**kw:pytest.fail('worker HTTP'))
    try:
        with pytest.raises(DataUnavailable) as e:
            source.get(URL, PARAMS)
        assert isinstance(e.value.__cause__, sqlite3.OperationalError)
        assert not source._hub_renew_at and not source._hub_priority_until
        assert source.get(URL, PARAMS) == {'_bcc_ws_book': True}
        assert source._hub_renew_at and calls[-1]=='committed'
    finally:
        source.close()


def test_public_demand_does_not_hold_quote_cache_lock(tmp_path):
    source = PublicData()
    entered, release = threading.Event(), threading.Event()
    class Registry:
        def subscribe(self, *a, **kw):
            entered.set()
            assert release.wait(5)
    source._hub_registry = Registry()
    def read_cache():
        with source._lock:
            return True
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=pool.submit(source._renew_hub_demand, None, 'key', URL, PARAMS,
                            lifetime=60, renewal=20, priority=True, priority_renewal=5)
        try:
            assert entered.wait(2)
            assert pool.submit(read_cache).result(2)
            assert not pending.done()
        finally:
            release.set()
        pending.result(2)
    assert source._hub_renew_at['key'] > source._hub_priority_until['key']
    source.close()
