"""Asset, cash and journal recovery through real PAPER fills and transfers."""
import json
from copy import deepcopy

import pytest

from bcc.config import defaults
from bcc.funding import apply_funding
from bcc.storage import AccountingError, Repository, Store
from bcc.supervisor import Supervisor
from bcc.trading import add_order, enter, exit_position, plan
from bcc.worker import process_polymarket
from test_core import attach
from test_poly_contract_seconds import ScheduledSource, cfg as poly_cfg


def opened(tmp_path, strategy='VELES', side='LONG'):
    operator = Supervisor(Repository(tmp_path / 'data'))
    cfg = defaults(strategy)
    cfg['name'] = 'Journal ' + strategy
    cfg['settings']['orders']['count'] = 3
    cfg['settings']['leverage'] = 3
    folder = operator.create_bot(cfg)
    store = Store(folder, cfg['capital'])
    key = 'BTCUSDT:main'
    with store.transaction() as (state, events):
        attach(state)
        assert enter(state, events, key, 'BTCUSDT', side, cfg['settings'],
                     100, 25, plan(cfg['settings']['orders'], side, 100), 'main')[0]
    store.audit()
    return operator, folder, store, key


@pytest.mark.parametrize('strategy', ['VELES', 'GRID', 'PUMP_DUMP', 'EMA',
                                     'MACD', 'BOLLINGER', 'EMA_RSI_VOLUME'])
@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_fill_dca_funding_partial_transfer_and_full_recovery(tmp_path, monkeypatch, strategy, side):
    import bcc.trading as trading
    import time
    tick = [time.time()]
    monkeypatch.setattr(trading, 'market_now', lambda: tick[0])
    operator, folder, store, key = opened(tmp_path, strategy, side)
    start = tick[0]
    initial_qty = store.read()['positions'][key]['qty']
    tick[0] += 60
    with store.transaction() as (state, events):
        attach(state, price=90 if side == 'LONG' else 110)
        assert add_order(state, events, key, 90 if side == 'LONG' else 110)
        apply_funding(state, events, 'BTCUSDT',
                      [dict(at=start + 30, rate=-.001)], {start + 30: 100}, tick[0])
        assert events[-1]['qty'] == pytest.approx(initial_qty)
    store.audit()
    tick[0] += 60
    with store.transaction() as (state, events):
        attach(state, price=105)
        assert exit_position(state, events, key, 105, 'TEST_PARTIAL', ratio=.25)
    store.audit()
    snapshot = store.read()
    original_events = store.history()
    archived = tmp_path / 'recovery.zip'
    operator.export_bots([folder], archived)
    restored = operator.import_bots(archived)[0]
    replica = Store(restored)
    replica.audit()
    assert replica.read() == snapshot
    assert replica.history() == original_events
    assert json.loads((folder / Store.MARKER).read_text(encoding="utf-8"))['account_id'] != json.loads(
        (restored / Store.MARKER).read_text(encoding="utf-8"))['account_id']
    tick[0] += 60
    with replica.transaction() as (state, events):
        attach(state, price=105)
        assert exit_position(state, events, key, 105, 'TEST_COMPLETE')
    replica.audit()
    # A delayed published rate belongs to the CLOSED cycle, never a new one.
    with replica.transaction() as (state, events):
        apply_funding(state, events, 'BTCUSDT',
                      [dict(at=start + 90, rate=.002)], {start + 90: 100}, tick[0])
    Store(restored, operator.repo.load(restored)[0]['capital']).audit()
    assert not replica.read()['positions']
    assert Store(folder).read() == snapshot
    assert Store(folder).history() == original_events


@pytest.mark.parametrize('field', ['qty', 'average', 'fees', 'margin', 'side',
                                  'realized', 'original_qty', 'spent', 'funding_total',
                                  'position_id', 'remove_identity', 'remove_position'])
def test_balanced_cash_cannot_hide_corrupted_position(tmp_path, field):
    operator, folder, store, key = opened(tmp_path)
    before = store.read()
    state = deepcopy(before)
    p = state['positions'][key]
    if field == 'remove_position':
        state['positions'].clear()
    elif field == 'remove_identity':
        del p['position_id']
    elif field == 'position_id':
        p[field] = 'a' * 32
    elif field == 'side':
        p[field] = 'SHORT'
    else:
        p[field] += 1
    with store.connect() as db:
        db.execute('UPDATE state SET document=? WHERE id=1', (json.dumps(state),))
    assert store.read()['cash'] == before['cash']
    damaged = (folder / 'paper.sqlite').read_bytes()
    with pytest.raises(AccountingError, match='позиц'):
        Store(folder, 500)
    with pytest.raises(AccountingError):
        operator.copy_bot(folder, history=True)
    with pytest.raises(AccountingError):
        operator.export_bots([folder], tmp_path / 'damaged.zip')
    assert (folder / 'paper.sqlite').read_bytes() == damaged
    assert len(operator.repo.list()) == 1
    assert not (tmp_path / 'damaged.zip').exists()


