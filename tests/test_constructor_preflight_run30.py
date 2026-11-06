"""Independent preflight regression: no workers, accounts, or repository writes."""
from copy import deepcopy
import random

import pytest

from bcc.config import defaults
from bcc.constructor_diagnostics import constructor_preflight
from bcc.constructor_transaction import publish_wizard_configuration


def config(strategy):
    cfg = defaults(strategy)
    cfg['name'] = 'Independent preflight'
    return cfg


@pytest.mark.parametrize('strategy', ('VELES', 'POLYMARKET', 'GRID', 'PUMP_DUMP', 'EMA'))
def test_preflight_matches_publication_and_does_not_mutate(strategy):
    cfg = config(strategy)
    original = deepcopy(cfg)
    report = constructor_preflight(cfg)
    assert report['valid'] and not report['errors']
    assert cfg == original
    assert publish_wizard_configuration(cfg) == cfg
    if strategy == 'POLYMARKET':
        assert report['checked_rules'] == len(cfg['symbols']) * len(cfg['polymarket']['durations'])
    if strategy == 'VELES':
        assert report['checked_rules'] == len(cfg['symbols']) * len(cfg['sections'])


def test_polymarket_reports_multiple_independent_timing_and_ask_defects():
    cfg = config('POLYMARKET')
    cfg['polymarket']['overrides']['BTC:300'] = {
        'entry_signal_mode': 'before_close',
        'entry_seconds_before_close': 420,
    }
    cfg['polymarket']['overrides']['ETH:900'] = {'ask_min': 0.95, 'ask_max': 0.1}
    original = deepcopy(cfg)
    report = constructor_preflight(cfg)
    assert not report['valid']
    assert {'BTC:300', 'ETH:900'} <= {item['scope'] for item in report['errors']}
    assert report['checked_rules'] == 6
    assert cfg == original
    with pytest.raises(ValueError):
        publish_wizard_configuration(cfg)


def test_veles_checks_every_coin_and_range_before_publication():
    cfg = config('VELES')
    cfg['coin_overrides']['BTCUSDT'] = {'settings': {'leverage': 1000}}
    cfg['coin_overrides']['ETHUSDT'] = {'entry': {'operator': 'AND', 'conditions': []}}
    original = deepcopy(cfg)
    report = constructor_preflight(cfg)
    assert not report['valid']
    assert any('BTCUSDT/' in item['scope'] for item in report['errors'])
    assert any('ETHUSDT/' in item['scope'] for item in report['errors'])
    assert cfg == original


def test_dormant_invalid_market_is_not_published_or_mistaken_for_active():
    cfg = config('POLYMARKET')
    cfg['polymarket']['durations'] = [300]
    cfg['polymarket']['overrides']['BTC:900'] = {
        'entry_signal_mode': 'before_close', 'entry_seconds_before_close': 9999}
    original = deepcopy(cfg)
    report = constructor_preflight(cfg)
    assert report['valid']
    assert 'рынок BTC:900' in report['dormant_excluded']
    assert 'BTC:900' in cfg['polymarket']['overrides']
    assert 'BTC:900' not in publish_wizard_configuration(cfg)['polymarket']['overrides']
    assert cfg == original


def test_bounded_error_reporting_fails_closed_without_partial_mutation():
    cfg = config('POLYMARKET')
    cfg['polymarket']['entry_signal_mode'] = 'before_close'
    cfg['polymarket']['entry_seconds_before_close'] = 9999
    original = deepcopy(cfg)
    report = constructor_preflight(cfg, max_errors=2)
    assert not report['valid'] and report['truncated']
    assert len(report['errors']) == 2
    assert cfg == original
    for value in (0, -1, 1.5, 1001):
        with pytest.raises(ValueError):
            constructor_preflight(cfg, max_errors=value)


@pytest.mark.parametrize('seed', range(30))
def test_randomized_polymarket_inherited_seconds_preflight(seed):
    rng = random.Random(30_202_611 + seed)
    cfg = config('POLYMARKET')
    symbol = rng.choice(cfg['symbols'])
    duration = rng.choice(cfg['polymarket']['durations'])
    before = rng.randint(20, duration - 1)
    cutoff = rng.randint(0, before - 1)
    cfg['polymarket']['overrides'][f'{symbol}:{duration}'] = {
        'entry_signal_mode': 'before_close',
        'entry_seconds_before_close': before,
        'entry_window_seconds': rng.randint(1, before - cutoff),
        'entry_min_seconds_left': cutoff,
    }
    report = constructor_preflight(cfg)
    assert report['valid'], report
    assert publish_wizard_configuration(cfg)['polymarket']['overrides'][f'{symbol}:{duration}']['entry_seconds_before_close'] == before
