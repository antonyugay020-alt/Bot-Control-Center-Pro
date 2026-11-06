"""One create/edit/load contract for the real VELES and Polymarket builders.

These tests do not send orders, start workers, or simulate GUI acceptance.
"""
from copy import deepcopy
from bcc.config import defaults, resolve_poly_market, validate
from bcc.constructor_contract import validate_constructor_roundtrip
from bcc.storage import Repository


def test_poly_seconds_per_coin_and_market_survive_create_edit_load(tmp_path):
    repo = Repository(tmp_path)
    config = defaults('POLYMARKET')
    config.update(name='PAPER timing', symbols=['BTC', 'ETH'])
    config['polymarket'].update(durations=[300, 900], impulse_mode='usd', impulse=85.0,
        entry_signal_mode='before_close', entry_seconds_before_close=60.0,
        entry_window_seconds=12.0, entry_min_seconds_left=5.0)
    config['coin_overrides'] = {'ETH': {'parameters': {'entry_seconds_before_close': 45.0,
        'entry_window_seconds': 8.0, 'impulse': 4.2}}}
    config['polymarket']['overrides'] = {'BTC:300': {
        'entry_seconds_before_close': 30.0, 'entry_window_seconds': 5.0}}
    before = deepcopy(config)
    assert validate_constructor_roundtrip(config) == validate(config)
    assert config == before
    folder = repo.create(config)
    loaded, digest = repo.load(folder)
    assert resolve_poly_market(loaded, 'BTC', 300)['entry_seconds_before_close'] == 30
    assert resolve_poly_market(loaded, 'BTC', 900)['entry_seconds_before_close'] == 60
    assert resolve_poly_market(loaded, 'ETH', 300)['entry_seconds_before_close'] == 45
    assert resolve_poly_market(loaded, 'ETH', 900)['impulse'] == 4.2
    loaded['polymarket']['overrides']['BTC:300']['entry_seconds_before_close'] = 40.0
    repo.save(folder, loaded, digest)
    reopened, _ = repo.load(folder)
    assert resolve_poly_market(reopened, 'BTC', 300)['entry_seconds_before_close'] == 40
    assert resolve_poly_market(reopened, 'BTC', 900)['entry_seconds_before_close'] == 60
    assert reopened['coin_overrides'] == loaded['coin_overrides']


def test_veles_independent_entry_exit_and_coin_section_survive_edit(tmp_path):
    repo = Repository(tmp_path)
    config = defaults('VELES')
    config.update(name='RSI continuous', symbols=['BTCUSDT', 'ETHUSDT'])
    sid = config['sections'][0]['id']
    config['coin_overrides'] = {'ETHUSDT': {'sections': {sid: {'enabled': False}}}}
    original = deepcopy(config)
    assert validate_constructor_roundtrip(config) == validate(config)
    assert config == original
    folder = repo.create(config)
    saved, digest = repo.load(folder)
    assert saved['sections'][0]['entry']['conditions'][0]['timeframe'] == 2
    assert saved['sections'][0]['exit']['conditions'][0]['timeframe'] == 1
    assert saved['coin_overrides']['ETHUSDT']['sections'][sid]['enabled'] is False
    saved['sections'][0]['entry']['conditions'][0]['timeframe'] = 5
    repo.save(folder, saved, digest)
    reopened, _ = repo.load(folder)
    assert reopened['sections'][0]['entry']['conditions'][0]['timeframe'] == 5
    assert reopened['sections'][0]['exit']['conditions'][0]['timeframe'] == 1
    assert reopened['coin_overrides'] == saved['coin_overrides']


def test_legacy_zero_window_is_lossless_and_remains_until_safety_cutoff(tmp_path):
    """An existing valid zero-width sentinel is not silently reinterpreted."""
    from bcc.config import poly_window_allows

    config = defaults('POLYMARKET')
    config['name'] = 'Legacy PAPER seconds'
    config['polymarket'].update(
        durations=[300], entry_signal_mode='before_close',
        entry_seconds_before_close=120, entry_window_seconds=0,
        entry_min_seconds_left=5,
    )
    repo = Repository(tmp_path)
    folder = repo.create(config)
    restored, version = repo.load(folder)
    rule = resolve_poly_market(restored, 'BTC', 300)
    assert rule['entry_window_seconds'] == 0
    assert poly_window_allows(rule, 0, 300, 200, 201)
    assert not poly_window_allows(rule, 0, 300, 296, 297)
    restored['name'] = 'Edited legacy PAPER seconds'
    repo.save(folder, restored, version)
    reopened, _ = repo.load(folder)
    assert resolve_poly_market(reopened, 'BTC', 300)['entry_window_seconds'] == 0


def test_invalid_market_timing_rejected_before_repository_commit(tmp_path):
    config = defaults('POLYMARKET')
    config['name'] = 'Atomic PAPER editor'
    repo = Repository(tmp_path)
    folder = repo.create(config)
    restored, digest = repo.load(folder)
    restored['polymarket']['overrides']['BTC:300'] = {
        'entry_signal_mode': 'before_close',
        'entry_seconds_before_close': 2,
        'entry_min_seconds_left': 5,
    }
    import pytest
    with pytest.raises(ValueError):
        repo.save(folder, restored, digest)
    persisted, unchanged_digest = repo.load(folder)
    assert unchanged_digest == digest
    assert 'BTC:300' not in persisted['polymarket']['overrides']
