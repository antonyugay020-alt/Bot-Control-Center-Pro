"""Independent multi-layer constructor acceptance without Qt or orders."""
from copy import deepcopy
from pathlib import Path
import random

import pytest

from bcc.config import defaults, range_section, resolve_poly_market, effective
from bcc.constructor_transaction import (project_constructor_transaction as stage,
                                         validate_constructor_publish as publish)
from bcc.constructor_session import (save_constructor_draft_with_revision,
                                     load_constructor_draft_snapshot)
from bcc.storage import Repository


def sample(strategy):
    c = defaults(strategy)
    c['name'] = 'Integrated multi-layer constructor'
    c['symbols'] = ['BTC', 'ETH', 'SOL'] if strategy == 'POLYMARKET' else [
        'BTCUSDT', 'ETHUSDT', 'SOLUSDT']
    return c


def test_combined_market_and_coin_timing_change_is_atomic(tmp_path):
    cfg = sample('POLYMARKET')
    cfg['polymarket'].update(entry_signal_mode='before_close',
                             entry_seconds_before_close=60,
                             entry_window_seconds=15,
                             entry_min_seconds_left=5)
    cfg['polymarket']['overrides']['BTC:300'] = {
        'entry_seconds_before_close': 30, 'entry_window_seconds': 10}
    before = deepcopy(cfg)
    with pytest.raises(ValueError):
        stage(cfg, poly_global={'entry_min_seconds_left': 40})
    result = stage(cfg, poly_global={'entry_min_seconds_left': 40,
                                     'entry_seconds_before_close': 100},
                   coin_rules={'ETH': {'parameters': {'entry_seconds_before_close': 90}}},
                   market_rules={'BTC:300': {'entry_seconds_before_close': 85,
                                             'entry_window_seconds': 10}})
    assert cfg == before
    assert resolve_poly_market(result, 'BTC', 300)['entry_seconds_before_close'] == 85
    assert resolve_poly_market(result, 'BTC', 300)['entry_min_seconds_left'] == 40
    assert resolve_poly_market(result, 'ETH', 900)['entry_seconds_before_close'] == 90
    assert publish(result) == result
    repo = Repository(tmp_path)
    folder = repo.create(publish(result))
    loaded, digest = repo.load(folder)
    assert loaded == result
    result2 = stage(loaded, market_rules={'BTC:300': {}})
    repo.save(folder, publish(result2), digest)
    assert 'BTC:300' not in repo.load(folder)[0]['polymarket']['overrides']


def test_combined_veles_sections_risk_and_coin_edits(tmp_path):
    cfg = sample('VELES')
    sid = cfg['sections'][0]['id']
    cfg['coin_overrides']['SOLUSDT'] = {'sections': {sid: {'enabled': False}}}
    snapshot = deepcopy(cfg)
    sections = deepcopy(cfg['sections'])
    sections[0]['id'] = 'renamed_range'
    settings = deepcopy(cfg['settings'])
    settings['leverage'] = 7.0
    # A sections-only edit leaves the old section override dormant in the
    # workspace, but it is excluded from executable policy on publication.
    temporary = stage(cfg, sections=sections)
    assert sid in temporary['coin_overrides']['SOLUSDT']['sections']
    assert sid not in publish(temporary)['coin_overrides']['SOLUSDT'].get('sections', {})
    staged = stage(cfg, sections=sections, settings=settings,
                   coin_rules={'SOLUSDT': {'sections': {'renamed_range': {'enabled': False}}}})
    assert cfg == snapshot
    assert staged['sections'][0]['id'] == 'renamed_range'
    assert effective(staged, 'BTCUSDT', sections[0])['leverage'] == 7.0
    assert publish(staged) == staged
    repo = Repository(tmp_path)
    folder = repo.create(publish(staged))
    assert repo.load(folder)[0] == staged


def test_dormant_rules_survive_draft_but_never_publish(tmp_path):
    cfg = sample('POLYMARKET')
    cfg['polymarket'].update(entry_signal_mode='before_close',
                             entry_seconds_before_close=60,
                             entry_window_seconds=12, entry_min_seconds_left=5)
    cfg['polymarket']['overrides']['SOL:3600'] = {'entry_seconds_before_close': 300}
    cfg['coin_overrides']['SOL'] = {'parameters': {'stake_pct': 8.0}}
    staged = stage(cfg, symbols=['BTC', 'ETH'], durations=[300, 900])
    assert 'SOL:3600' in staged['polymarket']['overrides']
    assert 'SOL' in staged['coin_overrides']
    executable = publish(staged)
    assert 'SOL:3600' not in executable['polymarket']['overrides']
    assert 'SOL' not in executable['coin_overrides']
    draft = tmp_path / 'workspace.json'
    _, revision = save_constructor_draft_with_revision(draft, staged, expected_revision=None)
    reopened, check = load_constructor_draft_snapshot(draft, expected_strategy='POLYMARKET')
    assert revision == check and reopened == staged
    restored = stage(reopened, symbols=['BTC', 'ETH', 'SOL'], durations=[300, 900, 3600])
    assert resolve_poly_market(publish(restored), 'SOL', 3600)['entry_seconds_before_close'] == 300


