"""Actual Hub/cache QA reads cannot age one channel by waiting for another."""
import time

import pytest

from bcc import market_clock
from bcc.market import SourceAccessDenied
from tools import bybit_ws_live_acceptance as qa
from test_ws_portfolio_observations import portfolio


@pytest.fixture
def sample(portfolio,monkeypatch):
    symbols,reader,hub,quotes=portfolio
    now=[time.time()+2.7]
    monkeypatch.setattr(market_clock,'utc_bounds',lambda:(now[0],now[0]+.02))
    monkeypatch.setattr(quotes,'_wall',lambda:now[0])
    monkeypatch.setattr(hub.source._orderbook_stream,'_wall',lambda:now[0])
    # The fixture's clock has no network references; this test explicitly
    # supplies the vetted UTC bounds, without changing system/cache wall time.
    monkeypatch.setattr(qa,'clock',market_clock.clock)
    symbol=symbols[0]
    def publish():
        stamp=int(now[0]*1000)
        assert quotes.ingest({'topic':'tickers.'+symbol,'type':'snapshot','ts':stamp,
            'data':{'symbol':symbol,'bid1Price':'100.4','ask1Price':'100.5','markPrice':'100.45'}},generation=quotes._generation)
        assert hub.publish_ws_event('tickers',symbol)
        books=hub.source._orderbook_stream
        assert books.ingest({'topic':'orderbook.50.'+symbol,'type':'snapshot','ts':stamp,
            'data':{'s':symbol,'u':2,'seq':2,'b':[['100.4','1000']],'a':[['100.5','1000']]}},generation=books._generation)
        assert hub.publish_ws_event('depth50',symbol)
    return symbol,reader,now,publish


def test_delayed_second_scalar_depth_read_cannot_expire_the_already_read_mark(sample,monkeypatch):
    symbol,reader,now,publish=sample
    gets=[];original=reader.get
    def get(url,params=None,**kwargs):
        if url.endswith('/orderbook'):
            gets.append(1)
            if len(gets)==2:
                now[0]+=.4;publish()
        return original(url,params,**kwargs)
    monkeypatch.setattr(reader,'get',get)
    result=qa.probe(reader,symbol,require_mark=True)
    assert result['status']=='PASS',result
    assert len(gets)==1 and result['snapshot_attempts']==1
    assert result['mark_age_seconds']<3 and result['age_upper_seconds']<3


def test_expiry_during_cache_read_requires_a_new_real_pair_not_timestamp_reuse(sample,monkeypatch):
    symbol,reader,now,publish=sample
    reads=[];original=reader.shared.read_fresh_many
    def read(*args,**kwargs):
        if reads:publish()
        values=original(*args,**kwargs);reads.append(values)
        if len(reads)==1:now[0]+=.4
        return values
    monkeypatch.setattr(reader.shared,'read_fresh_many',read)
    result=qa.probe(reader,symbol,require_mark=True)
    assert result['status']=='PASS',result
    assert result['snapshot_attempts']==len(reads)==2
    assert result['mark_age_seconds']<.1 and result['age_upper_seconds']<.1
    assert reads[1][1]['_bcc_ws_mark_at']>reads[0][1]['_bcc_ws_mark_at']
    assert reads[1][0]['result']['ts']>reads[0][0]['result']['ts']


@pytest.mark.parametrize('damage',['missing','rest_mark','wrong_symbol','old_mark','old_book','cache_busy'])
def test_unrepaired_pair_never_passes_or_waits_past_the_overall_budget(sample,monkeypatch,damage):
    symbol,reader,now,_=sample
    original=reader.shared.read_fresh_many
    from bcc.data_cache import CacheBusy
    def read(*args,**kwargs):
        value,ticker=original(*args,**kwargs)
        if damage=='missing':ticker=None
        if damage=='rest_mark':ticker['_bcc_ws_mark']=False
        if damage=='wrong_symbol':ticker['result']['list'][0]['symbol']='OTHERUSDT'
        if damage=='old_mark':ticker['_bcc_ws_mark_at']-=10000
        if damage=='old_book':value['result']['ts']-=10000
        if damage=='cache_busy':raise CacheBusy('held cache reader')
        return [value,ticker]
    monkeypatch.setattr(reader.shared,'read_fresh_many',read)
    monkeypatch.setattr(qa,'STEADY_READ_BUDGET',.15)
    started=time.monotonic();result=qa.probe(reader,symbol,require_mark=True)
    assert result['status']=='FAIL' and result['snapshot_attempts']>=2,result
    assert 'mark_price_verified' not in result
    assert time.monotonic()-started<.5


def test_local_provider_denial_cannot_be_retried_as_missing_pair(sample,monkeypatch):
    symbol,reader,_,_=sample
    checks=[]
    def denied(host):checks.append(1);raise SourceAccessDenied(host,451)
    # Preserve initial scalar registration to target the pair acquisition fence.
    pair=qa._sample_pair
    def acquire(source,symbol,row):
        monkeypatch.setattr(source,'_assert_provider_allowed',denied)
        return pair(source,symbol,row)
    monkeypatch.setattr(qa,'_sample_pair',acquire)
    result=qa.probe(reader,symbol,require_mark=True)
    assert result['status']=='FAIL' and 'HTTP 451' in result['error']
    assert checks==[1] and result['snapshot_attempts']==1
