"""Bybit first-stage acceptance does not retire other venue adapters or certify release."""

from pathlib import Path

import pytest

import bcc.health as health
from tools import verify_windows, windows_check


def test_bybit_health_scope_never_probes_deferred_market_venues(monkeypatch):
    called = []

    def success():
        called.append("bybit")
        return {"book_fresh": True, "book_age_upper": 0.25}

    def forbidden():
        raise AssertionError("Deferred market venue must not be probed")

    monkeypatch.setattr(health, "check_bybit", success)
    monkeypatch.setattr(health, "check_polymarket", forbidden)
    monkeypatch.setattr(health, "check_signal_source", forbidden)
    result = health.check_sources(venues=("bybit",))
    assert called == ["bybit"]
    assert result["all_available"] is True
    assert result["scope"] == ["bybit"]
    assert len(result["checks"]) == 1
    assert result["checks"][0]["source"] == "Bybit USDT Futures"
    assert result["read_only"] is True


def test_bybit_failure_fails_only_its_own_gate(monkeypatch):
    def blocked():
        raise RuntimeError("Bybit market unavailable")
    monkeypatch.setattr(health, "check_bybit", blocked)
    monkeypatch.setattr(health, "check_polymarket", lambda: {"ok": True})
    monkeypatch.setattr(health, "check_signal_source", lambda: {"ok": True})
    result = health.check_sources(venues=("bybit",))
    assert result["all_available"] is False
    assert result["checks"][0]["status"] == "UNAVAILABLE"
    assert len(result["checks"]) == 1


@pytest.mark.parametrize("scope", [(), ("bad",), ("bybit", "bybit")])
def test_invalid_or_duplicate_health_scope_fails_closed(scope):
    with pytest.raises(ValueError, match="scope"):
        health.check_sources(venues=scope)


def test_bybit_phase_keeps_global_release_gate_and_qt_tests_intact():
    sources = {
        "native": Path(verify_windows.__file__).read_text(encoding="utf-8"),
        "launcher": Path(windows_check.__file__).read_text(encoding="utf-8"),
    }
    assert 'choices=("all", "bybit")' in sources["native"]
    assert '"release_ready": False' in sources["native"]
    assert 'release_ready=False' in sources["launcher"]
    assert 'deferred_venues' in sources["native"]
    assert '"BYBIT_PHASE_AUTOMATED_PASS"' in sources["native"]
    assert '["-m", "bcc.health", "--output", str(output / "public_sources.json"),' in sources["native"]
    assert '"--venue", args.venue' in sources["native"]
    assert 'expected = ({"Bybit USDT Futures"}' in sources["native"]
    assert '["polymarket", "binance"]' in sources["native"]
    assert '["polymarket", "binance"]' in sources["launcher"]


def test_legacy_all_sources_scope_still_probes_every_provider(monkeypatch):
    seen = []
    def provider(name):
        def go():
            seen.append(name)
            return {"source": name}
        return go
    monkeypatch.setattr(health, "check_bybit", provider("bybit"))
    monkeypatch.setattr(health, "check_polymarket", provider("polymarket"))
    monkeypatch.setattr(health, "check_signal_source", provider("binance"))
    result = health.check_sources()
    assert set(seen) == {"bybit", "polymarket", "binance"}
    assert result["all_available"] is True
    assert result["scope"] == ["bybit", "polymarket", "binance"]
