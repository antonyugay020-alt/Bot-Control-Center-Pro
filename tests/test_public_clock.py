"""Public UTC intervals handle the observed PC skew without relaxing depth age."""

from copy import deepcopy

import pytest

from bcc import market_clock as mc
from bcc.config import defaults
from bcc.execution import ExecutionRejected, PaperExecutor
from bcc.market import DataUnavailable, PolymarketData, PublicData, check_book_time


@pytest.fixture
def measured(monkeypatch):
    timer = {"wall": 1000.0, "mono": 100.0}
    monkeypatch.setattr(mc.time, "time", lambda: timer["wall"])
    monkeypatch.setattr(mc.time, "monotonic", lambda: timer["mono"])
    reference = mc.MarketClock()
    monkeypatch.setattr(mc, "clock", reference)
    monkeypatch.setattr("bcc.market.clock", reference)

    def sample(offset, rtt=0.25):
        return {
            "http_status": 200,
            "cached_response": False,
            "local_clock_changed_during_request": False,
            "server_unix": timer["wall"] + offset,
            "server_precision_seconds": 0.001,
            "round_trip_seconds": rtt,
            "local_received_unix": timer["wall"],
            "received_monotonic": timer["mono"],
        }

    def probe(provider):
        offset = 0.594 if provider == "Bybit" else 0.539
        rtt = 0.254 if provider == "Bybit" else 0.358
        return {
            "source": provider,
            "samples": [sample(offset, rtt), sample(offset, rtt)],
        }

    monkeypatch.setattr(mc, "probe_source", probe)
    return reference, timer, sample


def test_observed_skew_accepts_fresh_depth_and_executor_rechecks_delay(measured):
    reference, timer, _ = measured
    reference.ensure()
    low, high = reference.bounds()
    assert (low, high) == pytest.approx((1000.594, 1000.849))
    assert mc.market_now() == low  # Closed candles cannot become ready early.
    assert mc.latest_now() == high
    assert timer["wall"] == 1000  # Never changes the OS clock.
    check_book_time(1000.636, "Polymarket")
    book = dict(
        at=1000.636,
        bids=[[9.9, 100]],
        asks=[[10, 100]],
        qty_step=1,
        min_qty=1,
        min_notional=1,
    )
    PaperExecutor(deepcopy(book)).estimate(
        "LONG", "ENTRY", defaults("VELES")["settings"], quantity=1
    )
    timer["wall"] += 3
    timer["mono"] += 3
    with pytest.raises(ExecutionRejected):
        PaperExecutor(book).estimate(
            "LONG", "ENTRY", defaults("VELES")["settings"], quantity=1
        )
    with pytest.raises(DataUnavailable):
        check_book_time(book["at"], "Polymarket")


@pytest.mark.parametrize("stamp", [997.8, 1001.2, float("nan"), float("inf")])
def test_rejects_old_future_or_nonfinite_books_with_full_uncertainty(measured, stamp):
    measured[0].ensure()
    assert not mc.book_is_fresh(stamp)


@pytest.mark.parametrize(
    "fault", ["cached", "slow", "disagree", "missing", "coarse", "jump"]
)
def test_unreliable_clock_never_enables_execution(measured, monkeypatch, fault):
    reference, _, sample = measured

    def probe(provider):
        a = sample(0.6)
        if provider == "Binance":
            if fault == "cached":
                a["cached_response"] = True
            if fault == "slow":
                a["round_trip_seconds"] = 1.1
            if fault == "disagree":
                a["server_unix"] += 1
            if fault == "coarse":
                a["server_precision_seconds"] = 1
            if fault == "jump":
                a["local_clock_changed_during_request"] = True
            if fault == "missing":
                return {"source": provider, "samples": []}
        return {"source": provider, "samples": [a, a]}

    monkeypatch.setattr(mc, "probe_source", probe)
    with pytest.raises(mc.ClockUnavailable):
        reference.ensure()
    assert not mc.book_is_fresh(1000)


def test_expiry_wall_clock_jump_and_recovery(measured):
    reference, timer, _ = measured
    reference.ensure()
    timer["wall"] += 0.1
    with pytest.raises(mc.ClockUnavailable):
        reference.bounds()
    assert not mc.book_is_fresh(1000.636)
    timer["wall"] += 11
    timer["mono"] += 11
    reference.ensure()
    assert reference.bounds()[0] > 1011
    timer["wall"] += 61
    timer["mono"] += 61
    with pytest.raises(mc.ClockUnavailable):
        reference.bounds()


def test_failed_refresh_retains_only_unexpired_reference(measured, monkeypatch):
    reference, timer, _ = measured
    reference.ensure()
    timer["wall"] += 31
    timer["mono"] += 31
    monkeypatch.setattr(mc, "probe_source", lambda p: {"source": p, "samples": []})
    reference.ensure()
    assert reference.bounds()[0] > 1031
    timer["wall"] += 30
    timer["mono"] += 30
    with pytest.raises(mc.ClockUnavailable):
        reference.ensure()
    assert not mc.book_is_fresh(1061)


def test_real_transports_require_reference_before_requesting_depth(
    measured, monkeypatch
):
    reference, _, _ = measured
    for source, method, arg in [
        (PublicData(), "orderbook", "BTCUSDT"),
        (PolymarketData(), "book", "123"),
    ]:

        def fail():
            raise mc.ClockUnavailable("not verified")

        monkeypatch.setattr(reference, "ensure", fail)
        monkeypatch.setattr(
            source, "get", lambda *a, **kw: pytest.fail("Unverified depth request")
        )
        try:
            with pytest.raises(mc.ClockUnavailable):
                getattr(source, method)(arg)
        finally:
            source.close()
