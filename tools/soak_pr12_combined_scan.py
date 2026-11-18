"""Synthetic scheduler+worker+SQLite with 824 symbols, exposure, fault and recovery.
No Hub/WS/network: independent synthetic observations are deliberately injected.
"""
import tempfile,time
from types import SimpleNamespace
from bcc.config import defaults,range_section
from bcc.storage import Repository,Store
from bcc.scheduler import MarketScheduler,MarketTick
from bcc.market import DataUnavailable
from bcc.market_clock import observed_quote
from bcc.runtime import process_tick
from bcc.strategies import Veles
from bcc.trading import enter,plan
from test_core import attach
from test_margin_finance import use_mark
import bcc.strategies as strategies

def main():
    symbols=[f'COIN{i}USDT' for i in range(824)];held=symbols[:6];target=symbols[10]; trigger=[False]
    cfg=defaults('VELES');cfg['name']='Synthetic exposure and full scan';cfg['symbols']=symbols;cfg['sections']=[range_section(30,40)];cfg['settings']['orders']['count']=1
    strategies.signal=lambda state,key,spec,bars,now: SimpleNamespace(matched=trigger[0] and key.startswith(target+':') and key.endswith(':entry'),available=True,observations=[])
    class Source:
        def __init__(self): self.fail=set(symbols[20:30]);self.failed=set()
        def candles(self,symbol,required):
            if symbol in self.fail and symbol not in self.failed:
                self.failed.add(symbol);raise DataUnavailable('synthetic history timeout')
            return [dict(t=(int(time.time())//60-1)*60000,open=100,high=101,low=99,close=100,volume=1)]
        def orderbook(self,symbol):
            m={};data=use_mark(m,symbol=symbol)
            return dict(at=time.time(),bids=[[99.99,100]],asks=[[100.01,100]],qty_step=.001,min_qty=.001,min_notional=.01,tick_size=.01,margin_required=True,margin_data=data)
        position_orderbook=orderbook
        def funding(self,symbol):return []
    source=Source()
    def observations():
        q=observed_quote(dict(bid=100,ask=100,at=time.time()))
        return ({s:q for s in held},{s:q for s in held})
    with tempfile.TemporaryDirectory(prefix='bcc-pr12-combined-') as root:
        folder=Repository(root).create(cfg);store=Store(folder,10000);section=cfg['sections'][0]['id']
        with store.transaction() as (state,events):
            for symbol in held:
                attach(state,symbol);use_mark(state,symbol=symbol)
                ok,reason=enter(state,events,symbol+':'+section,symbol,'LONG',cfg['settings'],100,25,plan(cfg['settings']['orders'],'LONG',100),section)
                assert ok,reason
                state['positions'][symbol+':'+section]['exit_signal']=cfg['sections'][0]['exit']
                state['marks'][symbol]=observed_quote(dict(bid=99.99,ask=100.01,at=time.time()))
            state.pop('_books',None)
        scheduler=MarketScheduler(source,cfg,lambda *_:100,workers=8,period=1)
        seen=set();failure_ticks=0;start=time.monotonic()
        try:
            while time.monotonic()-start<120 and len(seen)<824:
                for symbol,tick,error in scheduler.collect(store.read(),timeout=.01):
                    if error:raise RuntimeError((symbol,error))
                    process_tick(cfg,store,Veles(),tick,observations=observations())
                    if tick.history_error: failure_ticks+=1
                    elif tick.bars: seen.add(symbol)
            print('scan',len(seen),'failed_once',len(source.failed),'failure_ticks',failure_ticks,'elapsed',round(time.monotonic()-start,2),flush=True)
            assert len(seen)==824 and len(source.failed)==10 and failure_ticks>=10
            with store.transaction() as (state,events):state['margin_data'][held[0]]['risk_at']=time.time()-301
            trigger[0]=True
            def target_tick():return MarketTick(target,source.candles(target,100),source.orderbook(target),[],{})
            process_tick(cfg,store,Veles(),target_tick(),observations=observations())
            assert len(store.read()['positions'])==6
            with store.transaction() as (state,events):renewed=dict(state['margin_data'][held[0]]);renewed['risk_at']=time.time()
            process_tick(cfg,store,Veles(),MarketTick(held[0],[],None,[],{},margin_data=renewed,risk_only=True))
            process_tick(cfg,store,Veles(),target_tick(),observations=observations())
            assert len(store.read()['positions'])==7
            store.audit();print('entry_after_recovery PASS, ledger PASS',flush=True)
            deadline=time.monotonic()+120;processed=0;seen_soak=set()
            while time.monotonic()<deadline:
                for symbol,tick,error in scheduler.collect(store.read(),timeout=.01):
                    if error:raise RuntimeError((symbol,error))
                    try:process_tick(cfg,store,Veles(),tick,observations=observations())
                    except DataUnavailable:
                        scheduler.retry_tick(tick);continue
                    processed+=1
                    if tick.bars and not tick.history_error:seen_soak.add(symbol)
            store.audit();assert len(store.read()['positions'])==7
            print('sustained_seconds',120,'processed',processed,'symbols',len(seen_soak),'positions',7,'ledger PASS',flush=True)
        finally:scheduler.close()


if __name__ == "__main__":
    main()
