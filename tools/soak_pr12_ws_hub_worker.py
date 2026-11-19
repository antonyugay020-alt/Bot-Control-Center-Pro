"""Offline synthetic WS frames -> real Hub cache -> PublicData -> VELES -> SQLite."""
import os,time,tempfile
from types import SimpleNamespace
import bcc.market as market
import bcc.market_clock as mc
import bcc.strategies as strategies
from bcc.ws_quotes import BybitQuoteStream
from bcc.ws_orderbooks import BybitOrderbookWS
from bcc.market_hub import MarketDataHub
from bcc.market import PublicData,DataUnavailable
from bcc.market_clock import observed_quote
from bcc.config import defaults,range_section
from bcc.storage import Repository,Store
from bcc.runtime import process_tick
from bcc.scheduler import MarketTick
from bcc.strategies import Veles
from bcc.trading import enter,plan
from test_core import attach
from test_margin_finance import use_mark,raw_tiers
from test_bybit_ws_position_fast_lane import _instrument

def main():
    # Ignore inherited roots and modes: this experiment exclusively owns the
    # temporary cache below and must never attach to a running account's Hub.
    for key in ('BCC_PUBLIC_CACHE', 'BCC_MARKET_HUB_MODE', 'BCC_BYBIT_WS_QUOTES',
                'BCC_BYBIT_WS_BOOKS', 'BCC_BYBIT_WS_KLINES'):
        os.environ.pop(key, None)
    symbols=[f'COIN{i}USDT' for i in range(824)];held=symbols[:6];target=symbols[10];trigger=[False]
    cfg=defaults('VELES');cfg['name']='Synthetic WS-to-ledger 824';cfg['symbols']=symbols;cfg['sections']=[range_section(30,40)];cfg['settings']['orders']['count']=1
    strategies.signal=lambda state,key,spec,bars,now: SimpleNamespace(matched=trigger[0] and key.startswith(target+':') and key.endswith(':entry'),available=True,observations=[])
    clock=mc.MarketClock();clock.ensure=lambda:None;mc.clock=clock;market.clock=clock
    with tempfile.TemporaryDirectory(prefix='bcc-pr12-vertical-') as tmp:
        path=os.path.join(tmp,'market.sqlite');q=BybitQuoteStream();b=BybitOrderbookWS();q.subscribe_many(symbols);b.subscribe(symbols);qgen=q.connect();bgen=b.connect()
        producer=PublicData(quote_stream=q,orderbook_stream=b);hub=MarketDataHub(path,source=producer,max_workers=6)
        reader=None;start=time.monotonic();seq=[0]
        try:
            assert hub.acquire_leadership()
            for sym in symbols:
                hub.registry.subscribe('https://api.bybit.com/v5/market/tickers',{'category':'linear','symbol':sym},lifetime=120)
                hub.registry.subscribe('https://api.bybit.com/v5/market/orderbook',{'category':'linear','symbol':sym,'limit':50},lifetime=120)
            os.environ['BCC_PUBLIC_CACHE']=path;os.environ['BCC_MARKET_HUB_MODE']='client';os.environ['BCC_BYBIT_WS_QUOTES']='1';os.environ['BCC_BYBIT_WS_BOOKS']='1'
            reader=PublicData();reader.set_position_symbols(held);get=reader.get
            def forbidden_network(*args, **kwargs):
                raise AssertionError('Synthetic soak must never use network HTTP')
            reader.session.get = forbidden_network
            def patched(url,params=None,**kwargs):
                if url.endswith('/instruments-info'):return _instrument(params['symbol'])
                if url.endswith('/risk-limit'):return {'retCode':0,'time':int(time.time()*1000),'result':{'list':raw_tiers(params['symbol'])}}
                return get(url,params,**kwargs)
            reader.get=patched
            def publish(items):
                nonlocal qgen,bgen
                now=int(time.time()*1000);events=[];seq[0]+=1
                for sym in dict.fromkeys(items):
                    hub.registry.subscribe('https://api.bybit.com/v5/market/tickers',{'category':'linear','symbol':sym},lifetime=60 if sym in held else 12,priority=sym in held)
                    hub.registry.subscribe('https://api.bybit.com/v5/market/orderbook',{'category':'linear','symbol':sym,'limit':50},lifetime=60 if sym in held else 12,priority=sym in held)
                    assert q.ingest({'topic':'tickers.'+sym,'type':'snapshot','ts':now,'data':{'symbol':sym,'bid1Price':'99.99','ask1Price':'100.01','markPrice':'100'}},generation=qgen)
                    assert b.ingest({'topic':'orderbook.50.'+sym,'type':'snapshot','ts':now,'data':{'s':sym,'u':seq[0],'seq':seq[0],'b':[['99.99','1000']],'a':[['100.01','1000']]}},generation=bgen)
                    events.extend([('tickers',sym),('depth50',sym)])
                assert all(hub.publish_ws_events(events))
            folder=Repository(os.path.join(tmp,'accounts')).create(cfg);store=Store(folder,10000);section=cfg['sections'][0]['id']
            with store.transaction() as (state,events):
                for sym in held:
                    attach(state,sym);use_mark(state,symbol=sym)
                    ok,why=enter(state,events,sym+':'+section,sym,'LONG',cfg['settings'],100,25,plan(cfg['settings']['orders'],'LONG',100),section);assert ok,why
                    state['positions'][sym+':'+section]['exit_signal']=cfg['sections'][0]['exit']
                    state['marks'][sym]=observed_quote({'bid':99.99,'ask':100.01,'at':time.time()})
                state.pop('_books',None)
            seen=set()
            for sym in symbols:
                publish(held+[sym]);book=reader.orderbook(sym)
                bar=dict(t=(int(time.time())//60-1)*60000,open=100,high=101,low=99,close=100,volume=1)
                marks,quotes=reader._ws_portfolio_observations();process_tick(cfg,store,Veles(),MarketTick(sym,[bar],book,[],{}),observations=(marks,quotes));seen.add(sym)
            print('scan',len(seen),'positions',len(store.read()['positions']),'elapsed',round(time.monotonic()-start,2),flush=True)
            assert len(seen)==824 and len(store.read()['positions'])==6
            publish(held+[target]);stale_book=reader.orderbook(target);old_qgen=qgen;old_bgen=bgen
            q.disconnect();b.disconnect()
            assert not q.ingest({'topic':'tickers.'+target,'type':'snapshot','ts':int(time.time()*1000),'data':{'symbol':target,'bid1Price':'99.99','ask1Price':'100.01','markPrice':'100'}},generation=old_qgen)
            time.sleep(3.1)
            try: process_tick(cfg,store,Veles(),MarketTick(target,[bar],stale_book,[],{}))
            except DataUnavailable: pass
            else: raise AssertionError('Stale disconnected book was admitted')
            assert len(store.history())==6
            qgen=q.connect();bgen=b.connect()
            assert qgen!=old_qgen and bgen!=old_bgen
            print('disconnect, old-generation fence and stale-book denial PASS',flush=True)
            trigger[0]=True
            with store.transaction() as (state,events):state['margin_data'][held[0]]['risk_at']=time.time()-301
            publish(held+[target]);book=reader.orderbook(target);marks,quotes=reader._ws_portfolio_observations()
            process_tick(cfg,store,Veles(),MarketTick(target,[bar],book,[],{}),observations=(marks,quotes))
            blocked=store.read();assert len(blocked['positions'])==6
            assert any('Устарели публичные ступени риска' in row.get('reason','') for row in blocked['diagnostics'][target])
            risk=reader.refresh_position_risk(held[0]);assert risk
            process_tick(cfg,store,Veles(),MarketTick(held[0],[],None,[],{},margin_data=risk,risk_only=True))
            publish(held+[target]);book=reader.orderbook(target);marks,quotes=reader._ws_portfolio_observations()
            process_tick(cfg,store,Veles(),MarketTick(target,[bar],book,[],{}),observations=(marks,quotes));assert len(store.read()['positions'])==7
            store.audit();print('entry_after_fresh_tier PASS ledger PASS',flush=True)
            reader.set_position_symbols(held+[target]);deadline=time.monotonic()+120;renew=time.monotonic()+30;count=0;seen_soak=set()
            while time.monotonic()<deadline:
                if time.monotonic()>=renew:
                    assert hub.registry.renew_leader(hub.owner,token=hub._leader_token,lifetime=120)
                    renew=time.monotonic()+30
                sym=symbols[count%len(symbols)];publish(held+[target,sym]);book=reader.orderbook(sym)
                bar=dict(t=(int(time.time())//60-1)*60000,open=100,high=101,low=99,close=100,volume=1)
                marks,quotes=reader._ws_portfolio_observations();process_tick(cfg,store,Veles(),MarketTick(sym,[bar],book,[],{}),observations=(marks,quotes))
                seen_soak.add(sym);count+=1
            store.audit();assert len(store.read()['positions'])==7 and len(store.history())==7 and len(seen_soak)==824
            print('sustained_seconds',120,'ticks',count,'symbols',len(seen_soak),'positions',7,'ledger PASS',flush=True)
            # Exercise actual shared HTTP-policy fences against simulated provider responses.
            import requests
            from bcc.data_cache import CacheBusy
            from bcc.market import SourceAccessDenied
            class Refusal:
                def __init__(self,status): self.status_code=status;self.headers={}
                def raise_for_status(self): raise requests.HTTPError('synthetic HTTP '+str(self.status_code))
            host='api.bybit.com';fault='https://api.bybit.com/v5/market/risk-limit'
            def refuse(status,symbol):
                producer.session.get=lambda *args,**kwargs:Refusal(status)
                previous=os.environ.pop('BCC_MARKET_HUB_MODE',None)
                try:
                    try:producer.get(fault,{'category':'linear','symbol':symbol})
                    except DataUnavailable as exc:
                        if status==403: assert isinstance(exc,SourceAccessDenied)
                    else:raise AssertionError('HTTP policy refusal was ignored')
                finally:
                    if previous is not None:os.environ['BCC_MARKET_HUB_MODE']=previous
            old_target=target;target=symbols[11];reader.set_position_symbols(held+[old_target])
            publish(held+[old_target,target])
            with store.transaction() as (state,events):state['margin_data'][held[0]]['risk_at']=time.time()-301
            refuse(429,'FAULT429USDT')
            try:producer.shared.begin(fault,{'category':'linear','symbol':'AFTER429USDT'},host)
            except CacheBusy:pass
            else:raise AssertionError('429 did not fence HTTP admission')
            publish(held+[old_target,target]);book429=reader.orderbook(target);obs429=reader._ws_portfolio_observations()
            process_tick(cfg,store,Veles(),MarketTick(target,[bar],book429,[],{}),observations=obs429)
            blocked=store.read();assert len(blocked['positions'])==7 and len(store.history())==7
            assert any('Устарели публичные ступени риска' in row.get('reason','') for row in blocked['diagnostics'][target])
            print('429 HTTP fence, independent WS and stale-tier entry denial PASS',flush=True)
            # Simulate expiry inside the throwaway test database only.
            with producer.shared.db() as db:db.execute('UPDATE cooldown SET until=? WHERE host=?',(time.time()-1,host))
            producer.cooldowns[host]=time.time()-1
            refuse(403,'FAULT403USDT')
            before=len(store.history())
            try:reader.orderbook(target)
            except SourceAccessDenied:pass
            else:raise AssertionError('403 did not fence the reader')
            assert not hub.publish_ws_event('tickers',target)
            assert len(store.history())==before
            with producer.shared.db() as db:
                db.execute('UPDATE cooldown SET until=? WHERE host=?',(time.time()-1,host))
                db.execute('UPDATE provider_denial SET until_mono=?,until_utc=? WHERE host=?',(time.monotonic()-1,time.time()-1,host))
            producer.cooldowns[host]=time.time()-1
            producer._access_denials[host]=(time.monotonic()-1,403)
            publish(held+[old_target,target]);renewed=reader.margin_snapshot(held[0])
            process_tick(cfg,store,Veles(),MarketTick(held[0],[],None,[],{},margin_data=renewed,risk_only=True))
            publish(held+[old_target,target]);book=reader.orderbook(target);marks,quotes=reader._ws_portfolio_observations()
            process_tick(cfg,store,Veles(),MarketTick(target,[bar],book,[],{}),observations=(marks,quotes))
            assert len(store.read()['positions'])==8 and len(store.history())==8
            store.audit();print('403 fence, synthetic expiry and new entry PASS',flush=True)
        finally:
            if reader:reader.close()
            hub.close();producer.close()


if __name__ == "__main__":
    main()
