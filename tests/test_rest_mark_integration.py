"""Actual cache and client transport: budget, leader, denial, age, WS preference."""
import time
import pytest

from bcc import market_clock, market, ws_split_ticker
from bcc.data_cache import PublicCache, CacheBusy, ProviderDenied
from bcc.market_hub import HubRegistry
from bcc.rest_mark_fallback import URL, HOST
from bcc.ws_split_ticker import make_split_ticker

PARAMS={"category":"linear"}
S="BTCUSDT"

@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setenv("BCC_BYBIT_REST_MARK_FALLBACK","1")
    now=[round(time.time(),3)]
    monkeypatch.setattr(market_clock,"utc_bounds",lambda:(now[0],now[0]+.01))
    monkeypatch.setattr(ws_split_ticker,"book_age_bounds",lambda t:(now[0]-t,now[0]-t+.01))
    monkeypatch.setattr(market.clock,"bounds",lambda:(now[0],now[0]+.01))
    path=tmp_path/"cache.sqlite"
    cache=PublicCache(path)
    registry=HubRegistry(path)
    assert registry.claim_leader("owner",lifetime=120)
    fence=("owner",registry.leader_token("owner"))
    def snapshot():
        return dict(retCode=0,time=round(now[0]*1000),result=dict(category="linear",list=[
            dict(symbol=S,markPrice="100",bid1Price="98",ask1Price="102"),
            dict(symbol="ETHUSDT",markPrice="200")]))
    try:
        yield now,cache,registry,fence,snapshot
    finally:
        cache.close()

def publish(cache,fence,snapshot):
    key,owner,prior=cache.begin(URL,PARAMS,HOST,priority=True,mark_leader=fence)
    assert owner is not None and prior is None
    return cache.finish(key,owner,HOST,snapshot(),ttl=1.,mark_leader=fence)

def test_refresh_uses_existing_lease_and_budget_and_keeps_old_snapshot(setup):
    now,cache,registry,fence,snapshot=setup
    assert publish(cache,fence,snapshot)
    before=cache.read_fresh_many([],HOST,include_rest_marks=True)[0]
    key,owner,_=cache.begin(URL,PARAMS,HOST,priority=True,mark_leader=fence)
    assert cache.read_fresh_many([],HOST,include_rest_marks=True)[0]==before
    with pytest.raises(CacheBusy):
        cache.begin(URL,PARAMS,HOST,priority=True,mark_leader=fence)
    assert cache.finish(key,owner,HOST,snapshot(),ttl=1.,mark_leader=fence)
    with cache.read_db() as db:
        assert db.execute("SELECT count FROM request_budget WHERE host=?",(HOST,)).fetchone()[0]>=1

def test_replaced_leader_cannot_publish(setup):
    now,cache,registry,fence,snapshot=setup
    key,owner,_=cache.begin(URL,PARAMS,HOST,priority=True,mark_leader=fence)
    registry.release_leader(fence[0],token=fence[1])
    assert registry.claim_leader("replacement",lifetime=120)
    assert not cache.finish(key,owner,HOST,snapshot(),ttl=1.,mark_leader=fence)
    assert cache.read_fresh_many([],HOST,include_rest_marks=True)==[None]
    with pytest.raises(CacheBusy):
        cache.begin(URL,PARAMS,HOST,priority=True,mark_leader=fence)

def test_ordinary_http_response_cannot_publish_fallback(setup):
    now,cache,registry,fence,snapshot=setup
    key,owner,_=cache.begin(URL,PARAMS,HOST)
    assert cache.finish(key,owner,HOST,snapshot(),ttl=1.)
    assert cache.read_fresh_many([],HOST,include_rest_marks=True)==[None]

def test_client_reads_ws_first_then_rest_only_for_missing_mark(setup,monkeypatch):
    now,cache,registry,fence,snapshot=setup
    assert publish(cache,fence,snapshot)
    monkeypatch.setenv("BCC_MARKET_HUB_MODE","client")
    monkeypatch.setenv("BCC_BYBIT_WS_QUOTES","1")
    source=market.PublicData()
    source.shared=cache
    source.set_position_symbols([S,"ETHUSDT"])
    params=dict(category="linear",symbol=S)
    registry.subscribe(URL,params,priority=True)
    cache.bybit_ws_source_deadlines=True
    value=make_split_ticker(S,(101.,now[0],"a"*32))
    assert cache.publish_websocket(URL,params,HOST,value,ttl=1.,
                                  leader_owner=fence[0],leader_token=fence[1])
    try:
        marks,quotes=source.cached_position_observations()
        assert marks[S]["bid"]==101. and marks[S].get("mark_basis")!="rest_snapshot"
        assert marks["ETHUSDT"]["bid"]==200. and marks["ETHUSDT"]["mark_basis"]=="rest_snapshot"
        assert not quotes  # REST never grants executable spread
        assert source._ws_position_mark(S)["bid"]==101.
        assert source._ws_position_mark("ETHUSDT")["mark_basis"]=="rest_snapshot"
        now[0]+=3.1
        source._portfolio_ws_cache=None
        assert source.cached_position_observations()==({},{})
        # Even a fresh REST snapshot cannot bypass a venue-wide denial.
        assert publish(cache,fence,snapshot)
        cache.record_http_denial("a"*64,"fixture",HOST,cooldown=90,denial_status=403)
        with pytest.raises(market.SourceAccessDenied):
            source.cached_position_observations()
    finally:
        source.shared=None
        source.close()

def test_disabled_fallback_never_admits_snapshot(setup,monkeypatch):
    now,cache,registry,fence,snapshot=setup
    assert publish(cache,fence,snapshot)
    monkeypatch.delenv("BCC_BYBIT_REST_MARK_FALLBACK")
    with pytest.raises(ValueError):
        cache.read_fresh_many([],HOST,include_rest_marks=True)
    with pytest.raises(ValueError):
        cache.begin(URL,PARAMS,HOST,priority=True,mark_leader=fence)
