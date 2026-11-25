"""Portfolio cache handoff cannot wait behind a blocked history/funding GET."""
import json
import threading
import time

import pytest

from bcc import market, market_clock, scheduler
from bcc.data_cache import PublicCache, ProviderDenied
from bcc.hub_minute_repair import URL,HOST,params_for,identity
from bcc.closed_cohort import ClosedCohort,plan,extend,read_tails

B=60060000
S=B-60000
NOW=B+2000


def prev(last=S-60000,n=100):
    return [dict(t=last-(n-1-i)*60000,open=100.,high=103.,low=99.,
                 close=100.,volume=1.) for i in range(n)]


def response(symbol,t=S):
    return dict(retCode=0,time=t+60001,result=dict(symbol=symbol,category="linear",
                list=[[str(t),"100","103","99","101","2","200"]]))


def test_shared_portfolio_cache_advances_all_warm_windows_without_http(tmp_path,monkeypatch):
    monkeypatch.setenv("BCC_MARKET_HUB_MODE","client")
    for name in ("PORTFOLIO_HISTORY_CANDIDATE","CLOSED_HISTORY_CANDIDATE","CENTRAL_REPAIR_CANDIDATE","WS_KLINES"):
        monkeypatch.setenv("BCC_BYBIT_"+name,"1")
    monkeypatch.setattr(market.clock,"bounds",lambda:(NOW/1000,NOW/1000))
    monkeypatch.setattr(market,"market_now",lambda:NOW/1000)
    monkeypatch.setattr(market_clock,"market_now",lambda:NOW/1000)
    source=market.PublicData()
    source.shared=PublicCache(tmp_path/"cache.sqlite")
    symbols=[f"COIN{i}USDT" for i in range(50)]
    try:
        # Seed synthetic fixture rows without exhausting the real 48/s
        # ordinary HTTP budget. This test exercises only the reader.
        source.shared._ensure_schema()
        with source.shared.db() as db:
            db.executemany("INSERT INTO cache(k,value,until,host) VALUES(?,?,?,?)",
                [(identity(params_for(s,S)),json.dumps(response(s)),time.time()+180,HOST)
                 for s in symbols])
            db.commit()
        for symbol in symbols:
            source.cache[("closed_bars",symbol)]=(time.time(),prev())
        monkeypatch.setattr(source,"bybit",lambda *a,**kw:pytest.fail("cohort initiated HTTP"))
        out=source.cached_closed_cohort({s:100 for s in symbols})
        assert set(out)==set(symbols)
        assert all(len(b)==100 and b[-1]["t"]==S and b[-1]["close"]==101 for b in out.values())
        assert all(source.cache[("closed_bars",s)][1]==out[s] for s in symbols)
        source.shared.record_http_denial("a"*64,"fixture",HOST,cooldown=90,denial_status=403)
        with pytest.raises(market.SourceAccessDenied):
            source.cached_closed_cohort({s:100 for s in symbols})
    finally:
        source.close()


def test_quote_handoff_uses_new_history_before_original_job_finishes(monkeypatch):
    monkeypatch.setenv("BCC_BYBIT_MAINTAINED_MARK_CANDIDATE","1")
    monkeypatch.setenv("BCC_BYBIT_PORTFOLIO_HISTORY_CANDIDATE","1")
    monkeypatch.setattr(scheduler,"market_now",lambda:NOW/1000)
    monkeypatch.setattr(scheduler,"book_is_fresh",lambda stamp:True)
    monkeypatch.setattr(scheduler,"quote_is_recent",lambda *a,**kw:True)
    release=threading.Event()
    entered=threading.Event()
    class Source:
        def position_orderbook(self,symbol):
            return {"at":NOW/1000}
        def closed_candles(self,*a,**kw):
            raise AssertionError("unexpected unpatched fetch")
        def cached_closed_cohort(self,req):
            return {s:prev(S) for s in req}
    cfg={"strategy":"VELES","symbols":["BTCUSDT"]}
    state={"positions":{"p":{"symbol":"BTCUSDT","opened":0}},
           "pending":{},"marks":{}}
    sched=scheduler.MarketScheduler(Source(),cfg,lambda *a:100,workers=2)
    def blocked(*a,**kw):
        entered.set()
        assert release.wait(8)
        return scheduler.MarketTick("BTCUSDT",prev(),None,[],{},live_history=True)
    monkeypatch.setattr(sched,"_fetch",blocked)
    try:
        deadline=time.monotonic()+4
        got=False
        while time.monotonic()<deadline:
            for symbol,tick,error in sched.collect(state,timeout=.02):
                if (error is None and tick.book is not None and tick.bars
                        and tick.bars[-1]["t"]==S):
                    got=True
            if got and entered.is_set():
                break
        assert entered.is_set() and got
        assert "BTCUSDT" in sched.jobs and not sched.jobs["BTCUSDT"].done()
        assert sched._closed_cohort.delivered==1
        # Changing to an intrabar strategy must disable the closed-only path.
        cfg["settings"]={"trigger":"intrabar"}
        assert not sched._closed_symbols(state,{"BTCUSDT"})
    finally:
        release.set()
        sched.close()


def test_cohort_late_future_does_not_authorize_old_boundary():
    ready=threading.Event()
    def reader(req):
        assert ready.wait(3)
        return {"BTCUSDT":prev(S)}
    c=ClosedCohort(reader)
    try:
        c.poll({"BTCUSDT":100},B,0.)
        old=c.job
        for _ in range(100):
            c.poll({"BTCUSDT":100},B,0.)
            assert c.job is old
        ready.set()
        old.result(timeout=2)
        assert c.poll({"BTCUSDT":100},B+60000,1.)==set()
        assert not c.ready
    finally:
        ready.set()
        c.close()


def test_missing_history_never_fabricates_gap():
    previous=prev(S-120000)
    assert extend(previous,100,B,{S:dict(t=S,open=100.,high=103.,low=99.,close=101.,volume=1.)}) is None



def test_cohort_does_not_override_explicit_provider_denial(monkeypatch):
    monkeypatch.setenv("BCC_BYBIT_PORTFOLIO_HISTORY_CANDIDATE","1")
    monkeypatch.setattr(scheduler,"market_now",lambda:NOW/1000)
    class Source:
        pass
    sched=scheduler.MarketScheduler(Source(),{"strategy":"VELES","symbols":["BTCUSDT"]},lambda *a:100)
    sched._closed_cohort=ClosedCohort(lambda req:{})
    sched._closed_cohort.boundary=B
    sched._closed_cohort.ready={"BTCUSDT":prev(S)}
    tick=scheduler.MarketTick("BTCUSDT",[],None,[],{},
                            history_error="provider denied",history_denied=True)
    try:
        assert sched._apply_closed_cohort([("BTCUSDT",tick,None)],{"BTCUSDT"},{})==0
        assert tick.history_denied and tick.history_error=="provider denied" and not tick.bars
    finally:
        sched.close()