@pytest.mark.parametrize('field', ['qty', 'average', 'fees', 'margin', 'position_id', 'remove',
                                  'realized', 'original_qty', 'spent', 'funding_total'])
def test_position_mutation_without_receipt_rolls_back_atomically(tmp_path, field):
    _, folder, store, key = opened(tmp_path)
    before, history = store.read(), store.history()
    with pytest.raises(AccountingError, match='позиц'):
        with store.transaction() as (state, events):
            if field == 'remove':
                del state['positions'][key]
            elif field == 'position_id':
                state['positions'][key][field] = 'b' * 32
            else:
                state['positions'][key][field] += 1
    assert store.read() == before
    assert store.history() == history
    Store(folder, 500).audit()


@pytest.mark.parametrize('field', ['qty', 'price', 'fee', 'leverage', 'position_key', 'position_id'])
def test_corrupted_fill_is_rejected_even_with_balanced_cash(tmp_path, field):
    _, _, store, _ = opened(tmp_path)
    item = store.history()[0]
    if field in ('position_key', 'position_id'):
        item[field] = 'x' if field == 'position_key' else 'c' * 32
    else:
        item[field] += 1
    with store.connect() as db:
        db.execute('UPDATE executions SET data=? WHERE id=?', (json.dumps(item), item['id']))
    with pytest.raises(AccountingError, match='позиц'):
        store.audit()


@pytest.mark.parametrize('field', ['shares', 'cost', 'fee', 'qty', 'average'])
def test_polymarket_assets_are_replayed_independently_of_cash(tmp_path, field):
    configuration = poly_cfg()
    configuration['symbols'] = ['BTC']
    configuration['polymarket']['durations'] = [300]
    repo = Repository(tmp_path / 'poly')
    folder = repo.create(configuration)
    store = Store(folder, 500)
    source = ScheduledSource(42)
    with store.transaction() as (state, events):
        process_polymarket(configuration, state, events, source, source.now)
    store.audit()
    state = store.read()
    position = next(iter(state['positions'].values()))
    position[field] += .01
    with store.connect() as db:
        db.execute('UPDATE state SET document=? WHERE id=1', (json.dumps(state),))
    with pytest.raises(AccountingError, match='позиц'):
        store.audit()


def test_legacy_account_is_not_rewritten_or_given_invented_fills(tmp_path):
    _, folder, store, key = opened(tmp_path)
    # The exact pre-receipt schema remains readable without rewriting history.
    with store.connect() as db:
        state = json.loads(db.execute('SELECT document FROM state').fetchone()[0])
        del state['positions'][key]['position_id']
        db.execute('UPDATE state SET document=?', (json.dumps(state),))
        item = json.loads(db.execute('SELECT data FROM executions').fetchone()[0])
        for field in ('position_id', 'position_key', 'position_kind', 'leverage', 'margin_mode',
                      'position_terms_sha256'):
            item.pop(field)
        db.execute('UPDATE executions SET data=?', (json.dumps(item),))
    data = (folder / 'paper.sqlite').read_bytes()
    Store(folder, 500).audit()
    assert (folder / 'paper.sqlite').read_bytes() == data
    with store.transaction() as (state, events):
        attach(state, price=110)
        assert exit_position(state, events, key, 110, 'LEGACY_COMPLETE')
    store.audit()
    assert len(store.history()) == 2
    assert all('position_id' not in event for event in store.history())


def test_replay_uses_commit_order_when_market_clock_moves_backwards(tmp_path):
    _, _, store, key = opened(tmp_path)
    with store.transaction() as (state, events):
        attach(state, price=90)
        assert add_order(state, events, key, 90)
    with store.connect() as db:
        rows = list(db.execute('SELECT id,data FROM executions ORDER BY rowid'))
        for at, (event_id, raw) in zip((2000, 1000), rows):
            event = json.loads(raw)
            event['at'] = at
            db.execute('UPDATE executions SET at=?,data=? WHERE id=?',
                       (at, json.dumps(event), event_id))
    store.audit()


@pytest.mark.parametrize('payout', [0, .5, 1])
def test_polymarket_restore_edit_and_official_settlement_keeps_receipts(tmp_path, payout):
    configuration = poly_cfg()
    configuration['symbols'] = ['BTC']
    configuration['polymarket']['durations'] = [300]
    operator = Supervisor(Repository(tmp_path / 'poly'))
    folder = operator.create_bot(configuration)
    store = Store(folder, 500)
    source = ScheduledSource(42)
    with store.transaction() as (state, events):
        process_polymarket(configuration, state, events, source, source.now)
    original_position = deepcopy(store.read()['positions'])
    clone = operator.copy_bot(folder, history=True)
    restored = Store(clone, 500)
    edited, digest = operator.repo.load(clone)
    edited['settings']['disabled'] = True
    operator.save_bot(clone, edited, digest)
    assert restored.read()['positions'] == original_position
    source.payout = lambda _token: payout
    with restored.transaction() as (state, events):
        process_polymarket(edited, state, events, source, source.now + 43)
    restored.audit()
    assert not restored.read()['positions']
    events = list(reversed(restored.history()))
    assert [e['action'] for e in events] == ['OPEN', 'SETTLE']
    assert events[0]['position_id'] == events[1]['position_id']
    assert Store(folder).read()['positions'] == original_position
    Store(folder).audit()


