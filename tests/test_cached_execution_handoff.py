"""The owner can replace aged handoffs only with admitted genuine WS depth."""
from copy import deepcopy
import time

import pytest

from bcc import market_clock, runtime
from bcc.market import SourceAccessDenied
from bcc.scheduler import MarketTick
from test_ws_portfolio_observations import portfolio
from test_worker_observation_owner import account


def reference():
    wall,mono=time.time(),time.monotonic()
    market_clock.clock._reference=(wall,wall+.001,wall,mono)


def test_cached_execution_rebases_depth_without_any_get_or_retimestamp(portfolio,monkeypatch):
    symbols,reader,_,_=portfolio;reference();symbol=symbols[0]
    old=reader.position_orderbook(symbol);old['at']-=4
    original=deepcopy(old)
    monkeypatch.setattr(reader,'get',lambda *a,**k:pytest.fail('Owner attempted transport'))
    fresh=reader.cached_execution_book(symbol,old)
    assert fresh['at']>old['at'] and fresh['at']<time.time()+.01
    assert fresh['bids'][0]==[100.4,1000.]
    assert fresh['qty_step']==old['qty_step'] and fresh['margin_data']==old['margin_data']
    assert fresh['update_id']==1 and old==original


@pytest.mark.parametrize('fault',['origin','symbol','old','crossed','sort','empty','quantity','nan'])
def test_bad_cached_depth_cannot_repair_a_handoff(portfolio,monkeypatch,fault):
    symbols,reader,_,_=portfolio;reference();symbol=symbols[0]
    old=reader.position_orderbook(symbol);old['at']-=4
    read=reader.shared.read_fresh
    def corrupt(*args):
        value=deepcopy(read(*args))
        if fault=='origin':value.pop('_bcc_ws_book')
        if fault=='symbol':value['result']['s']='OTHERUSDT'
        if fault=='old':value['result']['ts']=int((time.time()-5)*1000)
        if fault=='crossed':value['result']['b'][0][0]='200'
        if fault=='sort':value['result']['b']=[['100','10'],['101','10']]
        if fault=='empty':value['result']['a']=[]
        if fault=='quantity':value['result']['a'][0][1]='0'
        if fault=='nan':value['result']['a'][0][0]='nan'
        return value
    monkeypatch.setattr(reader.shared,'read_fresh',corrupt)
    assert reader.cached_execution_book(symbol,old) is None


@pytest.mark.parametrize('status',[403,451])
def test_cached_handoff_still_obeys_provider_denial(portfolio,status):
    symbols,reader,hub,_=portfolio;reference();old=reader.position_orderbook(symbols[0])
    hub.source.shared.record_http_denial('a'*64,'observed','api.bybit.com',cooldown=90,denial_status=status)
    with pytest.raises(SourceAccessDenied):reader.cached_execution_book(symbols[0],old)


def test_real_worker_consumes_current_cache_for_aged_handoff_without_transport_retry(portfolio,tmp_path,monkeypatch):
    symbols,reader,_,_=portfolio;reference();cfg,store,folder=account(tmp_path/'account',symbols)
    old=reader.position_orderbook(symbols[0]);old['at']-=4
    done=[]
    class Scheduler:
        def __init__(self,*args):pass
        def collect(self,*args):return [(symbols[0],MarketTick(symbols[0],[{}],deepcopy(old),[],{}),None)]
        def retry_tick(self,*args):pytest.fail('Fresh genuine cache depth was ignored')
        def close(self):pass
    class Engine:
        def process(self,*args):done.append(1)
    monkeypatch.setattr(runtime,'MarketScheduler',Scheduler)
    monkeypatch.setattr(reader,'get',lambda *a,**k:pytest.fail('Owner attempted a GET'))
    runtime.run_loop(folder,cfg,store,reader,Engine(),{},lambda:bool(done),lambda *a:100,None)
    assert done==[1] and len(store.history())==20;store.audit()


def test_scanner_handoff_refreshes_candidate_mark_without_rewriting_risk_time(portfolio,monkeypatch):
    symbols,reader,_,_=portfolio;reference();symbol=symbols[0]
    old=reader.position_orderbook(symbol)
    old['at']-=4
    stamp=time.time()-10
    old['margin_data']={'symbol':symbol,'risk_at':stamp,'tiers':[{'id':1}],
                       'quote':{'bid':99.,'ask':99.,'at':stamp}}
    old['margin_marks']={}
    before=deepcopy(old)
    monkeypatch.setattr(reader,'get',lambda *a,**k:pytest.fail('Owner attempted transport'))
    fresh=reader.cached_execution_book(symbol,old)
    assert fresh is not None
    assert fresh['margin_data']['quote']['bid']==100.45
    assert fresh['margin_data']['quote']['at']>stamp
    assert fresh['margin_marks'][symbol]==fresh['margin_data']['quote']
    assert fresh['margin_data']['risk_at']==stamp
    assert fresh['margin_data']['tiers']==before['margin_data']['tiers']
    assert old==before
