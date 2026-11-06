"""Integration of RSI range exits and the immediately following PAPER entry."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_core import attach, bars

from bcc.config import defaults, group, condition, range_section
from bcc.storage import initial_state
from bcc.strategies import Veles, rsi_handoff_target
from bcc.trading import enter, plan


def setup_positions(cfg=None):
    cfg = deepcopy(cfg or defaults('VELES'))
    cfg['symbols'] = ['BTCUSDT']
    cfg['sections'] = [range_section(30, 40), range_section(40, 50)]
    cfg['settings'].update(leverage=1, allocation_pct=10, fee_rate=.001,
                           slippage_pct=0)
    cfg['settings']['orders'].update(count=1, first_offset_pct=0)
    state, events = initial_state(500), []
    attach(state)
    source = cfg['sections'][0]
    source_key = 'BTCUSDT:' + source['id']
    ok, reason = enter(state, events, source_key, 'BTCUSDT', 'LONG',
                       cfg['settings'], 100, 50,
                       plan(cfg['settings']['orders'], 'LONG', 100), source['id'])
    assert ok, reason
    state['positions'][source_key]['exit_signal'] = deepcopy(source['exit'])
    return cfg, state, events, source_key


def fake_signal(state, key, spec, bars_, now_ms):
    return SimpleNamespace(
        matched=key.endswith('LONG_30_40:exit'),
        available=True, observations=[],
    )


def test_exit_one_minute_enters_next_two_minute_range_on_same_decision(monkeypatch):
    cfg, state, events, source = setup_positions()
    monkeypatch.setattr('bcc.strategies.signal', fake_signal)
    assert cfg['sections'][0]['exit']['conditions'][0]['timeframe'] == 1
    assert cfg['sections'][1]['entry']['conditions'][0]['timeframe'] == 2
    Veles().process(cfg, state, events, 'BTCUSDT', bars([100, 100]),
                    99.99, 100.01, 120000)
    target = 'BTCUSDT:' + cfg['sections'][1]['id']
    assert source not in state['positions']
    assert target in state['positions']
    assert [e['action'] for e in events] == ['OPEN', 'CLOSE', 'OPEN']
    assert events[1]['reason'] == 'SIGNAL_EXIT'
    # Both writes are in the same serial execution, without waiting for a
    # second RSI crossing, and old account history is not reset.
    assert state['stats'][source]['cycles'] == 1


def test_short_ranges_transfer_on_matching_down_crossing():
    cfg = defaults('VELES')
    cfg['sections'] = [range_section(70, 60, 'SHORT'), range_section(60, 50, 'SHORT')]
    first = cfg['sections'][0]
    assert rsi_handoff_target(cfg, 'BTCUSDT', 'BTCUSDT:' + first['id'],
                              'SHORT', first['exit']) == 'BTCUSDT:' + cfg['sections'][1]['id']


@pytest.mark.parametrize('change', ['composite_exit', 'composite_entry',
                                    'different_period', 'different_direction',
                                    'ambiguous', 'disabled'])
def test_handoff_is_conservative_and_never_bypasses_protection(monkeypatch, change):
    cfg, state, events, source = setup_positions()
    dest = cfg['sections'][1]
    if change == 'composite_exit':
        cfg['sections'][0]['exit']['conditions'].append(
            condition('PRICE', 80, '>', 1))
        state['positions'][source]['exit_signal'] = deepcopy(cfg['sections'][0]['exit'])
    elif change == 'composite_entry':
        dest['entry']['conditions'].append(condition('PRICE', 80, '>', 1))
    elif change == 'different_period':
        dest['entry']['conditions'][0]['period'] = 21
    elif change == 'different_direction':
        dest['entry']['conditions'][0]['comparison'] = 'cross_down'
    elif change == 'ambiguous':
        copy = deepcopy(dest)
        copy['id'] = 'another_range_40_50'
        cfg['sections'].append(copy)
    elif change == 'disabled':
        dest['enabled'] = False
    monkeypatch.setattr('bcc.strategies.signal', fake_signal)
    Veles().process(cfg, state, events, 'BTCUSDT', bars([100, 100]),
                    99.99, 100.01, 120000)
    assert source not in state['positions']
    assert not state['positions']
    assert [e['action'] for e in events] == ['OPEN', 'CLOSE']


def test_no_rsi_handoff_when_exit_fills_only_partially(monkeypatch):
    cfg, state, events, source = setup_positions()
    book = state['_books']['BTCUSDT']
    for side in ('bids', 'asks'):
        for level in book[side]:
            level[1] = 0.05
    monkeypatch.setattr('bcc.strategies.signal', fake_signal)
    Veles().process(cfg, state, events, 'BTCUSDT', bars([100, 100]),
                    99.99, 100.01, 120000)
    assert source in state['positions']
    assert state['positions'][source].get('exit_pending')
    assert 'BTCUSDT:' + cfg['sections'][1]['id'] not in state['positions']
    assert not any(e['action'] == 'OPEN' and e['section'] == cfg['sections'][1]['id']
                   for e in events)


def test_pending_or_draining_blocks_transfer(monkeypatch):
    cfg, state, events, source = setup_positions()
    state['strategy']['draining'] = True
    monkeypatch.setattr('bcc.strategies.signal', fake_signal)
    Veles().process(cfg, state, events, 'BTCUSDT', bars([100, 100]),
                    99.99, 100.01, 120000)
    assert not state['positions']
    assert [e['action'] for e in events] == ['OPEN', 'CLOSE']


def test_handoff_commits_atomic_close_open_and_restores_after_restart(tmp_path, monkeypatch):
    from bcc.storage import Store
    cfg, _, _, source = setup_positions()
    store = Store(tmp_path, 500)
    with store.transaction() as (state, events):
        attach(state)
        first = cfg['sections'][0]
        ok, why = enter(state, events, source, 'BTCUSDT', 'LONG', cfg['settings'],
                        100, 50, plan(cfg['settings']['orders'], 'LONG', 100),
                        first['id'])
        assert ok, why
        state['positions'][source]['exit_signal'] = deepcopy(first['exit'])
        state.pop('_books')
    monkeypatch.setattr('bcc.strategies.signal', fake_signal)
    with store.transaction() as (state, events):
        attach(state)
        Veles().process(cfg, state, events, 'BTCUSDT', bars([100, 100]),
                        99.99, 100.01, 120000)
        assert [e['action'] for e in events] == ['CLOSE', 'OPEN']
        state.pop('_books')
    store.audit()
    restarted = Store(tmp_path).read()
    target = 'BTCUSDT:' + cfg['sections'][1]['id']
    assert list(restarted['positions']) == [target]
    assert restarted['stats'][source]['cycles'] == 1
    assert [e['action'] for e in reversed(store.history())] == ['OPEN', 'CLOSE', 'OPEN']
