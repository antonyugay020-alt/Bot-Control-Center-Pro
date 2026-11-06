"""The Qt market editor must validate the exact effective rule it saves."""
from copy import deepcopy

import pytest

from bcc.config import defaults, resolve_poly_market
from bcc.constructor_contract import (market_override_parent, market_editor_rules,
                                      validate_market_override)
from bcc.storage import Repository


def _editable(rule):
    return {k: v for k, v in rule.items() if k not in ("durations", "overrides")}


def _draft():
    c = defaults('POLYMARKET')
    c['name'] = 'Market editing'
    c['symbols'] = ['BTC', 'ETH']
    c['polymarket'].update(durations=[300, 900], entry_signal_mode='before_close',
        entry_seconds_before_close=60, entry_window_seconds=20,
        entry_min_seconds_left=5)
    c['coin_overrides'] = {'ETH': {'parameters': {'entry_seconds_before_close': 40,
        'entry_window_seconds': 10}}}
    c['polymarket']['overrides'] = {'BTC:900': {'ask_min': .7}}
    return c


def test_market_edit_is_minimal_and_preserves_other_markets(tmp_path):
    draft = _draft()
    original = deepcopy(draft)
    proposal = _editable(resolve_poly_market(draft, 'ETH', 300))
    proposal['entry_seconds_before_close'] = 30
    proposal['entry_window_seconds'] = 8
    patch = validate_market_override(draft, 'ETH', 300, proposal)
    assert patch == {'entry_seconds_before_close': 30, 'entry_window_seconds': 8}
    assert draft == original
    draft['polymarket']['overrides']['ETH:300'] = patch
    repo = Repository(tmp_path)
    folder = repo.create(draft)
    restored, version = repo.load(folder)
    assert resolve_poly_market(restored, 'ETH', 300)['entry_seconds_before_close'] == 30
    assert resolve_poly_market(restored, 'ETH', 900)['entry_seconds_before_close'] == 40
    assert resolve_poly_market(restored, 'BTC', 900)['ask_min'] == .7
    assert restored['polymarket']['overrides']['ETH:300'] == patch
    # Edit -> reload -> revert to inherited coin timing, without touching BTC.
    reset = _editable(resolve_poly_market(restored, 'ETH', 900))
    patch = validate_market_override(restored, 'ETH', 300, reset)
    assert patch == {}
    restored['polymarket']['overrides'].pop('ETH:300')
    repo.save(folder, restored, version)
    final, _ = repo.load(folder)
    assert 'ETH:300' not in final['polymarket']['overrides']
    assert resolve_poly_market(final, 'ETH', 300)['entry_seconds_before_close'] == 40


def test_market_edit_rejects_invalid_timing_without_mutating_draft():
    draft = _draft()
    old = deepcopy(draft)
    bad = _editable(resolve_poly_market(draft, 'ETH', 300))
    bad['entry_seconds_before_close'] = 3  # Less than safety cutoff.
    with pytest.raises(ValueError):
        validate_market_override(draft, 'ETH', 300, bad)
    assert draft == old
    with pytest.raises(ValueError):
        validate_market_override(draft, 'ETH', 300, {'durations': [900]})
    with pytest.raises(ValueError):
        validate_market_override(draft, 'ETH', 3600, bad)
    with pytest.raises(ValueError):
        validate_market_override(defaults('VELES'), 'ETH', 300, bad)


def test_market_edit_detects_invalid_inherited_coin_configuration():
    draft = _draft()
    draft['coin_overrides']['ETH']['parameters']['entry_seconds_before_close'] = 2
    invalid_eth = _editable(resolve_poly_market(draft, 'ETH', 300))
    with pytest.raises(ValueError):
        validate_market_override(draft, 'ETH', 300, invalid_eth)


def test_market_edit_does_not_require_finished_other_wizard_pages():
    draft = _draft()
    draft['name'] = ''  # Name is entered on a different page.
    draft['capital'] = 0  # Invalid until final Save.
    rule = _editable(resolve_poly_market(draft, 'ETH', 300))
    rule['entry_seconds_before_close'] = 35
    assert validate_market_override(draft, 'ETH', 300, rule) == {
        'entry_seconds_before_close': 35,
    }


def test_market_editor_parent_includes_disabled_and_excludes_old_market_patch():
    draft = _draft()
    draft['polymarket']['overrides']['ETH:300'] = {
        'disabled': True, 'entry_seconds_before_close': 25,
        'entry_window_seconds': 7,
    }
    before = deepcopy(draft)
    inherited = market_override_parent(draft, 'ETH', 300)
    assert inherited['disabled'] is False
    assert inherited['entry_seconds_before_close'] == 40
    assert inherited['entry_window_seconds'] == 10
    assert 'overrides' not in inherited and 'durations' not in inherited
    assert draft == before
    # The exact Qt reset values are a valid no-op for the shared validator.
    assert validate_market_override(draft, 'ETH', 300, inherited) == {}
    changed = dict(inherited, disabled=True)
    assert validate_market_override(draft, 'ETH', 300, changed) == {'disabled': True}


