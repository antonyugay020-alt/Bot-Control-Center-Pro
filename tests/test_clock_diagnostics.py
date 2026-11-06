"""Read-only clock evidence accounts for network and coarse server precision."""

import pytest

from bcc.clock_diagnostics import parse_time, probe_source, time_bounds


def test_clock_parsers_keep_polymarket_whole_second_uncertainty():
    assert parse_time("Polymarket", 1700000000) == (1700000000, 1.0)
    assert parse_time("Binance", {"serverTime": 1700000000123}) == (
        1700000000.123,
        0.001,
    )
    assert parse_time(
        "Bybit", {"retCode": 0, "result": {"timeNano": "1700000000123000000"}}
    )[0] == pytest.approx(1700000000.123)
    with pytest.raises(ValueError):
        parse_time("Polymarket", True)


def test_clock_interval_and_clock_jump_are_explicit():
    sample = time_bounds(1001, 1.0, 1000, 1000.4, 0.4)
    assert sample["server_minus_pc_lower_seconds"] == pytest.approx(0.6)
    assert sample["server_minus_pc_upper_seconds"] == pytest.approx(2.0)
    jump = time_bounds(1001, 1.0, 1000, 1005, 0.4)
    assert jump["local_clock_changed_during_request"]
    assert jump["server_minus_pc_lower_seconds"] is None


def test_clock_probe_uses_readonly_time_endpoint_and_handles_absent_cache_headers(
    monkeypatch,
):
    calls = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return type(
                "Response",
                (),
                {
                    "status_code": 200,
                    "headers": {},
                    "raise_for_status": lambda self: None,
                    "json": lambda self: 1700000000,
                },
            )()

    monkeypatch.setattr("bcc.clock_diagnostics.requests.Session", Session)
    result = probe_source("Polymarket", count=1)
    assert calls[0][0] == "https://clob.polymarket.com/time"
    assert result["samples"][0]["cached_response"] is False
    assert "error" not in result["samples"][0]


def test_failed_book_health_collects_clock_evidence_without_passing_source(monkeypatch):
    from bcc import health
    from bcc.market import DataUnavailable

    monkeypatch.setattr(health, "check_bybit", lambda: {"ok": True})
    monkeypatch.setattr(health, "check_signal_source", lambda: {"ok": True})

    def failed():
        raise DataUnavailable("Устаревший стакан Polymarket: возраст=-0.625")

    monkeypatch.setattr(health, "check_polymarket", failed)
    evidence = {"read_only": True, "clock_adjusted": False}
    monkeypatch.setattr("bcc.clock_diagnostics.probe_clocks", lambda: evidence)
    report = health.check_sources()
    assert not report["all_available"]
    assert report["clock_diagnostics"] == evidence
