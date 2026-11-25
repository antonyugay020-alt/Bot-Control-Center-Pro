"""Candidate recovery and bounded mark-first publication, no live network."""
import json
import time
import pytest
from bcc import market_clock, ws_split_ticker
from bcc.ws_dual_quotes import DualBybitQuoteStream
from bcc.ws_dispatch import MarketEventQueue

S="BTCUSDT"

class Socket:
    def __init__(self): self.messages=[]; self.closed=False
    def send(self, raw): self.messages.append(json.loads(raw))
    def close(self): self.closed=True

@pytest.fixture
def feed(monkeypatch):
    now=[round(time.time(),3)]
    bounds=lambda t:(now[0]-t,now[0]-t+.05)
    monkeypatch.setattr(market_clock,"book_age_bounds",bounds)
    monkeypatch.setattr(market_clock,"latest_now",lambda:now[0]+.05)
    monkeypatch.setattr(ws_split_ticker,"book_age_bounds",bounds)
    events=[]
    stream=DualBybitQuoteStream(wall=lambda:now[0],monotonic=lambda:now[0],
             maintained=True,maintained_recovery=True,on_update=lambda *a:events.append(a))
    primary,reserve=Socket(),Socket()
    stream.sync_symbols([S]);stream.set_priority_symbols([S])
    stream._primary.connect(primary);stream._recovery.connect(reserve)
    now[0]+=.8
    assert stream.refresh_stale_marks([S])==1
    def frame(cs,kind="delta",stamp=None,**data):
        return stream._recovery.ingest(dict(type=kind,topic="tickers."+S,cs=cs,
            ts=round(now[0]*1000) if stamp is None else stamp,
            data=dict(symbol=S,**data)),generation=stream._recovery._generation)
    try:
        yield now,stream,reserve,frame,events
    finally: stream.stop()

def test_recovery_maintains_same_topic_without_repeated_subscription(feed):
    now,stream,sock,frame,events=feed
    assert frame(1,"snapshot",markPrice="100")
    original=round(now[0]*1000)
    now[0]+=4.
    assert stream.independent_observations_for_hub(S)[0] is None
    assert frame(2,indexPrice="101")
    mark,_=stream.independent_observations_for_hub(S)
    assert mark[0]==100 and mark[3]==original and mark[1]==round(now[0]*1000)/1000
    before=len(sock.messages)
    assert stream.refresh_stale_marks([S])==0
    assert len(sock.messages)==before
    assert stream.transport_health()["mark_union"]["priority_fresh"]==1

def test_wire_resubscription_retires_chain_until_real_new_snapshot(feed):
    now,stream,sock,frame,events=feed
    assert frame(1,"snapshot",markPrice="100")
    now[0]+=1.8
    # Still valid evidence is not retired by the earlier refresh lead.
    assert stream.refresh_stale_marks([S])==0
    assert stream.independent_observations_for_hub(S)[0] is not None
    now[0]+=.6
    old=round(now[0]*1000)
    assert stream.refresh_stale_marks([S])==1
    assert stream.independent_observations_for_hub(S)[0] is None
    assert not frame(2,"snapshot",markPrice="999")
    unsubscribe=sock.messages[-1]
    child=stream._recovery
    def ack(msg):
        assert child._handle_control_message(sock,dict(op=msg["op"],
            req_id=msg["req_id"],success=True),child._generation)
    ack(unsubscribe)
    assert not frame(3,"snapshot",markPrice="999")
    ack(sock.messages[-1])
    assert stream.independent_observations_for_hub(S)[0] is None
    now[0]+=.2
    frame(4,markPrice="999")
    assert stream.independent_observations_for_hub(S)[0] is None
    frame(5,"snapshot",stamp=old,markPrice="999")
    assert stream.independent_observations_for_hub(S)[0] is None
    assert frame(6,"snapshot",markPrice="102")
    assert stream.independent_observations_for_hub(S)[0][0]==102
    child.disconnect()
    assert stream.independent_observations_for_hub(S)[0] is None

def test_recovery_removal_revokes_price_even_while_control_pending(feed):
    now,stream,sock,frame,_=feed
    assert frame(1,"snapshot",markPrice="100")
    now[0]+=2.4
    assert stream.refresh_stale_marks([S])==1
    stream.sync_symbols([])
    assert stream.independent_observations_for_hub(S)==(None,None)
    assert not frame(2,"snapshot",markPrice="999")

def test_mark_balanced_queue_reserves_tickers_without_starving_depth():
    q=MarketEventQueue()
    depth=[("depth50",f"D{i}") for i in range(100)]
    marks=[("tickers",f"M{i}") for i in range(30)]
    regular=[("kline1",f"K{i}") for i in range(20)]
    q.configure(set(depth+marks+regular),set(depth+marks))
    for key in depth+regular+marks: assert q.notify(*key)
    batch=q.take_mark_balanced_batch(timeout=0)
    assert len(batch)==64 and len(set(batch))==64
    assert batch[:16]==marks[:16]
    assert any(k[0]=="depth50" for k in batch)
    assert any(k[0]=="kline1" for k in batch)
    remaining=q.take_mark_balanced_batch(timeout=0)+q.take_mark_balanced_batch(timeout=0)
    assert set(batch+remaining)==set(depth+marks+regular)
    assert q.stats()["pending"]==0

def test_mark_balanced_queue_respects_revocation_and_coalesces_duplicates():
    q=MarketEventQueue()
    q.configure({("tickers",S),("depth50",S)})
    for _ in range(100): assert q.notify("tickers",S)
    q.notify("depth50",S)
    q.configure({("depth50",S)})
    assert not q.notify("tickers",S)
    assert q.take_mark_balanced_batch(timeout=0)==[("depth50",S)]


def test_recovery_evidence_reaches_real_hub_cache_and_is_revoked(feed,tmp_path):
    from bcc.market import PublicData
    from bcc.market_hub import MarketDataHub
    now,stream,sock,frame,events=feed
    url="https://api.bybit.com/v5/market/tickers"
    params={"category":"linear","symbol":S}
    source=PublicData(quote_stream=stream)
    hub=MarketDataHub(tmp_path/"cache.sqlite",source=source)
    try:
        source.shared.bybit_ws_source_deadlines=True
        hub.registry.subscribe(url,params,priority=True)
        assert hub.acquire_leadership()
        assert frame(1,"snapshot",markPrice="100")
        original=round(now[0]*1000)
        now[0]+=4
        assert frame(2,indexPrice="101")
        assert hub.publish_ws_event("tickers",S)
        row=source.shared.read_fresh(url,params,"api.bybit.com")
        assert row["_bcc_ws_mark_explicit_at"]==original
        assert row["_bcc_ws_mark_at"]==round(now[0]*1000)
        assert PublicData._decode_ws_position_mark(S,row)["bid"]==100.
        stream._recovery.disconnect()
        assert hub.publish_ws_event("tickers",S)
        assert source.shared.read_fresh(url,params,"api.bybit.com") is None
    finally:
        hub.close()
