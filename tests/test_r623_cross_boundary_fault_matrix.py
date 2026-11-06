"""Cross-component fault matrix: every case uses isolated temporary data.

No test may touch a user's working MY_BOTS or call a live market endpoint.
"""
import json
import sqlite3

import pytest

from bcc.market_hub import HubRegistry
from bcc.storage import AccountingError, Store
from bcc.lifecycle import Lifecycle
from bcc.storage import Repository
from bcc.market import PublicData
from test_core import config


@pytest.mark.parametrize('raw', ['{', 'NaN', 'null', '42', '{}', '[{"close":NaN}]'])
@pytest.mark.parametrize('reader', ['read', 'audit', 'chart', 'transaction'])
def test_invalid_persisted_chart_fails_closed_without_data_loss(tmp_path, raw, reader):
    store = Store(tmp_path, 100)
    with store.transaction() as (state, events):
        state['charts']['BTCUSDT'] = [{'close': 100.0}]
    with sqlite3.connect(store.path) as conn:
        conn.execute('UPDATE charts SET document=? WHERE symbol=?', (raw, 'BTCUSDT'))
    with pytest.raises(AccountingError):
        if reader == 'read':
            store.read()
        elif reader == 'audit':
            store.audit()
        elif reader == 'chart':
            store.chart('BTCUSDT')
        else:
            with store.transaction():
                pass
    assert Store(tmp_path).read(include_charts=False)['cash'] == 100
    with sqlite3.connect(store.path) as conn:
        assert conn.execute('SELECT document FROM charts').fetchone()[0] == raw


@pytest.mark.parametrize('raw', ['{', 'null', '[]', '5', 'NaN'])
def test_corrupt_optional_process_diagnostics_never_crash_monitor(tmp_path, raw):
    repo = Repository(tmp_path)
    folder = repo.create(config())
    path = folder / 'process.json'
    path.write_text(raw, encoding='utf-8')
    life = Lifecycle(repo)
    message = life.last_error(folder)
    assert 'process.json' in message
    assert path.read_text(encoding='utf-8') == raw


@pytest.mark.parametrize('raw', ['{', 'NaN', 'null', '[]', '{"symbol":NaN}'])
def test_corrupt_market_subscription_does_not_kill_other_public_clients(tmp_path, raw):
    db = tmp_path / 'public_hub.sqlite'
    registry = HubRegistry(db)
    url = 'https://api.bybit.com/v5/market/orderbook'
    bad_key = registry.subscribe(url, {'symbol': 'XRPUSDT'})
    good_key = registry.subscribe(url, {'symbol': 'BTCUSDT'})
    with registry._connect() as conn:
        conn.execute('UPDATE hub_subscriptions SET params=? WHERE k=?', (raw, bad_key))
    due = registry.due()
    assert len(due) == 1 and due[0][0] == good_key
    with registry._connect() as conn:
        record = conn.execute('SELECT params,last_error FROM hub_subscriptions WHERE k=?', (bad_key,)).fetchone()
        assert record[0] == raw
        assert 'invalid' in record[1].lower()


@pytest.mark.parametrize('url', [
    'https://private:secret@api.bybit.com/v5/market/orderbook',
    'https://api.bybit.com/v5/market/orderbook#token=secret',
])
def test_public_http_and_registry_reject_embedded_credentials(tmp_path, url):
    registry = HubRegistry(tmp_path / 'public_hub.sqlite')
    with pytest.raises(ValueError):
        registry.subscribe(url, {'symbol': 'BTCUSDT'})
    data = PublicData()
    try:
        with pytest.raises(ValueError):
            data.get(url, {'symbol': 'BTCUSDT'})
    finally:
        data.close()


def test_valid_public_client_repairs_only_damaged_subscription(tmp_path):
    registry = HubRegistry(tmp_path / 'hub.sqlite')
    url = 'https://api.bybit.com/v5/market/orderbook'
    params = {'symbol': 'ETHUSDT'}
    key = registry.subscribe(url, params)
    with registry._connect() as conn:
        conn.execute('UPDATE hub_subscriptions SET params=?,last_error=?,next_due=? WHERE k=?',
                     ('{broken', 'Invalid persisted subscription params', 12345678900, key))
    assert registry.due() == []
    assert registry.subscribe(url, params) == key
    with registry._connect() as conn:
        raw, error, due = conn.execute('SELECT params,last_error,next_due FROM hub_subscriptions WHERE k=?', (key,)).fetchone()
    assert json.loads(raw) == params
    assert error == '' and due == 0
    assert registry.due()[0][0] == key
