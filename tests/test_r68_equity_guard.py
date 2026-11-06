"""Extra audit invariants: unpriced positions cannot authorize new PAPER risk."""
from copy import deepcopy
from bcc.storage import initial_state
from bcc.trading import equity
from bcc.strategies import capital_margin, daily_ok
from bcc.worker import process_polymarket
from test_poly_contract_seconds import ScheduledSource, cfg


def unresolved_account():
    state = initial_state(500)
    state['positions']['prior'] = {
        'kind':'POLYMARKET', 'symbol':'ETH', 'token':'prior-token',
        'cost':250., 'shares':500., 'side':'UP', 'section':'ETH:300',
        'end':9e12, 'fee':0., 'slug':'prior', 'settings': {},
    }
    state['cash'] = 250.
    assert equity(state)[1] is True
    return state


def test_reinvest_sizing_fails_closed_when_unpriced():
    state = unresolved_account()
    settings = {'allocation_pct':10}
    assert capital_margin({'strategy':'GRID', 'reinvest':True, 'capital':500}, state, settings) == 0


def test_daily_admission_fails_closed_when_unknown_equity():
    state = unresolved_account()
    settings = {'daily_loss_pct':15}
    assert not daily_ok({'strategy':'POLYMARKET', 'capital':500}, state, 'BTC', settings, 0)


def test_polymarket_reinvest_no_new_buy_without_valuation():
    state = unresolved_account()
    before = deepcopy(state)
    config = cfg()
    config['symbols'] = ['BTC']
    config['polymarket']['durations'] = [300]
    config['reinvest'] = True
    source = ScheduledSource(42)
    from bcc.market import DataUnavailable
    source.outcome_quote = lambda token: (_ for _ in ()).throw(DataUnavailable("no quote"))
    events = []
    process_polymarket(config, state, events, source, source.now)
    assert not any(e['action']=='OPEN' for e in events)
    assert state['cash'] == before['cash']
    assert set(state['positions']) == set(before['positions'])


def test_polymarket_confirmed_fresh_valuation_still_allows_multi_market():
    import time
    from bcc.market_clock import observed_quote
    state = unresolved_account()
    config = cfg()
    config['symbols'] = ['BTC']
    config['polymarket']['durations'] = [300]
    config['reinvest'] = True
    source = ScheduledSource(42)
    source.outcome_quote = lambda token: observed_quote({
        'bid':0.5, 'ask':0.52, 'at':time.time(),
    })
    events = []
    process_polymarket(config, state, events, source, source.now)
    assert any(e['action']=='OPEN' for e in events), state['diagnostics']