def test_same_owner_and_open_timestamp_cannot_merge_funding_cycles(tmp_path, monkeypatch):
    import bcc.trading as trading
    import time
    tick = [time.time()]
    monkeypatch.setattr(trading, 'market_now', lambda: tick[0])
    operator, folder, store, key = opened(tmp_path)
    start = tick[0]
    for cycle in range(2):
        if cycle:
            # A repeated/corrected market timestamp is not a cycle identity.
            tick[0] = start
            settings = operator.repo.load(folder)[0]['settings']
            with store.transaction() as (state, events):
                attach(state)
                assert enter(state, events, key, 'BTCUSDT', 'LONG', settings,
                             100, 25, plan(settings['orders'], 'LONG', 100), 'main')[0]
        tick[0] = start + 60
        with store.transaction() as (state, events):
            attach(state, price=105)
            assert exit_position(state, events, key, 105, 'TEST_COMPLETE')
        store.audit()
    assert len(store.read()['funding_archive']) == 2
    rate = dict(at=start + 30, rate=.001)
    with store.transaction() as (state, events):
        apply_funding(state, events, 'BTCUSDT', [rate], {rate['at']: 100}, start + 90)
        assert len(events) == 2
        assert len({e['position_id'] for e in events}) == 2
    before = store.read()
    with store.transaction() as (state, events):
        apply_funding(state, events, 'BTCUSDT', [rate], {rate['at']: 100}, start + 90)
        assert not events
    assert store.read() == before
    Store(folder, 500).audit()


@pytest.mark.parametrize('mode', ['transaction', 'restart'])
@pytest.mark.parametrize('path', [('settings', 'fee_rate'), ('settings', 'profit', 'pct'),
                                  ('orders', 0, 'price'), ('budget',), ('reinvest_pct',),
                                  ('execution_rules', 'qty_step')])
def test_frozen_future_trade_terms_cannot_change_without_a_new_cycle(tmp_path, mode, path):
    _, folder, store, key = opened(tmp_path)
    before, history = store.read(), store.history()

    def change(state):
        target = state['positions'][key]
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] += .01

    if mode == 'transaction':
        with pytest.raises(AccountingError, match='Условия'):
            with store.transaction() as (state, _):
                change(state)
        assert store.read() == before
        assert store.history() == history
    else:
        state = deepcopy(before)
        change(state)
        with store.connect() as db:
            db.execute('UPDATE state SET document=? WHERE id=1', (json.dumps(state),))
        with pytest.raises(AccountingError, match='Условия'):
            Store(folder, 500)


@pytest.mark.parametrize('path', [('token',), ('slug',), ('end',),
                                  ('entry_rule', 'entry_seconds_before_close'),
                                  ('entry_observation', 'observed_ask')])
def test_polymarket_outcome_and_applied_rule_are_frozen_in_receipt(tmp_path, path):
    configuration = poly_cfg()
    configuration['symbols'] = ['BTC']
    configuration['polymarket']['durations'] = [300]
    repo = Repository(tmp_path / 'poly')
    folder = repo.create(configuration)
    store = Store(folder, 500)
    source = ScheduledSource(42)
    with store.transaction() as (state, events):
        process_polymarket(configuration, state, events, source, source.now)
    state = store.read()
    target = next(iter(state['positions'].values()))
    for part in path[:-1]:
        target = target[part]
    value = target[path[-1]]
    target[path[-1]] = value + '_changed' if isinstance(value, str) else value + .01
    with store.connect() as db:
        db.execute('UPDATE state SET document=? WHERE id=1', (json.dumps(state),))
    with pytest.raises(AccountingError, match='Условия'):
        Store(folder, 500)


def test_closed_cycle_accounting_must_match_before_late_funding_recovery(tmp_path):
    _, folder, store, key = opened(tmp_path)
    with store.transaction() as (state, events):
        attach(state, price=110)
        assert exit_position(state, events, key, 110, 'TEST_COMPLETE')
    state = store.read()
    next(iter(state['funding_archive'].values()))['cycle_pnl'] += 1
    with store.connect() as db:
        db.execute('UPDATE state SET document=? WHERE id=1', (json.dumps(state),))
    with pytest.raises(AccountingError, match='cycle_pnl'):
        Store(folder, 500)
