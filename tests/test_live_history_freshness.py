"""A cached history handoff must not outlive its source candle timestamps."""
import time
import threading

import pytest

from bcc.config import defaults, range_section
from bcc.market_clock import observed_quote
from bcc.runtime import process_tick
from bcc.scheduler import MarketScheduler, MarketTick
from bcc.storage import Store, initial_state
from bcc.strategies import Veles
from bcc.trading import enter, plan
from test_core import attach, book


@pytest.mark.parametrize('age_seconds',[121,240])
def test_expired_live_history_never_reaches_strategy(tmp_path,age_seconds):
    cfg=defaults('VELES');cfg['symbols']=['BTCUSDT'];cfg['sections']=[range_section(30,40)]
    store=Store(tmp_path,cfg['capital'])
    now=time.time();stamp=int((now-age_seconds)//60)*60000
    bars=[dict(t=stamp,open=100,high=101,low=99,close=100,volume=1)]
    tick=MarketTick('BTCUSDT',bars,book(),[],{});tick.live_history=True
    class ForbiddenEngine:
        def process(self,*args):pytest.fail('expired candles reached a strategy')
    assert process_tick(cfg,store,ForbiddenEngine(),tick) is True
    assert store.read(False)['diagnostics']['BTCUSDT'][0]['reason']=='Устаревшие свечи'
    assert store.history()==[]
    store.audit()


@pytest.mark.parametrize('stamp',[None,'invalid',True,10**1000])
def test_invalid_live_candle_timestamp_fails_closed(tmp_path,stamp):
    cfg=defaults('VELES');cfg['symbols']=['BTCUSDT'];cfg['sections']=[range_section(30,40)]
    store=Store(tmp_path,cfg['capital'])
    tick=MarketTick('BTCUSDT',[{'t':stamp}],book(),[],{});tick.live_history=True
    class ForbiddenEngine:
        def process(self,*args):pytest.fail('invalid timestamp reached a strategy')
    process_tick(cfg,store,ForbiddenEngine(),tick)
    assert tick.history_error=='Устаревшие свечи' and store.history()==[]


def test_future_live_candle_is_not_accepted(tmp_path):
    cfg=defaults('VELES');cfg['symbols']=['BTCUSDT'];cfg['sections']=[range_section(30,40)]
    store=Store(tmp_path,cfg['capital'])
    tick=MarketTick('BTCUSDT',[{'t':int((time.time()+180)*1000)}],book(),[],{});tick.live_history=True
    class ForbiddenEngine:
        def process(self,*args):pytest.fail('future candle reached a strategy')
    process_tick(cfg,store,ForbiddenEngine(),tick)
    assert tick.history_error=='Устаревшие свечи' and store.history()==[]


def test_history_is_rechecked_after_a_slow_account_transaction(tmp_path,monkeypatch):
    cfg=defaults('VELES');cfg['symbols']=['BTCUSDT'];cfg['sections']=[range_section(30,40)]
    store=Store(tmp_path,cfg['capital']);now=time.time()
    tick=MarketTick('BTCUSDT',[{'t':int(now//60)*60000}],book(),[],{});tick.live_history=True
    calls=[now,now+181]
    monkeypatch.setattr('bcc.runtime.market_now',lambda: calls.pop(0) if calls else now+181)
    class ForbiddenEngine:
        def process(self,*args):pytest.fail('history expired while the owner was busy')
    assert process_tick(cfg,store,ForbiddenEngine(),tick) is True
    assert tick.history_error=='Устаревшие свечи' and not store.history()


def test_expired_history_does_not_block_fresh_book_protective_exit(tmp_path):
    cfg=defaults('VELES');cfg['symbols']=['BTCUSDT'];cfg['sections']=[range_section(30,40)]
    cfg['settings']['orders']['count']=1;cfg['settings']['profit'].update(mode='simple',pct=.5)
    store=Store(tmp_path,cfg['capital']);section=cfg['sections'][0]['id']
    with store.transaction() as (state,events):
        attach(state)
        ok,why=enter(state,events,'BTCUSDT:'+section,'BTCUSDT','LONG',cfg['settings'],100,25,
                     plan(cfg['settings']['orders'],'LONG',100),section)
        assert ok,why
        state['positions']['BTCUSDT:'+section]['exit_signal']=cfg['sections'][0]['exit']
        state['marks']['BTCUSDT']=observed_quote({'bid':99.99,'ask':100.01,'at':time.time()})
        state.pop('_books',None)
    stamp=int((time.time()-180)//60)*60000
    bars=[dict(t=stamp,open=100,high=101,low=99,close=100,volume=1)]
    tick=MarketTick('BTCUSDT',bars,book(110),[],{});tick.live_history=True
    process_tick(cfg,store,Veles(),tick)
    assert not store.read(False)['positions']
    assert [row['action'] for row in reversed(store.history())]==['OPEN','CLOSE']
    store.audit()


def test_reused_position_history_is_revalidated_on_each_quote(tmp_path,monkeypatch):
    cfg=defaults('VELES');cfg['symbols']=['BTCUSDT'];cfg['sections']=[range_section(30,40)]
    store=Store(tmp_path,cfg['capital']);now=time.time();release=threading.Event();entered=threading.Event()
    class Source:
        calls=0
        def candles(self,symbol,required):
            self.calls+=1
            if self.calls>1:
                entered.set();assert release.wait(6)
            return [dict(t=int(now//60)*60000,open=100,high=101,low=99,close=100,volume=1)]
        def position_orderbook(self,symbol):return book()
        def orderbook(self,symbol):return book()
        def funding(self,symbol):return []
    state=initial_state(1000);state['positions']={'p':{'symbol':'BTCUSDT','opened':now}}
    state['marks']={'BTCUSDT':observed_quote({'bid':99.99,'ask':100.01,'at':time.time()})}
    source=Source();scheduler=MarketScheduler(source,cfg,lambda *_:100,workers=2,period=.05)
    class ForbiddenEngine:
        def process(self,*args):pytest.fail('cached expired history reached a strategy')
    try:
        deadline=time.monotonic()+3;cached=None
        while time.monotonic()<deadline and cached is None:
            for _,tick,error in scheduler.collect(state,timeout=.02):
                if tick and tick.bars:
                    cached=tick;break
        assert cached and cached.live_history
        monkeypatch.setattr('bcc.runtime.market_now',lambda:now+181)
        process_tick(cfg,store,ForbiddenEngine(),cached)
        assert cached.history_error=='Устаревшие свечи' and cached.bars==[]
        second=None
        while time.monotonic()<deadline and second is None:
            for _,tick,error in scheduler.collect(state,timeout=.02):
                if tick and tick.bars:
                    second=tick;break
        assert second and second.live_history and second.book
        process_tick(cfg,store,ForbiddenEngine(),second)
        assert second.history_error=='Устаревшие свечи' and second.bars==[]
        assert not store.history()
    finally:
        release.set();scheduler.close()


def test_expired_handoff_refetches_fresh_history_before_full_scan(tmp_path):
    cfg=defaults('VELES');cfg['symbols']=[f'C{i}USDT' for i in range(828)]
    cfg['sections']=[range_section(30,40)];target=cfg['symbols'][0]
    store=Store(tmp_path,cfg['capital'])
    class Source:
        def __init__(self):self.attempts={}
        def candles(self,symbol,required):
            self.attempts[symbol]=self.attempts.get(symbol,0)+1
            time.sleep(.004)
            stale=symbol==target and self.attempts[symbol]==1
            stamp=int((time.time()-(180 if stale else 0))//60)*60000
            return [dict(t=stamp,open=100,high=101,low=99,close=100,volume=1)]
        def orderbook(self,symbol):return book()
    class Engine:
        calls=0
        def process(self,*args):self.calls+=1
    source=Source();engine=Engine();scheduler=MarketScheduler(source,cfg,lambda *_:100,workers=8,period=.01)
    first=False;done=False
    try:
        deadline=time.monotonic()+12
        while time.monotonic()<deadline and not done:
            for symbol,tick,error in scheduler.collect(initial_state(1000),timeout=.02):
                if symbol!=target or not tick or not tick.book:continue
                blocked=process_tick(cfg,store,engine,tick)
                if not first:
                    assert blocked is True and tick.history_error=='Устаревшие свечи'
                    assert engine.calls==0
                    scheduler.retry_data_blocked(symbol);first=True
                else:
                    assert blocked is False and engine.calls==1
                    done=True;break
        assert first and done and source.attempts[target]>=2
        assert len(source.attempts)<200
        assert not store.history();store.audit()
    finally:scheduler.close()
