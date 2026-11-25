"""Bounded depth publication, actual kline confirmation and minute retry."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from bcc import ws_dispatch
from bcc.ws_dispatch import MarketEventQueue
from bcc.ws_klines import BybitKlineWS
from bcc.hub_minute_repair import URL, HOST, identity, params_for, retry_candidates


def test_depth_coalesces_without_losing_final_notification_or_delaying_other_sources(monkeypatch):
    now = [100.]
    monkeypatch.setattr(ws_dispatch, "time", SimpleNamespace(monotonic=lambda: now[0]))
    q = MarketEventQueue()
    depth = ("depth50", "BTCUSDT")
    mark = ("tickers", "BTCUSDT")
    candle = ("kline1", "BTCUSDT")
    q.configure({depth, mark, candle}, {depth, mark})
    assert q.notify(*depth)
    assert q.take_mark_balanced_batch(timeout=0, depth_interval=.25) == [depth]
    for _ in range(100):
        assert q.notify(*depth)
    assert q.notify(*mark) and q.notify(*candle)
    now[0] += .1
    assert set(q.take_mark_balanced_batch(timeout=0, depth_interval=.25)) == {mark, candle}
    assert q.stats()["pending"] == 1
    now[0] = 100.25
    assert q.take_mark_balanced_batch(timeout=0, depth_interval=.25) == [depth]
    assert q.stats()["pending"] == 0
    assert q.stats()["depth_delayed_batches"] >= 1
    q.configure({mark})
    assert not q._depth_dispatched


def test_balanced_flood_drain_is_unique_and_keeps_all_active_topics(monkeypatch):
    now = [100.]
    monkeypatch.setattr(ws_dispatch, "time", SimpleNamespace(monotonic=lambda: now[0]))
    q = MarketEventQueue()
    names = [f"C{i}USDT" for i in range(59)]
    active = {(kind, s) for kind in ("depth50", "tickers", "kline1") for s in names}
    q.configure(active, active)
    for item in active:
        assert q.notify(*item)
    delivered = []
    while q.stats()["pending"]:
        batch = q.take_mark_balanced_batch(timeout=0, depth_interval=.25)
        assert batch and len(batch) == len(set(batch)) <= 64
        delivered.extend(batch)
    assert len(delivered) == len(active) and set(delivered) == active


def _candle(start, confirmed):
    return dict(start=start, end=start+59999, interval="1", open="100",
                high="102", low="99", close="101", volume="2", confirm=confirmed)


def test_multiline_kline_keeps_confirmed_previous_minute_and_new_forming_minute():
    boundary = 60060000
    now = (boundary+500)/1000
    stream = BybitKlineWS(wall=lambda: now, monotonic=lambda: 100., retain_confirmed=True)
    stream.subscribe("BTCUSDT")
    gen = stream.connect()
    payload = dict(topic="kline.1.BTCUSDT", type="snapshot", ts=boundary+500,
                   data=[_candle(boundary, False), _candle(boundary-60000, True)])
    assert stream.ingest(payload, generation=gen)
    assert stream.get_candle("BTCUSDT").start_ms == boundary
    rows = stream.get_confirmed_candles("BTCUSDT")
    assert len(rows) == 1 and rows[0]["t"] == boundary-60000
    stats = stream.transport_health()
    assert stats["wire_messages"] == stats["wire_confirmed_rows"] == stats["multi_row_messages"] == 1
    assert stats["confirmed_frames"] == stats["retained"] == 1
    assert stats["accepted"] == 2


def test_unconfirmed_duplicate_and_wrong_epoch_never_create_finality():
    boundary = 60060000
    stream = BybitKlineWS(wall=lambda: (boundary+500)/1000,
                         monotonic=lambda: 100., retain_confirmed=True)
    stream.subscribe("BTCUSDT")
    gen = stream.connect()
    payload = dict(topic="kline.1.BTCUSDT", type="snapshot", ts=boundary+500,
                   data=[_candle(boundary-60000, False), _candle(boundary, False)])
    assert stream.ingest(payload, generation=gen)
    assert not stream.get_confirmed_candles("BTCUSDT")
    payload["data"] = [_candle(boundary-60000, False), _candle(boundary-60000, True)]
    assert not stream.ingest(payload, generation=gen)
    assert not stream.get_confirmed_candles("BTCUSDT")
    assert stream.transport_health()["rejected_envelopes"] == 1
    payload["data"] = [_candle(boundary-60000, True)]
    assert not stream.ingest(payload, generation=gen-1)
    assert not stream.get_confirmed_candles("BTCUSDT")
    assert stream.transport_health()["wire_confirmed_rows"] == 2


def test_retry_candidates_only_missing_without_live_demand_or_http_lease():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE hub_subscriptions(k TEXT PRIMARY KEY,url TEXT,params TEXT,active_until REAL);
        CREATE TABLE lease(k TEXT PRIMARY KEY,until REAL);
        CREATE TABLE cache(k TEXT PRIMARY KEY,value TEXT,host TEXT,until REAL);
    """)
    start = 60000000
    now_ms = start+62000
    now = 10.
    symbols = ["READYUSDT","BUSYUSDT","EXPIREDUSDT","DONEUSDT","NOPARENTUSDT"]
    for symbol in symbols[:-1]:
        parent = dict(category="linear", symbol=symbol, interval="1", limit=10)
        db.execute("INSERT INTO hub_subscriptions VALUES(?,?,?,?)",
                   (identity(parent), URL, json.dumps(parent, sort_keys=True,separators=(",",":")), 90.))
    for symbol, until in (("READYUSDT",90.),("EXPIREDUSDT",1.)):
        params = params_for(symbol,start)
        db.execute("INSERT INTO hub_subscriptions VALUES(?,?,?,?)",
                   (identity(params),URL,json.dumps(params,sort_keys=True,separators=(",",":")),until))
    db.execute("INSERT INTO lease VALUES(?,?)",(identity(params_for("BUSYUSDT",start)),30.))
    snapshot = dict(retCode=0,time=now_ms,result=dict(category="linear",symbol="DONEUSDT",
        list=[[str(start),"100","102","99","101","2"]]))
    db.execute("INSERT INTO cache VALUES(?,?,?,?)",
               (identity(params_for("DONEUSDT",start)),json.dumps(snapshot),HOST,90.))
    assert retry_candidates(db,symbols,start,now,now_ms) == ["EXPIREDUSDT"]
    db.execute("DELETE FROM lease")
    assert retry_candidates(db,symbols,start,now,now_ms) == ["BUSYUSDT","EXPIREDUSDT"]
    db.close()
