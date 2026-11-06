"""Lossless, non-trading constructor draft persistence and guarded reopen."""

from copy import deepcopy
import json
import random

import pytest

from bcc.config import defaults
from bcc.constructor_session import load_constructor_draft, save_constructor_draft
from bcc.constructor_contract import validate_constructor_roundtrip
from bcc.storage import Repository


def draft(strategy):
    cfg = defaults(strategy)
    cfg['name'] = ''  # an unfinished form must still be recoverable
    if strategy == 'POLYMARKET':
        cfg['symbols'] = ['BTC', 'ETH', 'SOL']
        cfg['polymarket']['durations'] = [300, 900]
        cfg['polymarket']['entry_signal_mode'] = 'before_close'
        cfg['polymarket']['entry_seconds_before_close'] = 80
        cfg['coin_overrides']['ETH'] = {'parameters': {
            'entry_seconds_before_close': 65,
            'entry_window_seconds': 12,
        }}
        cfg['polymarket']['overrides']['ETH:300'] = {
            'entry_seconds_before_close': 40,
            'entry_window_seconds': 8,
        }
    else:
        cfg['symbols'] = ['BTCUSDT', 'ETHUSDT']
        section = cfg['sections'][0]['id']
        cfg['coin_overrides']['BTCUSDT'] = {'sections': {
            section: {'entry': deepcopy(cfg['sections'][0]['entry'])}
        }}
        cfg['coin_overrides']['BTCUSDT']['sections'][section]['entry']['conditions'][0]['timeframe'] = 5
    return cfg


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
@pytest.mark.parametrize('seed', range(35))
def test_random_unfinished_drafts_reopen_without_losing_individual_rules(tmp_path, strategy, seed):
    rng = random.Random(20261105_2338 + seed)
    cfg = draft(strategy)
    if strategy == 'POLYMARKET':
        cfg['polymarket']['entry_seconds_before_close'] = rng.randint(70, 150)
        cfg['polymarket']['overrides']['ETH:300']['entry_seconds_before_close'] = rng.randint(20, 60)
        cfg['polymarket']['overrides']['ETH:300']['ask_min'] = round(rng.uniform(.1, .3), 4)
    else:
        cfg['settings']['orders']['count'] = rng.randint(2, 18)
        cfg['settings']['leverage'] = rng.randint(1, 15)
        cfg['sections'][0]['exit']['conditions'][0]['timeframe'] = rng.choice([1, 2, 5, 10, 15, 30])
    before = deepcopy(cfg)
    path = tmp_path / ('draft_' + strategy + '.json')
    saved = save_constructor_draft(path, cfg)
    assert saved == before
    assert cfg == before
    assert load_constructor_draft(path, expected_strategy=strategy) == before
    # Nothing has been published into MY_BOTS by saving a draft.
    assert not (tmp_path / 'MY_BOTS').exists()
    # Reopen the same form, complete the name and save through the real repo.
    saved['name'] = 'Новый бот'
    complete = validate_constructor_roundtrip(saved)
    repo = Repository(tmp_path)
    folder = repo.create(complete)
    reopened, _ = repo.load(folder)
    assert reopened == complete
    assert path.exists()  # a draft is not implicitly deleted by repository


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
def test_incomplete_invalid_trading_value_is_not_misreported_as_committed(tmp_path, strategy):
    cfg = draft(strategy)
    cfg['settings']['leverage'] = -999  # allowed in an unfinished draft only
    path = tmp_path / 'draft.json'
    save_constructor_draft(path, cfg)
    assert load_constructor_draft(path) == cfg
    cfg['name'] = 'Completed'
    with pytest.raises(ValueError):
        Repository(tmp_path).create(cfg)
    assert path.is_file()


def test_cross_strategy_reopen_never_overwrites_original(tmp_path):
    path = tmp_path / 'draft_POLYMARKET.json'
    original = draft('VELES')
    save_constructor_draft(path, original)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='другой стратегии'):
        load_constructor_draft(path, expected_strategy='POLYMARKET')
    assert path.read_bytes() == before


@pytest.mark.parametrize('corruption', [
    {'settings': None},
    {'sections': None},
    {'coin_overrides': 'wrong'},
    {'symbols': [123]},
    {'polymarket': {'durations': '300', 'overrides': {}}},
    {'polymarket': {'durations': [300], 'overrides': []}},
    {'api_key': 'private'},
])
def test_invalid_draft_never_replaces_existing_file(tmp_path, corruption):
    path = tmp_path / 'draft.json'
    old = draft('POLYMARKET')
    save_constructor_draft(path, old)
    before = path.read_bytes()
    cfg = draft('POLYMARKET')
    cfg.update(corruption)
    with pytest.raises((ValueError, TypeError)):
        save_constructor_draft(path, cfg)
    assert path.read_bytes() == before
    assert load_constructor_draft(path) == old


def test_corrupted_json_preserved_for_manual_recovery(tmp_path):
    path = tmp_path / 'draft.json'
    path.write_text('{ "name": ', encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        load_constructor_draft(path)
    assert path.read_bytes() == before


def test_draft_serialization_keeps_nested_indicator_filters(tmp_path):
    cfg = draft('POLYMARKET')
    cfg['polymarket']['overrides']['ETH:300']['indicator_filter'] = {
        'operator': 'AND', 'conditions': [
            {'indicator': 'RSI', 'period': 14, 'timeframe': 5,
             'trigger': 'bar_close', 'comparison': '>', 'value': 50,
             'reference': 'number', 'output': 'value', 'params': {}},
        ],
    }
    path = tmp_path / 'draft.json'
    assert save_constructor_draft(path, cfg) == cfg
    assert load_constructor_draft(path)['polymarket']['overrides']['ETH:300']['indicator_filter'] == cfg['polymarket']['overrides']['ETH:300']['indicator_filter']
