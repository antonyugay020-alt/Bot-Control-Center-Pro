"""More precise diagnostics retain the existing execution freshness limits."""

import pytest

from bcc.market import DataUnavailable, check_book_time


@pytest.mark.parametrize("age", [-0.5, 0, 3])
def test_book_time_accepts_only_existing_freshness_window(monkeypatch, age):
    monkeypatch.setattr("bcc.market.time.time", lambda: 1000)
    check_book_time(1000 - age, "Bybit")


@pytest.mark.parametrize("age", [-0.6, 3.1])
def test_book_time_reports_clock_difference_without_allowing_old_execution(
    monkeypatch, age
):
    monkeypatch.setattr("bcc.market.time.time", lambda: 1000)
    with pytest.raises(DataUnavailable) as error:
        check_book_time(1000 - age, "Bybit")
    text = str(error.value)
    assert f"возраст=[{age:+.3f}, {age:+.3f}]" in text
    assert "локальное UTC=1000.000" in text and "время стакана UTC=" in text
    assert ("опережает сверенное UTC" in text) == (age < 0)


def test_slow_bybit_and_binance_still_form_precise_independent_utc():
    """Replay the bounded timing envelopes observed by Windows native QA.

    Two valid Bybit responses can each span >0.5s due to public network RTT.
    Their intersection with independently checked Binance (<0.5s) is precise
    enough for trading. No single provider is ever sufficient.
    """
    from bcc.market_clock import (
        ClockUnavailable, _clock_consensus, _validated_clock_sources,
    )
    wall = 1794664657.292018
    anchor = 445533.0137443
    def sample(server, received, rtt, received_wall, precision):
        elapsed = anchor - received
        return {
            "http_status": 200, "cached_response": False,
            "local_clock_changed_during_request": False,
            "server_unix": server, "received_monotonic": received,
            "local_received_unix": received_wall,
            "round_trip_seconds": rtt,
            "server_precision_seconds": precision,
        }
    bybit = {
        "source": "Bybit",
        "samples": [
            sample(1794664656.8539157, 445532.2441773,
                   .566595500044059, 1794664656.5224514, .000001),
            sample(1794664657.6236072, 445533.0137443,
                   .7695407000137493, 1794664657.292018, .000001),
        ],
    }
    binance = {
        "source": "Binance",
        "samples": [
            sample(1794664656.844, 445532.2308739,
                   .5530409999773838, 1794664656.5091474, .001),
            sample(1794664657.156, 445532.5434954,
                   .3125775000080466, 1794664656.8217692, .001),
        ],
    }
    intervals, errors = _validated_clock_sources(
        [("Bybit", bybit), ("Binance", binance)], wall, anchor)
    assert not errors
    assert intervals["Bybit"][1] - intervals["Bybit"][0] > .5
    low, high, quorum = _clock_consensus(intervals, wall)
    assert quorum == ("Binance", "Bybit")
    assert 0 <= high - low <= .5
    with pytest.raises(ClockUnavailable, match="Нет двух независимых"):
        _clock_consensus({"Bybit": intervals["Bybit"]}, wall)


def test_high_latency_cannot_weaken_executable_clock_quorum():
    """Wider provider envelopes cannot relax the 0.5s consensus gate."""
    from bcc.market_clock import ClockUnavailable, _clock_consensus
    with pytest.raises(ClockUnavailable, match="Нет двух независимых"):
        _clock_consensus({
            "Bybit": (1000, 1000.9),
            "Binance": (1000, 1000.8),
        }, wall=1000)
