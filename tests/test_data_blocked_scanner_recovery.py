"""A fresh-data recovery rechecks scanner intent without replaying an old order."""
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from bcc.config import defaults, range_section
from bcc.market_clock import observed_quote
from bcc.runtime import process_tick
from bcc.scheduler import MarketScheduler, MarketTick
from bcc.storage import Repository, Store
from bcc.strategies import Veles
from bcc.trading import enter, plan
from test_core import attach, book
from test_margin_finance import use_mark


@pytest.mark.parametrize('signal_still_valid',[True,False])
def test_fresh_risk_rechecks_candidate_before_full_828_symbol_scan(tmp_path,monkeypatch,signal_still_valid):
    cfg=defaults('VELES');cfg['name']='isolated retry';cfg['symbols']=[f'C{i}USDT' for i in range(828)]
    cfg['sections']=[range_section(30,40)];cfg['settings']['orders']['count']=1
    held,candidate=cfg['symbols'][:2];section=cfg['sections'][0]['id']
    folder=Repository(tmp_path).create(cfg);store=Store(folder,10000)
    with store.transaction() as (state,events):
        attach(state,held);use_mark(state,symbol=held)
        ok,reason=enter(state,events,held+':'+section,held,'LONG',cfg['settings'],100,25,
                        plan(cfg['settings']['orders'],'LONG',100),section)
        assert ok,reason
        state['positions'][held+':'+section]['exit_signal']=cfg['sections'][0]['exit']
        state['marks'][held]=observed_quote({'bid':99.99,'ask':100.01,'at':time.time()})
        use_mark(state,symbol=candidate);candidate_margin=deepcopy(state['margin_data'][candidate])
        state['margin_data'][held]['risk_at']=time.time()-301
        state.pop('_books',None)
    signal_enabled=[True]
    monkeypatch.setattr('bcc.strategies.signal',lambda state,key,spec,bars,now:SimpleNamespace(
        matched=signal_enabled[0] and key==candidate+':'+section+':entry',available=True,observations=[]))
    class Source:
        def __init__(self):self.attempts={}
        def candles(self,symbol,required):
            self.attempts[symbol]=self.attempts.get(symbol,0)+1;time.sleep(.004)
            return [{'t':(int(time.time())//60-1)*60000,'open':100,'high':101,
                     'low':99,'close':100,'volume':1}]
        def orderbook(self,symbol):
            data=deepcopy(candidate_margin)
            data['quote']=observed_quote({'bid':100,'ask':100,'at':time.time()})
            return dict(book(),margin_required=True,margin_data=data)
        def position_orderbook(self,symbol):return book()
    source=Source();scheduler=MarketScheduler(source,cfg,lambda *_:100,workers=8,period=.01)
    blocked=False;rechecked=False;first_at=None
    try:
        deadline=time.monotonic()+12
        while time.monotonic()<deadline and not rechecked:
            for symbol,tick,error in scheduler.collect(store.read(False),timeout=.02):
                if error or symbol!=candidate or not tick or not tick.book or not tick.bars:continue
                fresh=observed_quote({'bid':100,'ask':100,'at':time.time()})
                if not blocked:
                    first_at=tick.book['at']
                    assert process_tick(cfg,store,Veles(),tick,observations=({held:fresh},{held:fresh})) is True
                    assert len(store.history())==1
                    scheduler.retry_data_blocked(symbol)
                    with store.transaction(False) as (state,_):
                        renewed=deepcopy(state['margin_data'][held]);renewed['risk_at']=time.time()
                    process_tick(cfg,store,Veles(),MarketTick(held,[],None,[],{},margin_data=renewed,risk_only=True))
                    signal_enabled[0]=signal_still_valid
                    blocked=True
                else:
                    assert tick.book['at']>first_at
                    assert process_tick(cfg,store,Veles(),tick,observations=({held:fresh},{held:fresh})) is False
                    rechecked=True
                    break
        assert blocked and rechecked
        assert len(source.attempts)<200, 'retry waited for the full universe'
        assert len(store.read(False)['positions'])==(2 if signal_still_valid else 1)
        assert len(store.history())==(2 if signal_still_valid else 1)
        store.audit()
    finally:scheduler.close()
