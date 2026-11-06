"""Ledger-index fault matrix. Every test uses only an isolated PAPER account."""
import json
import sqlite3

import pytest

from bcc.storage import AccountingError, Store


@pytest.mark.parametrize('column,value', [
    ('action', 'OPEN'),
    ('symbol', 'BTCUSDT'),
    ('section', 'OTHER'),
    ('at', 1.0),
    ('id', 'relabelled'),
])
@pytest.mark.parametrize('reader', ('audit', 'history', 'effective_capital'))
def test_corrupt_sqlite_index_cannot_disagree_with_event_json(tmp_path, column, value, reader):
    store = Store(tmp_path, 100)
    store.adjust_capital(30, 'deposit-1')
    with sqlite3.connect(store.path) as db:
        db.execute(f'UPDATE executions SET {column}=? WHERE id=?', (value, 'deposit-1'))
    before = store.path.read_bytes()
    with pytest.raises(AccountingError, match='Столбцы'):
        getattr(store, reader)()
    # No automatic reset of the ledger or replacement of a corrupt record.
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM executions').fetchone()[0] == 1
        assert db.execute(f'SELECT {column} FROM executions').fetchone()[0] == value


@pytest.mark.parametrize('data', [
    {'id':'missing-at', 'symbol':'BTCUSDT', 'section':'A', 'action':'FEE', 'cash_delta':0},
    {'id':'bad-action', 'at':1, 'symbol':'BTCUSDT', 'section':'A', 'action':'', 'cash_delta':0},
    {'id':'bad-cash', 'at':1, 'symbol':'BTCUSDT', 'section':'A', 'action':'FEE', 'cash_delta': '1'},
])
def test_invalid_new_event_rolls_back_cash_and_journal(tmp_path, data):
    store = Store(tmp_path, 100)
    with pytest.raises(AccountingError):
        with store.transaction(include_charts=False) as (state, events):
            events.append(data)
    assert store.read(include_charts=False)['cash'] == 100
    assert store.history() == []


def test_valid_trades_and_deposits_remain_compatible(tmp_path):
    store = Store(tmp_path, 100)
    store.adjust_capital(20, 'deposit-1')
    store.adjust_capital(-10, 'deposit-2')
    assert store.effective_capital() == 110
    assert store.read()['cash'] == 110
    assert len(store.history()) == 2
    store.audit()

@pytest.mark.parametrize('field', ('private_key', 'polymarket_relayer_api_key', 'walletPrivateKey'))
@pytest.mark.parametrize('reader', ('audit', 'history', 'effective_capital'))
def test_private_credentials_in_journal_block_unsafe_account_export(tmp_path, field, reader):
    store = Store(tmp_path, 100)
    store.adjust_capital(10, 'deposit-1')
    with sqlite3.connect(store.path) as db:
        row = db.execute('SELECT data FROM executions WHERE id=?', ('deposit-1',)).fetchone()
        payload = json.loads(row[0])
        payload['metadata'] = {field: 'do-not-export'}
        db.execute('UPDATE executions SET data=? WHERE id=?', (json.dumps(payload), 'deposit-1'))
    with pytest.raises(AccountingError, match='приватные'):
        getattr(store, reader)()
    with sqlite3.connect(store.path) as db:
        assert 'do-not-export' in db.execute('SELECT data FROM executions').fetchone()[0]
