"""Actual PAPER decision stores immutable effective rules and observed facts."""
from copy import deepcopy

from bcc.config import resolve_poly_market, validate
from bcc.storage import Store, Repository
from bcc.worker import process_polymarket
from test_poly_contract_seconds import ScheduledSource, cfg


def test_persisted_polymarket_entry_evidence_survives_edit_copy_restart(tmp_path):
    config = cfg()
    config['symbols'] = ['BTC']
    config['polymarket']['durations'] = [300]
    config['polymarket']['overrides']['BTC:300'] = {
        'entry_seconds_before_close': 45, 'entry_window_seconds': 10,
        'ask_min': .4, 'ask_max': .6,
    }
    config = validate(config)
    repo = Repository(tmp_path)
    folder = repo.create(config)
    store = Store(folder, config['capital'])
    source = ScheduledSource(42)
    with store.transaction() as (state, events):
        process_polymarket(config, state, events, source, source.now)
    opened = next(iter(store.read()['positions'].values()))
    proof = deepcopy(opened['entry_observation'])
    rule = deepcopy(opened['entry_rule'])
    assert rule['ask_min'] == .4 and rule['entry_seconds_before_close'] == 45
    assert proof['observed_ask'] == .5
    assert proof['impulse_usd'] == 100
    assert proof['selected_impulse'] == .5
    assert abs(proof['seconds_before_close'] - 42) < .1
    event = store.history()[0]
    assert event['entry_observation'] == proof
    assert event['applied_entry_rule'] == rule

    updated, digest = repo.load(folder)
    updated['polymarket']['overrides']['BTC:300']['ask_min'] = .3
    updated['polymarket']['entry_seconds_before_close'] = 70
    repo.save(folder, updated, digest)
    assert resolve_poly_market(repo.load(folder)[0], 'BTC', 300)['ask_min'] == .3
    assert Store(folder).read()['positions'][next(iter(store.read()['positions']))]['entry_rule'] == rule
    replica = repo.copy(folder, history=True)
    snapshot = Store(replica)
    snapshot.audit()
    cloned = next(iter(snapshot.read()['positions'].values()))
    assert cloned['entry_observation'] == proof
    assert cloned['entry_rule'] == rule
    assert snapshot.history()[0]['entry_observation'] == proof


def test_evidence_does_not_contain_account_secrets(tmp_path):
    config = validate(cfg())
    config['symbols'] = ['BTC']
    config['polymarket']['durations'] = [300]
    state = __import__('bcc.storage', fromlist=['initial_state']).initial_state(500)
    events = []
    source = ScheduledSource(40)
    process_polymarket(config, state, events, source, source.now)
    observation = next(iter(state['positions'].values()))['entry_observation']
    assert set(observation) == {
        'impulse_usd', 'impulse_pct', 'selected_impulse', 'observed_ask',
        'book_at', 'entry_ts_utc', 'seconds_into_market',
        'seconds_before_close', 'market_start', 'market_end',
    }
