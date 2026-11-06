"""PAPER indicator admission, closed OHLCV and immutable history; no web I/O."""
from copy import deepcopy
from math import nan

import pytest

from bcc.config import condition, defaults, group, normalize_config, resolve_poly_market, validate
from bcc.market import DataUnavailable, PolymarketData
from bcc.poly_indicators import check_filter, history_needed
from bcc.storage import Store, initial_state
from bcc.worker import process_polymarket
from test_poly_contract_seconds import ScheduledSource


def composite():
    rsi = condition("RSI", 50, ">", 1)
    ema = dict(condition("EMA", 0, "<", 2), reference="price")
    volume = condition("REL_VOLUME", 1.5, ">", 1)
    return group(rsi, ema, volume)


def history(now, amount=300, rising=True, *, spike=True):
    end = int(now // 60) * 60000 - 60000
    rows = []
    for i in range(amount):
        price = 100 + i * (0.05 if rising else -0.05)
        rows.append(dict(t=end - (amount - 1 - i) * 60000,
                         open=price, high=price + 0.06, low=price - 0.06,
                         close=price + 0.03, volume=100 if i == amount - 1 and spike else 1))
    return rows


class FilterSource(ScheduledSource):
    def __init__(self, seconds_left=42, *, rising=True, spike=True):
        super().__init__(seconds_left)
        self.received = []
        self.rising = rising
        self.spike = spike
        self.bad = None

    def indicator_bars(self, coin, required, now):
        self.received.append((coin, required, now))
        if self.bad == 'unavailable':
            raise DataUnavailable("Свечи недоступны")
        rows = history(now, max(required, 180), self.rising, spike=self.spike)
        if self.bad == 'nan':
            rows[-1]['close'] = nan
        return rows


def configured():
    c = defaults('POLYMARKET')
    c.update(name='Optional indicator filter', symbols=['BTC'])
    c['polymarket'].update(durations=[300], entry_signal_mode='before_close',
                           entry_seconds_before_close=45,
                           entry_window_seconds=10, entry_min_seconds_left=5,
                           indicator_filter=composite())
    return validate(c)


def test_legacy_bot_does_not_require_candles_and_not_rewritten():
    cfg = configured()
    cfg['polymarket'].pop('indicator_filter')
    original = deepcopy(cfg)
    normalized = validate(cfg)
    assert cfg == original
    assert normalized['polymarket']['indicator_filter'] is None
    src = ScheduledSource(42)  # Has no method indicator_bars()
    state, events = initial_state(500), []
    process_polymarket(normalized, state, events, src, src.now)
    assert state['positions'] and any(e['action'] == 'OPEN' for e in events)
    assert 'indicator_evidence' not in next(iter(state['positions'].values()))['entry_observation']


def test_filter_resolved_per_coin_and_per_market_without_leaking():
    cfg = defaults('POLYMARKET')
    cfg.update(name='multi', symbols=['BTC', 'ETH'])
    cfg['polymarket']['indicator_filter'] = composite()
    cfg['coin_overrides'] = {'BTC': {'parameters': {'indicator_filter': None}}}
    cfg['polymarket']['overrides'] = {'BTC:900': {'indicator_filter': composite()}}
    before = deepcopy(cfg)
    validated = validate(cfg)
    assert cfg == before
    assert resolve_poly_market(validated, 'BTC', 300)['indicator_filter'] is None
    assert resolve_poly_market(validated, 'BTC', 900)['indicator_filter'] is not None
    assert resolve_poly_market(validated, 'ETH', 900)['indicator_filter'] == composite()


@pytest.mark.parametrize('wrong', [
    dict(condition('MACD', 0, '>', 1), reference='number'),
    dict(condition('RSI', 50, '>', 1), trigger='intrabar'),
    dict(condition('RSI', 50, '>', 30), period=500),
    dict(condition('EMA', 50, '>', 1), reference='indicator', reference_indicator='MACD', reference_period=14),
])
def test_invalid_filter_fails_configuration_before_trading(wrong):
    c = configured()
    c['polymarket']['indicator_filter'] = group(wrong)
    with pytest.raises(ValueError, match='Polymarket'):
        validate(c)


@pytest.mark.parametrize('rising,spike,expected', [
    (True, True, True), (True, False, False), (False, True, False),
])
def test_composite_and_conditions_block_or_allow_real_paper_entry(rising, spike, expected):
    cfg = configured()
    source = FilterSource(rising=rising, spike=spike)
    state, events = initial_state(500), []
    process_polymarket(cfg, state, events, source, source.now)
    assert bool(state['positions']) is expected, state['diagnostics']
    assert source.received and source.received[0][1] == history_needed(composite())
    if expected:
        position = next(iter(state['positions'].values()))
        assert position['entry_observation']['indicator_evidence']['matched']
        assert position['entry_rule']['indicator_filter'] == composite()
        assert next(e for e in events if e['action'] == 'OPEN')['applied_entry_rule'] == position['entry_rule']
    else:
        assert state['cash'] == 500
        assert not events


@pytest.mark.parametrize('bad', ['unavailable', 'nan'])
def test_missing_or_corrupt_indicator_candles_fail_closed(bad):
    cfg = configured()
    feed = FilterSource()
    feed.bad = bad
    state, events = initial_state(500), []
    process_polymarket(cfg, state, events, feed, feed.now)
    assert not state['positions'] and not events and state['cash'] == 500
    assert state['diagnostics']['POLYMARKET']


def test_indicator_evidence_frozen_across_edit_and_restart(tmp_path):
    cfg = configured()
    source = FilterSource()
    store = Store(tmp_path, 500)
    with store.transaction() as (state, events):
        process_polymarket(cfg, state, events, source, source.now)
    original = next(iter(store.read()['positions'].values()))['entry_rule']
    cfg['polymarket']['indicator_filter'] = None
    with Store(tmp_path).transaction() as (state, events):
        process_polymarket(validate(cfg), state, events, source, source.now + 1)
    reread = Store(tmp_path)
    assert next(iter(reread.read()['positions'].values()))['entry_rule'] == original
    assert reread.history()[0]['applied_entry_rule'] == original
    reread.audit()


def test_closed_binance_history_requires_latest_minute_and_no_gaps(monkeypatch):
    feed = PolymarketData()
    now = 1750000000.0
    start = int(now // 60) * 60000 - 180 * 60000
    def rows():
        return [[start + i * 60000, '100','101','99','100','5',
                 start + i * 60000 + 59999] for i in range(180)]
    ok = rows()
    assert ok[-1][0] == int(now // 60) * 60000 - 60000
    monkeypatch.setattr(feed, 'get', lambda url, args=None, ttl=0: ok)
    candles = feed.indicator_bars('BTC', 180, now)
    assert len(candles) == 180 and candles[-1]['t'] == ok[-1][0]
    for altered in (lambda x: x.pop(-2),
                    lambda x: x[-1].__setitem__(6, x[-1][6] + 60000),
                    lambda x: x[-1].__setitem__(5, 'nan')):
        sample = rows()
        altered(sample)
        monkeypatch.setattr(feed, 'get', lambda url, args=None, ttl=0, data=sample: data)
        with pytest.raises(DataUnavailable):
            feed.indicator_bars('BTC', 180, now)


def test_or_group_evaluated_from_actual_closed_candles():
    cfg = configured()
    cfg['polymarket']['indicator_filter'] = group(
        condition('RSI', 10, '<', 1),
        condition('VOLUME', 10, '>', 1),
    )
    cfg['polymarket']['indicator_filter']['operator'] = 'OR'
    cfg = validate(cfg)
    src = FilterSource(spike=True)
    state, events = initial_state(500), []
    process_polymarket(cfg, state, events, src, src.now)
    assert state['positions'] and len(events) == 1
    observed = next(iter(state['positions'].values()))['entry_observation']
    assert observed['indicator_evidence']['matched']
    assert [o['matched'] for o in observed['indicator_evidence']['observations']] == [False, True]


def test_local_market_filter_none_does_not_request_history():
    cfg = configured()
    cfg['polymarket']['overrides']['BTC:300'] = {'indicator_filter': None}
    cfg = validate(cfg)
    src = ScheduledSource(42)  # No method indicator_bars()
    state, events = initial_state(500), []
    process_polymarket(cfg, state, events, src, src.now)
    assert state['positions'] and len(events) == 1


@pytest.mark.parametrize('corrupt', ['stale', 'missing', 'gap'])
def test_worker_rejects_stale_and_partial_indicator_history(corrupt):
    cfg = configured()
    source = FilterSource()
    original = source.indicator_bars
    def corrupt_bars(coin, required, now):
        rows = original(coin, required, now)
        if corrupt == 'stale':
            for row in rows:
                row['t'] -= 60000
        elif corrupt == 'missing':
            rows.pop()
        else:
            rows[-1]['t'] += 60000
        return rows
    source.indicator_bars = corrupt_bars
    state, events = initial_state(500), []
    process_polymarket(cfg, state, events, source, source.now)
    assert not state['positions'] and not events and state['cash'] == 500


def test_closed_rsi_crossover_applies_to_paper_entry_not_just_threshold():
    cfg = configured()
    cfg['polymarket']['indicator_filter'] = group(condition('RSI', 50, 'cross_up', 1))
    cfg = validate(cfg)
    src = FilterSource(rising=False, spike=False)
    def crossing(coin, required, now):
        rows = history(now, max(required, 180), rising=False, spike=False)
        prior = rows[-2]['close']
        rows[-1].update(open=prior, low=prior - 0.1,
                        close=prior + 50, high=prior + 50.1)
        return rows
    src.indicator_bars = crossing
    matched, snapshot = check_filter(cfg['polymarket']['indicator_filter'], src, 'BTC', src.now)
    assert matched is True and snapshot['observations'][0]['matched'] is True
    state, events = initial_state(500), []
    process_polymarket(cfg, state, events, src, src.now)
    assert len(events) == 1 and events[0]['action'] == 'OPEN'
    assert state['positions']

    # The same closes, without an actual crossing, must not trigger a trade.
    src2 = FilterSource(rising=True)
    blocked, observation = check_filter(cfg['polymarket']['indicator_filter'], src2, 'BTC', src2.now)
    assert blocked is False and observation['available']
