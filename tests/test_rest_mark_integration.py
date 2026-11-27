"""Actual cache and client transport: budget, leader, denial, age, WS preference."""
import time
import pytest

from bcc import market_clock, market, ws_split_ticker
from bcc.data_cache import PublicCache, CacheBusy, CacheContention, ProviderDenied
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
    # Dedicated collateral publications never rewrite the raw bulk row.
    assert cache.read_fresh(URL,PARAMS,HOST) is None
    before=cache.read_fresh_many([],HOST,include_rest_marks=True)[0]
    key,owner,_=cache.begin(URL,PARAMS,HOST,priority=True,mark_leader=fence)
    assert cache.read_fresh_many([],HOST,include_rest_marks=True)[0]==before
    with pytest.raises(CacheBusy):
        cache.begin(URL,PARAMS,HOST,priority=True,mark_leader=fence)
    now[0]+=.1
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


def test_rest_refresh_path_uses_public_http_and_original_timestamp(setup,monkeypatch):
    now,cache,registry,fence,snapshot=setup
    monkeypatch.delenv("BCC_MARKET_HUB_MODE",raising=False)
    calls=[]
    class Response:
        status_code=200
        headers={}
        def raise_for_status(self): pass
        def json(self): return snapshot()
    class Session:
        def get(self,url,**kw):
            calls.append((url,kw))
            return Response()
    class Source(market.PublicData):
        @property
        def session(self):return Session()
    source=Source(quote_stream=object())
    source.shared=cache
    original_begin=cache.begin
    reservations=[]
    def begin(*args, **kwargs):
        reservations.append(1)
        if len(reservations)==1:
            raise CacheContention('transient SQLite writer contention')
        return original_begin(*args, **kwargs)
    monkeypatch.setattr(cache,'begin',begin)
    try:
        source.refresh_rest_marks(*fence)
        assert len(reservations)==2
        assert len(calls)==1 and calls[0][0]==URL
        assert calls[0][1]==dict(params=PARAMS,timeout=(1,2),allow_redirects=False)
        first=cache.read_fresh_many([],HOST,include_rest_marks=True)[0]
        assert first["time"]==round(now[0]*1000)
        # Identical/regressive exchange timestamps never renew the compact row.
        with pytest.raises(market.DataUnavailable):
            source.refresh_rest_marks(*fence)
        now[0]-=.1
        with pytest.raises(market.DataUnavailable):
            source.refresh_rest_marks(*fence)
        assert cache.read_fresh_many([],HOST,include_rest_marks=True)[0]==first
        now[0]+=.2
        source.refresh_rest_marks(*fence)
        assert cache.read_fresh_many([],HOST,include_rest_marks=True)[0]["time"]>first["time"]
        source.cooldowns[HOST]=time.time()+30
        before=len(calls)
        with pytest.raises(market.DataUnavailable):
            source.refresh_rest_marks(*fence)
        assert len(calls)==before
    finally:
        source.shared=None
        source.close()


def test_dedicated_mark_writer_preserves_existing_ordinary_bulk_row(setup):
    now,cache,registry,fence,snapshot=setup
    key,owner,_=cache.begin(URL,PARAMS,HOST)
    raw=snapshot()
    assert cache.finish(key,owner,HOST,raw,ttl=60.)
    with cache.read_db() as db:
        before=db.execute("SELECT value,until FROM cache WHERE k=?", (key,)).fetchone()
    now[0]+=.2
    assert publish(cache,fence,snapshot)
    with cache.read_db() as db:
        after=db.execute("SELECT value,until FROM cache WHERE k=?", (key,)).fetchone()
    assert before==after
    assert cache.read_fresh_many([],HOST,include_rest_marks=True)[0]['time']==round(now[0]*1000)


def test_mark_lease_is_independent_of_regular_bulk_lease(setup):
    from bcc.data_cache import RestMarkLeaseBusy
    from bcc.rest_mark_fallback import LEASE_KEY
    now,cache,registry,fence,snapshot=setup
    ordinary,ordinary_owner,_=cache.begin(URL,PARAMS,HOST,priority=True)
    mark,mark_owner,_=cache.begin(URL,PARAMS,HOST,priority=True,mark_leader=fence)
    assert mark==LEASE_KEY and ordinary!=mark
    with pytest.raises(RestMarkLeaseBusy):
        cache.begin(URL,PARAMS,HOST,priority=True,mark_leader=fence)
    assert cache.finish(mark,mark_owner,HOST,snapshot(),ttl=1.,mark_leader=fence)
    assert cache.finish(ordinary,ordinary_owner,HOST,snapshot(),ttl=1.)
    assert cache.read_fresh(URL,PARAMS,HOST) is not None
    assert cache.read_fresh_many([],HOST,include_rest_marks=True)[0] is not None


def test_mark_reservation_never_increases_total_http_budget(setup,monkeypatch):
    from bcc.data_cache import RestMarkBudgetBusy
    now,cache,registry,fence,snapshot=setup
    wall=time.time()
    monkeypatch.setattr(time,'time',lambda:wall)
    cache._ensure_schema()
    with cache.db() as db:
        db.execute('INSERT OR REPLACE INTO request_budget VALUES(?,?,?)',(HOST,int(wall),58))
    with pytest.raises(CacheBusy,match='бюджет'):
        cache.begin(URL,PARAMS,HOST,priority=True)
    for _ in range(2):
        key,owner,_=cache.begin(URL,PARAMS,HOST,priority=True,mark_leader=fence)
        cache.finish(key,owner,HOST)
    with pytest.raises(RestMarkBudgetBusy):
        cache.begin(URL,PARAMS,HOST,priority=True,mark_leader=fence)
    with cache.read_db() as db:
        assert db.execute('SELECT count FROM request_budget WHERE host=?',(HOST,)).fetchone()[0]==60


def test_mark_lease_cannot_be_used_as_ordinary_publication(setup):
    now,cache,registry,fence,snapshot=setup
    key,owner,_=cache.begin(URL,PARAMS,HOST,priority=True,mark_leader=fence)
    with pytest.raises(ValueError,match='leader-fenced'):
        cache.finish(key,owner,HOST,snapshot(),ttl=1.)
    cache.finish(key,owner,HOST)
    assert cache.read_fresh_many([],HOST,include_rest_marks=True)==[None]
