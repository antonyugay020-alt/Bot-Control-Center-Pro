"""Public WS diagnostic invariants: provenance counters only, never new price authority.

No private account, network connections, orders, filesystem scanning, or
unbounded per-symbol telemetry. All wall/monotonic clocks are injected.
"""
import json
import time

import pytest

from bcc.data_cache import CacheBusy
from bcc.market import PublicData
from bcc.market_hub import MarketDataHub
from bcc.ws_quotes import BybitQuoteStream, StreamUnavailable
from bcc.ws_orderbooks import BybitOrderbookWS


class Socket:
    def __init__(self, *, fail=False):
        self.sent = []
        self.fail = fail
        self.closed = False

    def send(self, payload):
        if self.fail:
            raise OSError("fake socket closed")
        self.sent.append(json.loads(payload))

    def close(self):
        self.closed = True


def test_ticker_diagnostic_distinguishes_original_mark_and_quote_only_update():
    now = [10000.]
    socket = Socket()
    stream = BybitQuoteStream(wall=lambda: now[0], monotonic=lambda: now[0])
    stream.subscribe("BTCUSDT")
    generation = stream.connect(socket)
    original_stamp = int(now[0] * 1000)
    assert stream.ingest({
        "type":"snapshot", "topic":"tickers.BTCUSDT", "ts":original_stamp,
        "data":{"symbol":"BTCUSDT", "markPrice":"100.5",
                "bid1Price":"100", "ask1Price":"101"}
    }, generation=generation)
    before = stream.refresh_pressure()
    assert before["genuine_mark_events"] == 1
    assert before["genuine_quote_events"] == 1
    assert before["connections_opened"] == 1
    now[0] += .2
    assert stream.ingest({
        "type":"delta", "topic":"tickers.BTCUSDT", "ts":int(now[0] * 1000),
        "data":{"symbol":"BTCUSDT", "bid1Price":"100.1"}
    }, generation=generation)
    after = stream.refresh_pressure()
    assert after["genuine_mark_events"] == 1
    assert after["genuine_quote_events"] == 2
    assert stream.mark("BTCUSDT")[1] == original_stamp / 1000


def test_ticker_recovery_commands_are_visible_but_never_refresh_mark():
    now=[1000.]
    stream=BybitQuoteStream(wall=lambda:now[0],monotonic=lambda:now[0])
    stream.subscribe("BTCUSDT")
    stream.set_priority_symbols(["BTCUSDT"])
    sock=Socket()
    stream.connect(sock)
    now[0]+=3.01
    assert stream.refresh_stale_marks(["BTCUSDT"]) == 1
    pressure=stream.refresh_pressure()
    assert pressure["snapshot_topics_requested"]==1
    assert pressure["snapshot_batches_requested"]==1
    assert len(sock.sent)==2
    with pytest.raises(StreamUnavailable):
        stream.mark("BTCUSDT")
    sock.fail=True
    now[0]+=.51
    with pytest.raises(StreamUnavailable):
        stream.refresh_stale_marks(["BTCUSDT"])
    pressure=stream.refresh_pressure()
    assert pressure["snapshot_send_failures"]==1
    assert not stream._connected


def test_depth_counts_real_snapshots_and_recovery_without_synthetic_book():
    now=[10000.]
    sock=Socket()
    stream=BybitOrderbookWS(wall=lambda:now[0],monotonic=lambda:now[0])
    stream.subscribe(["BTCUSDT"])
    gen=stream.connect(sock)
    assert stream.ingest({
        "type":"snapshot", "topic":"orderbook.50.BTCUSDT",
        "ts":int(now[0]*1000),
        "data":{"s":"BTCUSDT","u":1,"seq":1,"b":[["100","1"]],"a":[["101","1"]]}
    },generation=gen)
    pressure=stream.refresh_pressure()
    assert pressure["genuine_book_events"]==1
    assert pressure["genuine_book_snapshots"]==1
    now[0]+=1.2
    assert stream.refresh_stale_books(["BTCUSDT"])==1
    assert stream.refresh_pressure()["snapshot_topics_requested"]==1
    assert stream.get_orderbook("BTCUSDT")["ts"]==10000.0
    now[0]+=2.01
    with pytest.raises(StreamUnavailable):
        stream.get_orderbook("BTCUSDT")


def test_sqlite_publisher_counts_source_rejection_vs_commit_rejection(tmp_path, monkeypatch):
    hub=MarketDataHub(tmp_path/"isolated.sqlite", source=PublicData())
    events=[("tickers","BTCUSDT"),("depth50","ETHUSDT")]
    try:
        hub._leader_token="owned-lease"
        hub._prepare_ws_event=lambda kind,symbol: (
            dict(url="u",params={},host="api.bybit.com",value={},ttl=1.)
            if kind=="tickers" else False)
        monkeypatch.setattr(hub.source.shared,"publish_websockets",
                            lambda payload, **kwargs:[True]*len(payload))
        assert hub.publish_ws_events(events)==[True,False]
        s=hub.ws_publication_stats()
        assert (s["events_seen"],s["prepared"],s["published"],s["rejected_source"],
                s["rejected_store"],s["sqlite_busy"])==(2,1,1,1,0,0)
        assert s["max_prepare_seconds"]>=0
        assert s["max_commit_seconds"]>=0
        assert hub.ws_publication_stats() == s

        def sqlite_busy(*args,**kwargs):
            raise CacheBusy("synthetic locked WAL")
        monkeypatch.setattr(hub.source.shared,"publish_websockets",sqlite_busy)
        assert hub.publish_ws_events(events)==[False,False]
        s=hub.ws_publication_stats()
        assert s["sqlite_busy"]==1
        assert s["rejected_store"]==1
        assert s["rejected_source"]==2
        assert s["published"]==1
    finally:
        hub.close()
        hub.source.close()


def test_publisher_refuses_unowned_leader_without_synthetic_success(tmp_path):
    hub=MarketDataHub(tmp_path/"isolated.sqlite", source=PublicData())
    try:
        hub._prepare_ws_event=lambda kind,symbol: dict(url="x")
        assert hub.publish_ws_event("tickers","BTCUSDT") is False
        s=hub.ws_publication_stats()
        assert s["rejected_leader"]==1
        assert s["rejected_store"]==0
        assert s["published"]==0
    finally:
        hub.close()
        hub.source.close()
