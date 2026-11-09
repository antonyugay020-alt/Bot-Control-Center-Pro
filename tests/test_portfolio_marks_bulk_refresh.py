"""Scale mark-price independently of per-position execution depth.

Strictly synthetic Bybit public responses; no trading or external requests.
"""
from copy import deepcopy
from contextlib import contextmanager
import time
import pytest

from bcc.market import PublicData, DataUnavailable
from bcc.market_clock import observed_quote, quote_is_recent
from bcc.portfolio_marks import commit_portfolio_marks
from bcc.storage import Store
from bcc.margin import MODEL, margin_status, parse_tiers
from test_margin_finance import opened, raw_tiers


def test_one_ticker_packet_prices_more_than_hundred_positions(monkeypatch):
    source = PublicData()
    names = [f"COIN{i}USDT" for i in range(121)]
    calls = []
    now = time.time()
    def stub(url, params, ttl):
        calls.append((url, params, ttl))
        return {"retCode": 0, "time": int(now * 1000),
                "result": {"list": [{"symbol": name, "markPrice": "100"}
                                     for name in names]}}
    monkeypatch.setattr('bcc.market.clock.ensure', lambda: None)
    monkeypatch.setattr('bcc.market.book_is_fresh', lambda _: True)
    monkeypatch.setattr(source, 'get', stub)
    try:
        marks = source.portfolio_marks(names)
        assert len(marks) == 121
        assert all(quote_is_recent(v) for v in marks.values())
        assert len(calls) == 1
        assert calls[0][0].endswith('/v5/market/tickers')
        assert calls[0][1] == {'category': 'linear'}
    finally:
        source.close()


@pytest.mark.parametrize('bad', ['missing', 'duplicate', 'nan', 'negative'])
def test_packet_never_uses_guessed_price(monkeypatch, bad):
    source = PublicData()
    rows = [{"symbol": "BTCUSDT", "markPrice": "100"}]
    if bad == 'missing': rows = []
    if bad == 'duplicate': rows *= 2
    if bad == 'nan': rows[0]['markPrice'] = 'nan'
    if bad == 'negative': rows[0]['markPrice'] = '-1'
    monkeypatch.setattr('bcc.market.clock.ensure', lambda: None)
    monkeypatch.setattr('bcc.market.book_is_fresh', lambda _: True)
    monkeypatch.setattr(source, 'get', lambda *_a, **_k: {
        'retCode': 0, 'time': int(time.time()*1000),
        'result': {'list': rows}})
    try:
        with pytest.raises(DataUnavailable):
            source.portfolio_marks(['BTCUSDT'])
    finally:
        source.close()


def test_bulk_refresh_reopens_margin_admission_for_60_exposures(tmp_path):
    from bcc.storage import initial_state
    from bcc.market_clock import observed_quote
    from bcc.margin import parse_tiers
    state, _, _ = opened(mode='CROSS', capital=500000)
    sample = next(iter(state['positions'].values()))
    start = time.time()
    for i in range(60):
        symbol = f'COIN{i}USDT'
        entry = deepcopy(sample)
        entry['symbol'] = symbol
        state['positions'][f'id{i}'] = entry
        state['margin_data'][symbol] = {
            'symbol':symbol, 'tiers':parse_tiers(raw_tiers(symbol), symbol),
            'quote': observed_quote(dict(bid=100,ask=100,at=start-10)),
            'risk_at':start,
        }
    assert margin_status(state)
    # A ledger with 61 externally fabricated positions is intentionally not
    # treated as a real PAPER account. Exercise the isolated mark updater on
    # its exact accounting-state shape without forging execution receipts.
    class StateStore:
        def __init__(self, current): self.current = current
        @contextmanager
        def transaction(self, include_charts=False):
            yield self.current, []
        def read(self, include_charts=False): return self.current
    store=StateStore(state)
    marks = {f'COIN{i}USDT':observed_quote(dict(bid=101,ask=101,at=time.time()))
             for i in range(60)}
    marks['BTCUSDT']=observed_quote(dict(bid=101,ask=101,at=time.time()))
    assert commit_portfolio_marks(store,marks)==61
    refreshed=store.read(include_charts=False)
    assert margin_status(refreshed)==''
    assert len(refreshed['positions'])==61


def test_delayed_or_partial_packet_cannot_authorize_fresh_collateral(tmp_path):
    state,_,_=opened(mode='CROSS')
    class StateStore:
        def __init__(self,current):self.current=current
        @contextmanager
        def transaction(self,include_charts=False): yield self.current, []
        def read(self,include_charts=False):return self.current
    store=StateStore(state)
    before=store.read(include_charts=False)['margin_data']['BTCUSDT']['quote']['at']
    assert commit_portfolio_marks(store,{})==0
    assert commit_portfolio_marks(store,{
        'BTCUSDT':observed_quote(dict(bid=200,ask=200,at=time.time()-15))})==0
    after=store.read(include_charts=False)['margin_data']['BTCUSDT']['quote']['at']
    assert before==after
