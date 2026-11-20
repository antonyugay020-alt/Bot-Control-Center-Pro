"""Live cache observations must outlive blocked transport futures on the owner."""
import threading
import time

import pytest

from bcc.config import defaults, range_section
from bcc.market import SourceAccessDenied
from bcc.market_clock import observed_quote, quote_is_recent
from bcc.margin import margin_status
from bcc.position_report import position_mark_pnl
from bcc.runtime import process_tick, run_loop
from bcc.scheduler import MarketTick
from bcc.storage import Repository, Store
from bcc.trading import enter, plan
from test_core import attach, book
from test_margin_finance import use_mark
from test_ws_portfolio_observations import portfolio


class NeverTrades:
    def process(self,*args):pass


def account(path,symbols):
    cfg=defaults('VELES');cfg['name']='Owner continuity';cfg['symbols']=symbols
    cfg['sections']=[range_section(30,40)];cfg['settings']['orders']['count']=1
    cfg['settings']['profit']['pct']=50
    folder=Repository(path).create(cfg);store=Store(folder,10000)
    with store.transaction() as (state,events):
        for symbol in symbols:
            attach(state,symbol);use_mark(state,symbol=symbol)
            ok,why=enter(state,events,symbol+':'+cfg['sections'][0]['id'],symbol,'LONG',
                         cfg['settings'],100,25,plan(cfg['settings']['orders'],'LONG',100),cfg['sections'][0]['id'])
            assert ok,why
        for symbol in symbols:
            state['marks'][symbol]=observed_quote({'bid':100,'ask':100.1,'at':time.time()-6})
            state['margin_data'][symbol]['quote']=observed_quote({'bid':100,'ask':100,'at':time.time()-6})
        state.pop('_books',None)
    return cfg,store,folder


def test_actual_worker_keeps_twenty_sqlite_and_display_marks_fresh_while_all_transport_lanes_wait(portfolio,tmp_path,monkeypatch):
    from bcc import market_clock
    symbols,reader,hub,stream=portfolio
    cfg,store,folder=account(tmp_path/'accounts',symbols)
    cfg['symbols'] = symbols + [f'SCAN{i}USDT' for i in range(801)]
    # Supply a vetted deterministic UTC reference; all feed frames below are
    # genuinely ingested and committed through the production Hub/SQLite path.
    wall,mono=time.time(),time.monotonic()
    market_clock.clock._reference=(wall,wall+.001,wall,mono)
    original=reader.position_orderbook
    calls={};threads=[];lock=threading.Lock()
    retry_entered=threading.Event();history_entered=threading.Event();release=threading.Event()
    publish_stop=threading.Event();failures=[]
    def quote(symbol):
        with lock:
            calls[symbol]=calls.get(symbol,0)+1;attempt=calls[symbol]
            threads.append(threading.current_thread().name)
        if attempt>1:
            retry_entered.set();assert release.wait(timeout=7)
        result=original(symbol)
        if attempt==1:result['at']-=4
        return result
    def history(*args):
        history_entered.set();assert release.wait(timeout=7)
        return []
    monkeypatch.setattr(reader,'position_orderbook',quote)
    # This scenario deliberately has no executable cache recovery; exercise
    # the bounded transport retry while the independent observer stays live.
    monkeypatch.setattr(reader,'cached_execution_book',lambda *a:None)
    monkeypatch.setattr(reader,'candles',history)
    monkeypatch.setattr(reader,'refresh_position_risk',lambda symbol:None)
    monkeypatch.setattr(reader,'funding',lambda symbol:[])
    def publish():
        try:
            while not publish_stop.is_set():
                stamp=int(time.time()*1000)
                for symbol in symbols:
                    stream.ingest({'type':'snapshot','topic':'tickers.'+symbol,'ts':stamp,
                                   'data':{'symbol':symbol,'bid1Price':'100.4','ask1Price':'100.5','markPrice':'100.45'}},generation=stream._generation)
                    hub.publish_ws_event('tickers',symbol)
                publish_stop.wait(.08)
        except BaseException as exc:failures.append(exc)
    publisher=threading.Thread(target=publish);publisher.start();started=time.monotonic()
    def stop():
        if time.monotonic()-started>=3.3:
            release.set();return True
        return False
    try:
        run_loop(folder,cfg,store,reader,NeverTrades(),{},stop,lambda *a:100,None)
        state=Store(folder).read(False)
        assert not failures and len(state['positions'])==20
        assert margin_status(state)==''
        assert all(quote_is_recent(state['marks'][symbol],maximum=3) for symbol in symbols)
        assert all(state['marks'][symbol]['at']>started-mono+wall+.5 for symbol in symbols)
        assert all(position_mark_pnl(state,p) is not None for p in state['positions'].values())
        assert retry_entered.is_set() and history_entered.is_set()
        assert threading.current_thread().name not in threads
        assert len(store.history())==20;store.audit()
    finally:
        release.set();publish_stop.set();publisher.join(timeout=3)
        assert not publisher.is_alive()


