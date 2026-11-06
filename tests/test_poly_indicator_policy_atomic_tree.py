"""Full executable-filter replacement through all constructor inheritance layers.

These are read-only PAPER constructor tests. No worker, market connection or
live account state is created.
"""
from copy import deepcopy
import random

import pytest

from bcc.config import (condition, defaults, group, merge_poly_rule,
                        difference_poly_rule, resolve_poly_market)
from bcc.constructor_contract import (
    market_editor_rules, project_poly_policy_transaction,
    validate_coin_policy, validate_market_override, validate_constructor_roundtrip)
from bcc.storage import Repository


def policy():
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Lossless nested indicator filters'
    cfg['symbols'] = ['BTC', 'ETH']
    cfg['polymarket']['durations'] = [300, 900]
    cfg['polymarket']['entry_signal_mode'] = 'before_close'
    cfg['polymarket']['entry_seconds_before_close'] = 40
    cfg['polymarket']['entry_window_seconds'] = 10
    return cfg


def tree_with_nested_group():
    return {'operator': 'AND', 'conditions': [condition('RSI', 40, '>', 1)],
            'groups': [group(condition('EMA', 0, '>', 1))]}


def tree_without_nested_group():
    return group(condition('RSI', 60, '<', 2))


def test_global_replacement_removes_nested_group_without_resurrection(tmp_path):
    cfg = policy()
    cfg['polymarket']['indicator_filter'] = tree_with_nested_group()
    original = deepcopy(cfg)
    replacement = tree_without_nested_group()
    updated = project_poly_policy_transaction(cfg, global_rule={'indicator_filter': replacement})
    assert cfg == original
    assert resolve_poly_market(updated, 'BTC', 300)['indicator_filter'] == replacement
    assert 'groups' not in resolve_poly_market(updated, 'BTC', 300)['indicator_filter']
    assert validate_constructor_roundtrip(updated) == updated
    repo = Repository(tmp_path)
    folder = repo.create(updated)
    loaded, _ = repo.load(folder)
    assert resolve_poly_market(loaded, 'ETH', 900)['indicator_filter'] == replacement


def test_coin_replacement_removes_parent_nested_group_and_survives_market_reopen():
    cfg = policy()
    cfg['polymarket']['indicator_filter'] = tree_with_nested_group()
    replacement = tree_without_nested_group()
    patch = validate_coin_policy(cfg, 'ETH', {'indicator_filter': replacement})
    assert patch['indicator_filter'] == replacement
    staged = project_poly_policy_transaction(cfg, coin_rules={'ETH': {'indicator_filter': replacement}})
    assert staged['coin_overrides']['ETH']['parameters']['indicator_filter'] == replacement
    for duration in (300, 900):
        inherited, current = market_editor_rules(staged, 'ETH', duration)
        assert inherited['indicator_filter'] == replacement
        assert current['indicator_filter'] == replacement
    assert resolve_poly_market(staged, 'BTC', 300)['indicator_filter'] == tree_with_nested_group()


def test_market_replacement_removes_inherited_nested_group_and_roundtrips(tmp_path):
    cfg = policy()
    cfg['polymarket']['indicator_filter'] = tree_with_nested_group()
    replacement = tree_without_nested_group()
    patch = validate_market_override(cfg, 'ETH', 300, {'indicator_filter': replacement})
    assert patch['indicator_filter'] == replacement
    staged = project_poly_policy_transaction(cfg, market_rules={'ETH:300': {'indicator_filter': replacement}})
    assert staged['polymarket']['overrides']['ETH:300']['indicator_filter'] == replacement
    assert resolve_poly_market(staged, 'ETH', 300)['indicator_filter'] == replacement
    assert resolve_poly_market(staged, 'ETH', 900)['indicator_filter'] == tree_with_nested_group()
    repo = Repository(tmp_path)
    folder = repo.create(staged)
    restored, version = repo.load(folder)
    assert market_editor_rules(restored, 'ETH', 300)[1]['indicator_filter'] == replacement
    # A later unrelated timing edit must not resurrect the removed group.
    later = project_poly_policy_transaction(restored, market_rules={
        'ETH:300': {'indicator_filter': replacement, 'entry_seconds_before_close': 32}})
    repo.save(folder, later, version)
    final, _ = repo.load(folder)
    assert resolve_poly_market(final, 'ETH', 300)['indicator_filter'] == replacement
    assert resolve_poly_market(final, 'ETH', 300)['entry_seconds_before_close'] == 32


def test_reset_filter_to_inherited_and_disable_filter_explicitly():
    cfg = policy()
    cfg['polymarket']['indicator_filter'] = tree_with_nested_group()
    replacement = tree_without_nested_group()
    staged = project_poly_policy_transaction(cfg, market_rules={'BTC:300': {'indicator_filter': replacement}})
    reset = project_poly_policy_transaction(staged, market_rules={'BTC:300': {}})
    assert resolve_poly_market(reset, 'BTC', 300)['indicator_filter'] == tree_with_nested_group()
    disabled = project_poly_policy_transaction(staged, market_rules={'BTC:300': {'indicator_filter': None}})
    assert resolve_poly_market(disabled, 'BTC', 300)['indicator_filter'] is None
    assert disabled['polymarket']['overrides']['BTC:300']['indicator_filter'] is None


@pytest.mark.parametrize('seed', range(30))
def test_random_nested_group_removal_never_changes_untouched_market(seed):
    rng = random.Random(721910 + seed)
    cfg = policy()
    original = tree_with_nested_group()
    cfg['polymarket']['indicator_filter'] = original
    sym = rng.choice(cfg['symbols'])
    duration = rng.choice(cfg['polymarket']['durations'])
    replacement = group(condition('RSI', rng.randint(20, 80), '<', rng.choice((1, 2, 5))))
    staged = project_poly_policy_transaction(cfg, market_rules={
        f'{sym}:{duration}': {'indicator_filter': replacement,
                             'entry_seconds_before_close': rng.randint(20, 39)}})
    assert resolve_poly_market(staged, sym, duration)['indicator_filter'] == replacement
    for other in cfg['symbols']:
        for other_duration in cfg['polymarket']['durations']:
            if (other, other_duration) != (sym, duration):
                assert resolve_poly_market(staged, other, other_duration)['indicator_filter'] == original
    assert validate_constructor_roundtrip(staged) == staged
