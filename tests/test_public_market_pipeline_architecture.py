"""Deterministic transport/ledger integration tests without network or live bots."""
import time
from contextlib import contextmanager
from copy import deepcopy

import pytest

from bcc.market import DataUnavailable, PublicData
from bcc.market_clock import observed_quote
from bcc.margin import MarginUnavailable, snapshot
from bcc.portfolio_marks import commit_public_risk_tiers
from bcc.public_risk_refresh import PublicRiskRefresher
from test_margin_finance import opened, raw_tiers


def http_transport(monkeypatch, symbol='BTCUSDT'):
    from bcc import market
    monkeypatch.setattr(market.clock, 'ensure', lambda: None)
    monkeypatch.setattr(market, 'check_book_time', lambda *args: None)
    client = PublicData()
    now = time.time()
    calls = []

    def get(url, params=None, ttl=0):
        kind = url.rsplit('/', 1)[-1]
        calls.append((kind, params, ttl))
        if kind == 'instruments-info':
            return {'retCode': 0, 'result': {'list': [{
                'symbol': symbol, 'status': 'Trading',
                'contractType': 'LinearPerpetual', 'quoteCoin': 'USDT',
                'lotSizeFilter': {'qtyStep': '0.01', 'minOrderQty': '0.01',
                                  'minNotionalValue': '5', 'maxMktOrderQty': '20000'},
                'priceFilter': {'tickSize': '0.01'},
            }]}}
        if kind == 'orderbook':
            return {'retCode': 0, 'time': int(now*1000),
                    'result': {'s': symbol, 'ts': int(now*1000),
                               'b': [['99.9', '10000']],
                               'a': [['100.1', '10000']]}}
        if kind == 'risk-limit':
            return {'retCode': 0, 'time': int(now*1000),
                    'result': {'list': raw_tiers(symbol), 'nextPageCursor': ''}}
        if kind == 'tickers':
            return {'retCode': 0, 'time': int(now*1000), 'result': {'list': [
                {'symbol': symbol, 'markPrice': '100'}]}}
        raise AssertionError('unexpected public endpoint ' + url)
    monkeypatch.setattr(client, 'get', get)
    return client, calls


def test_critical_position_quote_is_depth_and_metadata_only(monkeypatch):
    client, calls = http_transport(monkeypatch)
    try:
        for _ in range(19):
            result = client.position_book_fast('BTCUSDT')
            assert result['margin_required']
            assert result['margin_data'] is None
            assert result['margin_marks'] == {}
            assert result['bids'] and result['asks']
        assert len(calls) == 38
        assert set(kind for kind, _p, _ttl in calls) == {'instruments-info', 'orderbook'}
        # In contrast the ordinary entry path MUST still verify both public
        # tier and mark sources, not silently lower the financial standard.
        filled = client.position_orderbook('BTCUSDT')
        assert filled['margin_data']['symbol'] == 'BTCUSDT'
        assert filled['margin_marks']['BTCUSDT']['bid'] == 100
        assert set(k for k,_p,_ttl in calls[-4:]) == {
            'instruments-info', 'risk-limit', 'tickers', 'orderbook'}
    finally:
        client.close()


def test_public_risk_refresh_does_not_depend_on_large_ticker_packet(monkeypatch):
    client, calls = http_transport(monkeypatch)
    try:
        response = client.risk_tiers('BTCUSDT')
        assert response['symbol'] == 'BTCUSDT'
        assert response['tiers']
        assert len(calls) == 1
        assert calls[0][0] == 'risk-limit'
    finally:
        client.close()


class StateStore:
    def __init__(self, current):
        self.current = current

    @contextmanager
    def transaction(self, include_charts=False):
        yield self.current, []


def test_public_risk_tiers_commit_is_durable_but_keeps_market_mark():
    state, _, _ = opened(mode='CROSS')
    state = deepcopy(state)
    original = deepcopy(state['margin_data']['BTCUSDT'])
    store = StateStore(state)
    later = time.time()
    risk = {'symbol':'BTCUSDT', 'tiers': deepcopy(original['tiers']), 'risk_at':later}
    assert commit_public_risk_tiers(store,'BTCUSDT',risk) is True
    assert state['margin_data']['BTCUSDT']['quote'] == original['quote']
    assert commit_public_risk_tiers(store,'BTCUSDT',{**risk,'risk_at':later-20}) is False
    assert state['margin_data']['BTCUSDT']['risk_at'] == later
    assert commit_public_risk_tiers(store,'ETHUSDT',{**risk,'symbol':'ETHUSDT'}) is False


def test_stale_mark_remains_blocked_even_if_tiers_refreshed():
    state, _, _ = opened(mode='CROSS')
    store = StateStore(state)
    state['margin_data']['BTCUSDT']['quote'] = observed_quote({
        'bid': 100, 'ask': 100, 'at': time.time()-20})
    risk = {k: deepcopy(state['margin_data']['BTCUSDT'][k]) for k in ('tiers','risk_at')}
    risk['risk_at'] = time.time()
    risk['symbol'] = 'BTCUSDT'
    assert commit_public_risk_tiers(store,'BTCUSDT',risk)
    with pytest.raises(MarginUnavailable, match='mark-price'):
        snapshot(state,'BTCUSDT')