@pytest.mark.parametrize('channel',['mark','quote','expired'])
def test_missing_execution_book_keeps_only_independent_current_cache_authority(tmp_path,channel):
    cfg,store,_=account(tmp_path,['BTCUSDT'])
    stamp=time.time()-(4 if channel=='expired' else 0)
    evidence=observed_quote({'bid':100.4,'ask':100.5,'at':stamp})
    observations=({'BTCUSDT':evidence} if channel!='quote' else {},
                  {'BTCUSDT':evidence} if channel!='mark' else {})
    process_tick(cfg,store,NeverTrades(),MarketTick('BTCUSDT',[],None,[],{},quote_error='depth unavailable'),observations=observations)
    state=store.read(False)
    assert ('BTCUSDT' in state['marks']) == (channel=='quote')
    assert ('quote' in state['margin_data']['BTCUSDT']) == (channel=='mark')
    assert len(state['positions'])==1 and len(store.history())==1;store.audit()


def test_provider_denial_at_owner_blocks_an_already_completed_fresh_tick(tmp_path,monkeypatch):
    from bcc import runtime
    cfg,store,folder=account(tmp_path,['BTCUSDT'])
    class Scheduler:
        def __init__(self,*args):pass
        def collect(self,*args):return [('BTCUSDT',MarketTick('BTCUSDT',[{}],book(),[],{}),None)]
        def close(self):pass
    class Source:
        def cached_position_observations(self):raise SourceAccessDenied('api.bybit.com',451)
    class ForbiddenEngine:
        def process(self,*args):pytest.fail('A provider-denied cached tick reached a strategy')
    monkeypatch.setattr(runtime,'MarketScheduler',Scheduler)
    run_loop(folder,cfg,store,Source(),ForbiddenEngine(),{},
             lambda:bool(store.read(False)['diagnostics'].get('BTCUSDT')),lambda *a:100,None)
    state=store.read(False)
    assert 'BTCUSDT' not in state['marks'] and 'quote' not in state['margin_data']['BTCUSDT']
    assert len(store.history())==1;store.audit()


def test_worker_schedules_reconsideration_when_data_blocks_candidate(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from bcc import runtime
    from bcc.strategies import Veles
    cfg,store,folder=account(tmp_path,['BTCUSDT'])
    candidate='CANDIDATEUSDT';cfg['symbols'].append(candidate)
    with store.transaction(include_charts=False) as (state,_):
        use_mark(state,symbol=candidate)
        state['margin_data']['BTCUSDT']['risk_at']=time.time()-301
        state['margin_data']['BTCUSDT']['quote']=observed_quote({'bid':100,'ask':100,'at':time.time()})
        state['marks']['BTCUSDT']=observed_quote({'bid':99.99,'ask':100.01,'at':time.time()})
    retried=[]
    class Scheduler:
        def __init__(self,*args):pass
        def collect(self,*args):
            bar={'t':(int(time.time())//60-1)*60000,'open':100,'high':101,
                 'low':99,'close':100,'volume':1}
            return [(candidate,MarketTick(candidate,[bar],book(),[],{}),None)]
        def retry_data_blocked(self,symbol):retried.append(symbol)
        def close(self):pass
    monkeypatch.setattr(runtime,'MarketScheduler',Scheduler)
    monkeypatch.setattr('bcc.strategies.signal',lambda *a:SimpleNamespace(
        matched=True,available=True,observations=[]))
    deadline=time.monotonic()+3
    run_loop(folder,cfg,store,object(),Veles(),{},
             lambda:bool(retried) or time.monotonic()>=deadline,lambda *a:100,None)
    assert retried==[candidate]
    state=store.read(False)
    assert len(state['positions'])==1 and len(store.history())==1
    assert any('ступени риска' in item['reason'] for item in state['diagnostics'][candidate])
    store.audit()