def test_unfinished_name_is_allowed_in_staging_but_not_publication():
    cfg = sample('VELES')
    cfg['name'] = ''
    result = stage(cfg, settings=deepcopy(cfg['settings']))
    assert result['name'] == ''
    with pytest.raises(ValueError, match='название'):
        publish(result)


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
@pytest.mark.parametrize('seed', range(60))
def test_randomized_independent_multilevel_transactions(strategy, seed, tmp_path):
    rng = random.Random(202611060001 + seed * 79 + (strategy == 'POLYMARKET'))
    cfg = sample(strategy)
    before = deepcopy(cfg)
    settings = deepcopy(cfg['settings'])
    settings['leverage'] = rng.randint(1, 20)
    sym = rng.choice(cfg['symbols'])
    if strategy == 'VELES':
        sections = deepcopy(cfg['sections'])
        a, b = sorted(rng.sample(range(1, 99), 2))
        section = range_section(a, b, 'LONG', rng.choice([1, 2, 5, 10, 15, 30]),
                                rng.choice([1, 2, 5, 10, 15, 30]))
        section['id'] = f'run25_{seed}'
        sections.append(section)
        staged = stage(cfg, settings=settings, sections=sections,
                       coin_rules={sym: {'settings': {'allocation_pct': rng.randint(1, 20)}}})
        assert staged['sections'][-1]['id'] == section['id']
    else:
        sec = rng.randint(50, 110)
        cutoff = rng.randint(1, 10)
        staged = stage(cfg, settings=settings,
                       poly_global={'entry_signal_mode': 'before_close',
                                    'entry_seconds_before_close': sec,
                                    'entry_window_seconds': rng.randint(1, 15),
                                    'entry_min_seconds_left': cutoff},
                       coin_rules={sym: {'parameters': {'stake_pct': rng.randint(1, 20)}}},
                       market_rules={f'{sym}:300': {'ask_min': round(rng.uniform(.1, .3), 4)}})
        assert resolve_poly_market(staged, sym, 300)['entry_seconds_before_close'] == sec
    assert cfg == before
    executable = publish(staged)
    repo = Repository(tmp_path)
    folder = repo.create(executable)
    loaded, _ = repo.load(folder)
    assert loaded == executable


@pytest.mark.parametrize('kind', ['invalid_duration', 'invalid_seconds', 'unknown_market',
                                   'bad_coin', 'bad_section', 'bad_global_key', 'secret'])
def test_bad_transaction_rejects_without_mutation(kind):
    cfg = sample('POLYMARKET')
    original = deepcopy(cfg)
    cases = {
        'invalid_duration': {'durations': [123]},
        'invalid_seconds': {'poly_global': {'entry_signal_mode': 'before_close',
                                           'entry_seconds_before_close': 999}},
        'unknown_market': {'market_rules': {'ZEC:300': {'ask_min': .2}}},
        'bad_coin': {'coin_rules': {'ETH': {'parameters': {'stake_pct': -1}}}},
        'bad_section': {'sections': []},
        'bad_global_key': {'poly_global': {'private_extra': 1}},
        'secret': {'market_rules': {'BTC:300': {'private_key': 'secret'}}},
    }
    with pytest.raises((ValueError, KeyError, TypeError)):
        stage(cfg, **cases[kind])
    assert cfg == original


def test_ui_commit_uses_same_publish_contract():
    # Source-level wiring guard is useful when Qt cannot be imported on CI.
    source = (Path(__file__).resolve().parents[1] / 'bcc' / 'wizard.py').read_text(encoding='utf-8')
    assert 'workspace = self.data(include_inactive=True)' in source
    assert 'proposed = publish_wizard_configuration(workspace)' in source
    from bcc.constructor_transaction import publish_wizard_configuration, validate_constructor_publish
    from bcc.config import defaults
    for strategy in ('VELES', 'POLYMARKET'):
        cfg = defaults(strategy)
        cfg['name'] = 'Effective-policy publication'
        assert publish_wizard_configuration(cfg) == validate_constructor_publish(cfg)
