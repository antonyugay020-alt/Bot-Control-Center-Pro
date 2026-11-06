"""Atomic multi-level PAPER constructor edits, no Qt/worker/network required."""
from copy import deepcopy
import random
import pytest

from bcc.config import defaults, resolve_poly_market
from bcc.constructor_contract import (project_poly_policy_transaction,
                                      validate_constructor_roundtrip)
from bcc.storage import Repository


def base():
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Batch policy validation'
    cfg['symbols'] = ['BTC', 'ETH', 'SOL']
    cfg['polymarket'].update(durations=[300, 900, 3600],
        entry_signal_mode='before_close', entry_seconds_before_close=100,
        entry_window_seconds=10, entry_min_seconds_left=5, ask_min=.2, ask_max=.95)
    return cfg


def test_batch_global_coin_market_changes_are_atomic_and_persist(tmp_path):
    cfg=base(); untouched=deepcopy(cfg)
    staged=project_poly_policy_transaction(cfg,
        global_rule={'entry_seconds_before_close': 130, 'entry_window_seconds': 20},
        coin_rules={'ETH': {'entry_seconds_before_close': 70, 'entry_window_seconds': 10},
                    'SOL': {'disabled': True, 'ask_min': .7}},
        market_rules={'ETH:300': {'entry_seconds_before_close': 25,
                                  'entry_window_seconds': 7},
                      'BTC:900': {'ask_min': .6}})
    assert cfg == untouched
    assert resolve_poly_market(staged,'BTC',300)['entry_seconds_before_close']==130
    assert resolve_poly_market(staged,'ETH',300)['entry_seconds_before_close']==25
    assert resolve_poly_market(staged,'ETH',900)['entry_seconds_before_close']==70
    assert resolve_poly_market(staged,'SOL',300)['disabled'] is True
    assert resolve_poly_market(staged,'BTC',900)['ask_min']==.6
    assert validate_constructor_roundtrip(staged)==staged
    repo=Repository(tmp_path); folder=repo.create(staged)
    loaded,version=repo.load(folder)
    assert loaded==staged
    updated=project_poly_policy_transaction(loaded,
        coin_rules={'ETH': {}}, market_rules={'ETH:300': {}})
    repo.save(folder,updated,version)
    final,_=repo.load(folder)
    assert resolve_poly_market(final,'ETH',300)['entry_seconds_before_close']==130
    assert 'ETH:300' not in final['polymarket']['overrides']


def test_invalid_combined_edit_cannot_modify_original_or_partial_market():
    cfg=base(); cfg['polymarket']['overrides']['SOL:300']={'ask_min': .9}
    before=deepcopy(cfg)
    with pytest.raises(ValueError,match='SOL:300'):
        project_poly_policy_transaction(cfg,coin_rules={
            'BTC':{'disabled':True}, 'SOL':{'ask_max':.8}})
    assert cfg==before
    with pytest.raises(ValueError,match='Рынок .*:'):
        project_poly_policy_transaction(cfg,global_rule={'entry_min_seconds_left':120},
            market_rules={'ETH:300':{'entry_seconds_before_close':60}})
    assert cfg==before


def test_market_reset_uses_updated_coin_parent_not_stale_patch():
    cfg=base()
    cfg['coin_overrides']['ETH']={'parameters':{'entry_seconds_before_close':60}}
    cfg['polymarket']['overrides']['ETH:300']={'entry_seconds_before_close':30}
    out=project_poly_policy_transaction(cfg,
        coin_rules={'ETH':{'entry_seconds_before_close':75}},
        market_rules={'ETH:300':{}})
    assert 'ETH:300' not in out['polymarket']['overrides']
    assert resolve_poly_market(out,'ETH',300)['entry_seconds_before_close']==75
    assert resolve_poly_market(cfg,'ETH',300)['entry_seconds_before_close']==30


@pytest.mark.parametrize('seed',range(30))
def test_randomized_multi_coin_atomicity_and_roundtrip(seed,tmp_path):
    rng=random.Random(818000+seed); cfg=base()
    coins={}
    for symbol in cfg['symbols']:
        before=rng.randint(30,240)
        coins[symbol]={'entry_seconds_before_close':before,
                       'entry_window_seconds':rng.randint(1,min(15,before-5)),
                       'disabled':bool(rng.getrandbits(1))}
    staged=project_poly_policy_transaction(cfg,coin_rules=coins)
    assert cfg==base()
    assert validate_constructor_roundtrip(staged)==staged
    repo=Repository(tmp_path); folder=repo.create(staged)
    loaded,digest=repo.load(folder)
    assert loaded==staged
    target=rng.choice(cfg['symbols'])
    updated=project_poly_policy_transaction(loaded,
        market_rules={f'{target}:300':{'ask_min':round(rng.uniform(.3,.7),4)}})
    repo.save(folder,updated,digest)
    final,_=repo.load(folder)
    assert final==updated