def test_market_editor_parent_respects_coin_disabled_and_global_timing():
    draft = _draft()
    draft['coin_overrides']['ETH']['parameters']['disabled'] = True
    inherited = market_override_parent(draft, 'ETH', 900)
    assert inherited['disabled'] is True
    assert inherited['entry_seconds_before_close'] == 40
    assert market_override_parent(draft, 'BTC', 900)['disabled'] is False
    with pytest.raises(ValueError):
        market_override_parent(draft, 'ETH', 3600)


def test_reopen_market_filter_keeps_inherited_and_specific_filters(tmp_path):
    from bcc.config import condition, group
    cfg = _draft()
    inherited_filter = group(condition('RSI', 45, '<', 1))
    market_filter = group(condition('EMA', 15, '>', 1))
    cfg['polymarket']['indicator_filter'] = inherited_filter
    cfg['polymarket']['overrides']['ETH:300'] = {'indicator_filter': market_filter}
    original = deepcopy(cfg)
    parent, effective = market_editor_rules(cfg, 'ETH', 300)
    assert parent['indicator_filter'] == inherited_filter
    assert effective['indicator_filter'] == market_filter
    assert market_editor_rules(cfg, 'ETH', 900)[1]['indicator_filter'] == inherited_filter
    assert cfg == original

    # Editing ASK must preserve the market's independently configured filter.
    edited = dict(effective, ask_min=.35)
    patch = validate_market_override(cfg, 'ETH', 300, edited)
    from bcc.config import merge
    assert merge(parent, patch)['indicator_filter'] == market_filter
    cfg['polymarket']['overrides']['ETH:300'] = patch
    repo = Repository(tmp_path)
    folder = repo.create(cfg)
    restored, _ = repo.load(folder)
    assert market_editor_rules(restored, 'ETH', 300)[1]['indicator_filter'] == market_filter
    assert market_editor_rules(restored, 'ETH', 900)[1]['indicator_filter'] == inherited_filter
    # Reset is the *complete* inherited rule, including the indicator group.
    assert validate_market_override(restored, 'ETH', 300,
                                    market_editor_rules(restored, 'ETH', 300)[0]) == {}


def test_reopen_coin_filter_inheritance_and_disabled_override():
    from bcc.config import condition, group
    cfg = _draft()
    coin_filter = group(condition('RSI', 55, '>', 1))
    cfg['coin_overrides']['ETH']['parameters']['indicator_filter'] = coin_filter
    cfg['polymarket']['overrides']['ETH:300'] = {'indicator_filter': None,
                                                  'disabled': True}
    parent, effective = market_editor_rules(cfg, 'ETH', 300)
    assert parent['indicator_filter'] == coin_filter
    assert effective['indicator_filter'] is None
    assert effective['disabled'] is True
    assert market_editor_rules(cfg, 'ETH', 900)[1]['indicator_filter'] == coin_filter
    assert validate_market_override(cfg, 'ETH', 300, effective) == {
        'indicator_filter': None, 'disabled': True}
    with pytest.raises(ValueError):
        cfg['polymarket']['overrides']['ETH:300'] = {'unknown_field': 1}
        market_editor_rules(cfg, 'ETH', 300)


@pytest.mark.parametrize('seed', range(20))
def test_randomized_filter_inheritance_market_ask_and_timing_roundtrip(seed, tmp_path):
    import random
    from bcc.config import condition, group, merge
    rng = random.Random(903710 + seed)
    cfg = _draft()
    symbol = rng.choice(cfg['symbols'])
    duration = rng.choice(cfg['polymarket']['durations'])
    global_filter = group(condition('RSI', rng.randrange(30, 65), '<', 1))
    market_filter = group(condition('EMA', rng.randrange(8, 25), '>', 1))
    cfg['polymarket']['indicator_filter'] = global_filter
    cfg['polymarket']['overrides'][f'{symbol}:{duration}'] = {
        'indicator_filter': market_filter,
    }
    original = deepcopy(cfg)
    parent, current = market_editor_rules(cfg, symbol, duration)
    assert current['indicator_filter'] == market_filter
    proposed = dict(current, ask_min=round(rng.uniform(.2, .5), 4),
                    entry_seconds_before_close=rng.randrange(30, 36))
    patch = validate_market_override(cfg, symbol, duration, proposed)
    assert merge(parent, patch)['indicator_filter'] == market_filter
    assert cfg == original
    cfg['polymarket']['overrides'][f'{symbol}:{duration}'] = patch
    repo = Repository(tmp_path)
    folder = repo.create(cfg)
    restored, version = repo.load(folder)
    assert market_editor_rules(restored, symbol, duration)[1]['indicator_filter'] == market_filter
    assert market_editor_rules(restored, symbol, duration)[1]['ask_min'] == proposed['ask_min']
    reset = market_editor_rules(restored, symbol, duration)[0]
    restored['polymarket']['overrides'].pop(f'{symbol}:{duration}')
    repo.save(folder, restored, version)
    final, _ = repo.load(folder)
    assert market_editor_rules(final, symbol, duration)[1]['indicator_filter'] == reset['indicator_filter']
