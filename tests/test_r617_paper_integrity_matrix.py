"""Whole-account damage matrix: fail closed with no silent reset or partial writes.

Never use a production account. All SQLite modifications happen under tmp_path.
"""
import sqlite3
from pathlib import Path

import pytest

from bcc.storage import (AccountingError, Store, DEPOSIT_INTENT,
                         recover_deposit_change)


def damage_state(folder, raw):
    with sqlite3.connect(folder / 'paper.sqlite') as connection:
        connection.execute('UPDATE state SET document=? WHERE id=1', (raw,))


def damage_event(folder, raw):
    with sqlite3.connect(folder / 'paper.sqlite') as connection:
        connection.execute("INSERT INTO executions VALUES ('bad-id',1,'ACCOUNT','CAPITAL','CAPITAL_ADJUST',?)", (raw,))


@pytest.mark.parametrize('broken', ['[]', 'null', '42', '"text"', 'NaN', '{'])
@pytest.mark.parametrize('method', ['read', 'audit', 'transaction', 'chart', 'chart_symbols', 'effective_capital'])
def test_invalid_paper_document_blocks_every_reader_and_writer(tmp_path, broken, method):
    store = Store(tmp_path, 100)
    damage_state(tmp_path, broken)
    with pytest.raises(AccountingError):
        if method == 'transaction':
            with store.transaction(include_charts=False) as (state, events):
                state['cash'] = 0
        elif method == 'chart':
            store.chart('BTCUSDT')
        else:
            getattr(store, method)()
    # Nothing attempted to repair the user's original paper.sqlite.
    with sqlite3.connect(tmp_path / 'paper.sqlite') as connection:
        assert connection.execute('SELECT document FROM state').fetchone()[0] == broken


@pytest.mark.parametrize('broken', ['[]', 'null', 'NaN', '{', '{"cash_delta":"abc"}', '{"cash_delta":NaN}'])
@pytest.mark.parametrize('method', ['audit', 'history', 'effective_capital'])
def test_invalid_paper_journal_blocks_accounting_and_history(tmp_path, broken, method):
    store = Store(tmp_path, 100)
    damage_event(tmp_path, broken)
    with pytest.raises(AccountingError):
        getattr(store, method)()
    with sqlite3.connect(tmp_path / 'paper.sqlite') as connection:
        assert connection.execute('SELECT data FROM executions WHERE id="bad-id"').fetchone()[0] == broken


@pytest.mark.parametrize('broken', ['[]', 'null', 'NaN', '{', '{"version":1,"account_id":"a","initial":"100"}', '{"version":1,"account_id":"a","initial":true}'])
def test_invalid_account_marker_blocks_start_and_preserves_ledger(tmp_path, broken):
    Store(tmp_path, 100)
    (tmp_path / Store.MARKER).write_text(broken, encoding='utf-8')
    with pytest.raises(AccountingError):
        Store(tmp_path, 100)
    assert Store(tmp_path).read()['cash'] == 100
    assert (tmp_path / Store.MARKER).read_text(encoding='utf-8') == broken


@pytest.mark.parametrize('broken', ['[]', 'null', 'NaN', '{', '{"version":1,"event_id":"a"}'])
def test_invalid_deposit_intent_never_applies_partial_changes(tmp_path, broken):
    Store(tmp_path, 100)
    (tmp_path / DEPOSIT_INTENT).write_text(broken, encoding='utf-8')
    with pytest.raises(AccountingError):
        recover_deposit_change(tmp_path)
    assert Store(tmp_path).read()['cash'] == 100
    assert (tmp_path / DEPOSIT_INTENT).read_text(encoding='utf-8') == broken


def test_good_capital_adjustment_survives_restart_and_audit(tmp_path):
    store = Store(tmp_path, 100)
    store.adjust_capital(30, 'unique-R617-credit')
    assert Store(tmp_path, 130).effective_capital() == 130
    assert Store(tmp_path).read()['cash'] == 130
    assert len(Store(tmp_path).history()) == 1
    Store(tmp_path).audit()
    with pytest.raises(sqlite3.IntegrityError):
        Store(tmp_path).adjust_capital(30, 'unique-R617-credit')
    assert Store(tmp_path).read()['cash'] == 130
