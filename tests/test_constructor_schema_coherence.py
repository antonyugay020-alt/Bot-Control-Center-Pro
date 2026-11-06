"""Lossless global/coin/market Polymarket forms and VELES repository cycle.

No network, worker launch, live settings, or orders are used in these tests.
"""
from copy import deepcopy

import pytest

from bcc.config import defaults, poly_rule_keys, resolve_poly_market
from bcc.constructor_contract import (
    POLY_FORM_SPECS, market_override_parent, validate_constructor_roundtrip,
    validate_market_override, validate_poly_form_schema,
)
from bcc.storage import Repository


def test_polymarket_ui_schema_covers_every_persisted_policy_field():
    assert validate_poly_form_schema() is True
    fields = [spec[0] for spec in POLY_FORM_SPECS]
    assert len(fields) == len(set(fields))
    assert set(fields) | {'indicator_filter'} == poly_rule_keys()
    assert {'disabled', 'entry_seconds_before_close', 'entry_window_seconds',
            'entry_min_seconds_left', 'impulse', 'ask_min', 'ask_max'} <= set(fields)


def test_polymarket_form_schema_rejects_new_unrendered_fields(monkeypatch):
    monkeypatch.setattr('bcc.constructor_contract.poly_rule_keys',
                        lambda: poly_rule_keys() | {'new_persisted_policy'})
    with pytest.raises(ValueError, match='не охватывает'):
        validate_poly_form_schema()


def test_global_coin_market_disable_and_seconds_survive_edit_save_load(tmp_path):
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'All three editor levels'
    cfg['symbols'] = ['BTC', 'ETH']
    cfg['polymarket']['durations'] = [300, 900]
    cfg['polymarket'].update(
        entry_signal_mode='before_close', entry_seconds_before_close=60,
        entry_window_seconds=15, entry_min_seconds_left=5,
        disabled=True,
    )
    # Coin re-enables entries, with its own independent entry timing.
    cfg['coin_overrides']['ETH'] = {'parameters': {
        'disabled': False, 'entry_seconds_before_close': 45,
        'entry_window_seconds': 10,
    }}
    assert resolve_poly_market(cfg, 'BTC', 300)['disabled'] is True
    assert resolve_poly_market(cfg, 'ETH', 900)['disabled'] is False
    inherited = market_override_parent(cfg, 'ETH', 300)
    assert inherited['disabled'] is False
    assert inherited['entry_seconds_before_close'] == 45
    proposal = deepcopy(inherited)
    proposal['disabled'] = True
    proposal['entry_seconds_before_close'] = 25
    proposal['entry_window_seconds'] = 7
    cfg['polymarket']['overrides']['ETH:300'] = validate_market_override(
        cfg, 'ETH', 300, proposal,
    )
    assert cfg['polymarket']['overrides']['ETH:300'] == {
        'disabled': True, 'entry_seconds_before_close': 25,
        'entry_window_seconds': 7,
    }
    repo = Repository(tmp_path)
    folder = repo.create(cfg)
    loaded, revision = repo.load(folder)
    assert loaded == validate_constructor_roundtrip(cfg)
    assert resolve_poly_market(loaded, 'BTC', 900)['disabled'] is True
    assert resolve_poly_market(loaded, 'ETH', 900)['disabled'] is False
    assert resolve_poly_market(loaded, 'ETH', 300)['disabled'] is True
    assert resolve_poly_market(loaded, 'ETH', 300)['entry_seconds_before_close'] == 25
    # Reset only this market to the inherited coin policy, then persist.
    inherited = market_override_parent(loaded, 'ETH', 300)
    assert validate_market_override(loaded, 'ETH', 300, inherited) == {}
    loaded['polymarket']['overrides'].pop('ETH:300')
    repo.save(folder, loaded, revision)
    final, _ = repo.load(folder)
    assert resolve_poly_market(final, 'ETH', 300)['disabled'] is False
    assert resolve_poly_market(final, 'ETH', 300)['entry_seconds_before_close'] == 45
    assert resolve_poly_market(final, 'BTC', 300)['disabled'] is True


def test_veles_rsi_entry_exit_timeframes_survive_repository_edit(tmp_path):
    cfg = defaults('VELES')
    cfg['name'] = 'RSI continuous ranges'
    cfg['sections'][0]['entry']['conditions'][0]['timeframe'] = 2
    cfg['sections'][0]['exit']['conditions'][0]['timeframe'] = 1
    cfg['coin_overrides']['BTCUSDT'] = {'settings': {'allocation_pct': 7.0}}
    repo = Repository(tmp_path)
    folder = repo.create(cfg)
    loaded, revision = repo.load(folder)
    assert loaded['sections'][0]['entry']['conditions'][0]['timeframe'] == 2
    assert loaded['sections'][0]['exit']['conditions'][0]['timeframe'] == 1
    loaded['coin_overrides']['BTCUSDT']['settings']['allocation_pct'] = 8.0
    repo.save(folder, loaded, revision)
    final, _ = repo.load(folder)
    assert final['coin_overrides']['BTCUSDT']['settings']['allocation_pct'] == 8.0
    assert final['sections'][0]['entry']['conditions'][0]['timeframe'] == 2
    assert final['sections'][0]['exit']['conditions'][0]['timeframe'] == 1
