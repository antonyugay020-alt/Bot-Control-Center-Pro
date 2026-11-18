"""Market-health observation timestamps are separate from stale errors."""
from bcc.market_health import record_market_outcome
from bcc.support import _market_health_summary


def test_observed_failure_and_recovery_clocks(monkeypatch):
    monkeypatch.setattr("bcc.market_health.time.time", lambda: 1000.0)
    health = {}
    assert record_market_outcome(health, "BTCUSDT", quote="Hub timeout")
    assert health["last_failed_utc"]["quote"] == 1000.0
    monkeypatch.setattr("bcc.market_health.time.time", lambda: 1002.0)
    assert not record_market_outcome(health, "BTCUSDT", quote="Hub timeout")
    assert health["last_failed_utc"]["quote"] == 1002.0
    monkeypatch.setattr("bcc.market_health.time.time", lambda: 1005.0)
    assert record_market_outcome(health, "BTCUSDT", quote="")
    assert health["last_recovered_utc"]["quote"] == 1005.0
    assert health["unavailable_symbols"] == {}
    assert not record_market_outcome(health, "BTCUSDT", quote="")
    assert health["last_recovered_utc"]["quote"] == 1005.0


def test_report_privacy_and_clock_validation():
    health = {
        "unavailable_symbols": {"BTCUSDT": {"quote": "Hub timeout"}},
        "last_failed_utc": {"quote": 150., "history": float("nan"), "secret": 160.},
        "last_recovered_utc": {"quote": 300., "history": 140.},
    }
    report = _market_health_summary({"market_health": health}, "Работает", now=200.)
    assert report["last_failed_utc"] == {"quote": 150.}
    assert report["last_recovered_utc"] == {"history": 140.}
    assert report["unavailable_counts"] == {"quote": 1, "history": 0}
    assert "BTCUSDT" not in str(report)
    assert "secret" not in str(report)
