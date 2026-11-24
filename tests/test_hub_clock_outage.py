"""Clock loss pauses producer admission, never extends evidence or loses leadership."""
import threading
from types import SimpleNamespace

import pytest

from bcc import market_clock as mc
from bcc.market import PublicData
from bcc.market_hub import MarketDataHub
from bcc.hub_minute_repair import URL, params_for


def test_expired_hub_clock_refresh_is_nonblocking_singleflight(monkeypatch):
    timer = {"wall": 1061., "mono": 161.}
    monkeypatch.setattr(mc, "time", SimpleNamespace(
        time=lambda: timer["wall"], monotonic=lambda: timer["mono"]))
    ref = mc.MarketClock()
    ref._reference = (1000., 1000.01, 1000., 100.)
    ref._attempted = 100.
    monkeypatch.setattr(mc, "clock", ref)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def probe(provider):
        calls.append(provider)
        entered.set()
        assert release.wait(timeout=5)
        sample = dict(http_status=200, cached_response=False,
            local_clock_changed_during_request=False,
            server_unix=timer["wall"], server_precision_seconds=.001,
            round_trip_seconds=.01, local_received_unix=timer["wall"],
            received_monotonic=timer["mono"])
        return {"source": provider, "samples": [sample, sample]}
    monkeypatch.setattr(mc, "probe_source", probe)
    caller = threading.Thread(target=ref.poll_refresh)
    caller.start()
    try:
        assert entered.wait(timeout=2)
        caller.join(timeout=1)
        assert not caller.is_alive(), "expired reference blocked service thread"
        original = ref._reference
        refresh = ref._refresh_thread
        for _ in range(100):
            assert ref.poll_refresh() is False
            assert ref._refresh_thread is refresh
        assert ref._reference == original
        with pytest.raises(mc.ClockUnavailable):
            ref.bounds()
        assert not mc.book_is_fresh(timer["wall"])
    finally:
        release.set()
        caller.join(timeout=5)
        if ref._refresh_thread is not None:
            ref._refresh_thread.join(timeout=5)
    assert ref.poll_refresh() is True
    assert set(calls) == set(mc.PRIMARY_CLOCKS)
    assert len(calls) == 2
    assert ref.evidence["status"] == "PASS"
    assert mc.book_is_fresh(timer["wall"])


@pytest.mark.parametrize("workers", [1, 6])
def test_outage_retains_leader_without_market_work(tmp_path, monkeypatch, workers):
    source = PublicData()
    hub = MarketDataHub(tmp_path/"cache.sqlite", source=source, max_workers=workers)
    try:
        assert hub.acquire_leadership()
        token = hub._leader_token
        monkeypatch.setattr(hub, "subscribe_active_websockets",
            lambda: pytest.fail("market work admitted without verified clock"))
        monkeypatch.setattr(hub, "run_once",
            lambda: pytest.fail("single worker market work admitted without clock"))
        hub._leader_check_due = 0.
        assert hub.poll_nonblocking(market_ready=False) == []
        assert hub._leader_held and hub._leader_token == token
        assert hub.registry.leader_token(hub.owner) == token
    finally:
        hub.close()


def test_minute_repair_clock_expiry_at_both_boundaries(tmp_path, monkeypatch):
    source = PublicData()
    hub = MarketDataHub(tmp_path/"cache.sqlite", source=source, max_workers=6)
    source.shared.central_repair = True
    try:
        assert hub.acquire_leadership()
        parent = dict(category="linear", symbol="BTCUSDT", interval="1", limit=10)
        hub.registry.subscribe(URL, parent, priority=True)
        def unavailable():
            raise mc.ClockUnavailable("test expiry")
        monkeypatch.setattr(mc, "market_now", unavailable)
        hub._schedule_minute_repairs(["BTCUSDT"])
        assert hub._repair_scheduled == 0
        now = 60062.
        times = iter([now])
        def mid_transaction():
            try:
                return next(times)
            except StopIteration:
                return unavailable()
        monkeypatch.setattr(mc, "market_now", mid_transaction)
        hub._schedule_minute_repairs(["BTCUSDT"])
        assert hub._repair_scheduled == 0
        assert not hub._repair_seen
        monkeypatch.setattr(mc, "market_now", lambda: now)
        hub._schedule_minute_repairs(["BTCUSDT"])
        assert hub._repair_scheduled == 1
        assert hub._repair_seen == {"BTCUSDT"}
    finally:
        hub.close()