def test_inconsistent_refresh_never_modifies_existing_risk_terms():
    state, _, _ = opened(mode='CROSS')
    old = deepcopy(state['margin_data']['BTCUSDT'])
    store=StateStore(state)
    risk={'symbol':'BTCUSDT','risk_at':time.time(),
          'tiers':deepcopy(old['tiers'])}
    risk['tiers'][0]['maintenance_rate'] = .9
    with pytest.raises(ValueError, match='Противоречивая'):
        commit_public_risk_tiers(store,'BTCUSDT',risk)
    assert state['margin_data']['BTCUSDT']==old


def test_refresher_background_limits_work_and_never_trades():
    state, _, _ = opened(mode='CROSS')
    store = StateStore(state)
    risk = deepcopy(state['margin_data']['BTCUSDT'])
    calls = []

    class Source:
        def risk_tiers(self, symbol):
            calls.append(symbol)
            return {'symbol':symbol, 'tiers':risk['tiers'], 'risk_at':time.time()}

    source = Source()
    with_ref = PublicRiskRefresher(source,store,workers=1,refresh_after=0.001)
    try:
        state['margin_data']['BTCUSDT']['risk_at'] -= 250
        with_ref.poll(state)
        deadline=time.monotonic()+2
        while with_ref.stats['completed'] == 0 and time.monotonic() < deadline:
            time.sleep(.01)
            with_ref.poll(state)
        assert with_ref.stats['completed'] >= 1
        assert calls
        assert state['margin_data']['BTCUSDT']['quote'] == risk['quote']
        assert not state.get('executions')
    finally:
        with_ref.close()


def test_scheduler_uses_fast_depth_only_for_proven_public_tiers():
    from bcc.scheduler import MarketScheduler
    state, _, _ = opened(mode='CROSS')
    events = []
    now = time.time()
    class Source:
        def position_book_fast(self, symbol):
            events.append('fast')
            return {'bids': [[99, 100]], 'asks': [[101, 100]], 'at': time.time()}
        def position_orderbook(self, symbol):
            events.append('full')
            return {'bids': [[99, 100]], 'asks': [[101, 100]], 'at': time.time()}
        def candles(self, symbol, required):
            return []
        def funding(self, symbol):
            return []

    src=Source()
    scheduler=MarketScheduler(src, {'symbols':['BTCUSDT']}, lambda *_:100,
                              workers=2, period=0.05)
    try:
        for _ in range(10):
            scheduler.collect(state, timeout=.02)
            if 'fast' in events: break
        assert 'fast' in events, events
        assert 'full' not in events
    finally:
        scheduler.close()

    events.clear()
    state['margin_data']['BTCUSDT']['risk_at'] = now-255
    scheduler=MarketScheduler(src, {'symbols':['BTCUSDT']}, lambda *_:100,
                              workers=2, period=0.05)
    try:
        for _ in range(10):
            scheduler.collect(state, timeout=.02)
            if 'full' in events: break
        assert 'full' in events, events
        assert 'fast' not in events, 'Unverified risk tiers may never bypass full proof'
    finally:
        scheduler.close()


def test_risk_refresher_errors_are_bounded_and_do_not_replace_evidence():
    state, _, _ = opened(mode='CROSS')
    original=deepcopy(state['margin_data']['BTCUSDT'])
    state['margin_data']['BTCUSDT']['risk_at'] -= 190
    class Denied:
        def risk_tiers(self, symbol):
            raise DataUnavailable('provider unavailable')
    service=PublicRiskRefresher(Denied(), StateStore(state), workers=1,
                                refresh_after=180, retry_after=30)
    try:
        service.poll(state)
        deadline=time.monotonic()+2
        while service.stats['errors'] == 0 and time.monotonic() < deadline:
            time.sleep(.01)
            service.poll(state)
        assert service.stats['errors'] == 1
        assert service.stats['in_flight'] == 0
        assert state['margin_data']['BTCUSDT']['tiers'] == original['tiers']
        assert not service.poll(state)['in_flight']
    finally:
        service.close()


def test_unverified_financial_model_forces_full_quote():
    from bcc.scheduler import MarketScheduler
    state, _, _ = opened(mode='CROSS')
    state['margin_model'] = None
    calls=[]
    class Source:
        def position_book_fast(self, symbol):
            calls.append('fast')
            return {'at': time.time()}
        def position_orderbook(self, symbol):
            calls.append('full')
            return {'at': time.time(), 'bids':[[99,1]], 'asks':[[101,1]]}
        def candles(self, symbol, required):return []
        def funding(self, symbol):return []
    scheduler=MarketScheduler(Source(), {'symbols':['BTCUSDT']}, lambda *_:100,
                              workers=2, period=.05)
    try:
        for _ in range(8):
            scheduler.collect(state, timeout=.02)
            if calls: break
        assert calls == ['full']
    finally:
        scheduler.close()


def test_position_risk_tier_refresh_preserves_urgent_public_budget(monkeypatch):
    client, calls = http_transport(monkeypatch)
    try:
        witness = []
        original = client.get
        def wrapped(url, params=None, ttl=0):
            witness.append(bool(getattr(client._thread, 'position_mark', False)))
            return original(url, params, ttl)
        monkeypatch.setattr(client,'get',wrapped)
        client.risk_tiers('BTCUSDT')
        assert witness == [False]
        client.position_risk_tiers('BTCUSDT')
        assert witness == [False,True]
        assert not getattr(client._thread,'position_mark',False)
    finally:
        client.close()
