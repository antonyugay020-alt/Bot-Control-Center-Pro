"""Editor can remove/re-add selections without silently retaining hidden rules."""
from copy import deepcopy

from bcc.config import defaults, validate
from bcc.storage import Store, Repository
from bcc.wizard_state import selected_configuration


def test_removed_polymarket_market_and_coin_overrides_pruned_on_save_only():
    config = defaults('POLYMARKET')
    config['name'] = 'Config projection'
    config['symbols'] = ['BTC', 'ETH']
    config['polymarket']['durations'] = [300, 900]
    config['coin_overrides']['ETH'] = {'parameters': {'ask_min': .2}}
    config['polymarket']['overrides'] = {
        'BTC:300': {'entry_seconds_before_close': 45},
        'BTC:900': {'entry_seconds_before_close': 50},
        'ETH:300': {'entry_seconds_before_close': 40},
    }
    old = deepcopy(config)
    config['symbols'] = ['BTC']
    config['polymarket']['durations'] = [300]
    saved, removed = selected_configuration(config)
    validate(saved)
    assert set(saved['polymarket']['overrides']) == {'BTC:300'}
    assert saved['coin_overrides'] == {}
    assert set(removed) == {'монета ETH', 'рынок ETH:300', 'рынок BTC:900'}
    assert old['polymarket']['overrides'] == config['polymarket']['overrides']
    # A re-selected duration or coin still has its original unsaved draft.
    config['symbols'].append('ETH')
    config['polymarket']['durations'].append(900)
    restored, gone = selected_configuration(config)
    assert not gone and restored['polymarket']['overrides'] == old['polymarket']['overrides']


def test_removed_veles_coin_section_preserves_existing_valid_section():
    config = defaults('VELES')
    config['name'] = 'Removed section test'
    config['symbols'] = ['BTCUSDT']
    sid = config['sections'][0]['id']
    config['coin_overrides'] = {'BTCUSDT': {'sections': {
        sid: {'enabled': False}, 'old-section': {'enabled': False},
    }}}
    cleaned, removed = selected_configuration(config)
    assert cleaned['coin_overrides']['BTCUSDT']['sections'] == {sid: {'enabled': False}}
    assert removed == ['секция BTCUSDT/old-section']
    validate(cleaned)


def test_repository_still_rejects_dropping_live_paper_coin(tmp_path):
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Live guard'
    cfg['symbols'] = ['BTC', 'ETH']
    repo = Repository(tmp_path)
    folder = repo.create(cfg)
    store = Store(folder, cfg['capital'])
    # Only the persistence invariant is under test. This entry resembles an
    # in-flight PAPER position and is never submitted to an external venue.
    with store.transaction() as (state, events):
        state['positions']['existing'] = {
            'kind': 'POLYMARKET', 'symbol': 'ETH', 'section': 'ETH:300',
            'side': 'UP', 'token': 'token', 'shares': 5, 'qty': 5,
            'average': .5, 'cost': 2.5, 'fee': 0, 'margin': 0,
            'opened': 0, 'end': 300, 'slug': 'synthetic', 'settings': cfg['settings'],
        }
    edited, digest = repo.load(folder)
    edited['symbols'] = ['BTC']
    projected, _ = selected_configuration(edited)
    import pytest
    with pytest.raises(ValueError, match='Открытая позиция'):
        repo.save(folder, projected, digest)
    assert repo.load(folder)[0]['symbols'] == ['BTC', 'ETH']
