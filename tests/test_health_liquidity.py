"""Source checks distinguish an empty outcome book from transport failures."""

import pytest

from bcc import health
from bcc.market import DataUnavailable


def source_fixture(monkeypatch, books):
    closed = []
    calls = []

    class Source:
        def discover(self, *args):
            return {
                "slug": "test-5m-market",
                "start": 1000,
                "end": 1300,
                "tokens": {"UP": "up", "DOWN": "down"},
            }

        def execution_terms(self, market):
            return {"min_size": 5, "fee_schedule": {"rate": 0.07, "exponent": 1}}

        def book(self, token):
            calls.append(token)
            value = books[token]
            if isinstance(value, Exception):
                raise value
            return value, 1001

        def close(self):
            closed.append(True)

    monkeypatch.setattr(health, "PolymarketData", Source)
    return closed, calls


@pytest.mark.parametrize("up", [[], [{"price": "0.4", "size": "0"}]])
def test_empty_up_uses_verified_down_depth(monkeypatch, up):
    closed, calls = source_fixture(
        monkeypatch, {"up": up, "down": [{"price": "0.6", "size": "50"}]}
    )
    result = health.check_polymarket()
    assert result["outcome"] == "DOWN" and result["best_ask"] == 0.6
    assert result["checked_outcomes"]["UP"]["status"] == "NO_LIQUIDITY"
    assert calls == ["up", "down"] and closed == [True]


def test_both_empty_are_explicitly_not_a_successful_liquidity_check(monkeypatch):
    closed, _ = source_fixture(monkeypatch, {"up": [], "down": []})
    with pytest.raises(health.NoLiquidity, match="Нет исполнимых ASK"):
        health.check_polymarket()
    assert closed == [True]
    monkeypatch.setattr(health, "check_bybit", lambda: {})
    monkeypatch.setattr(health, "check_signal_source", lambda: {})
    report = health.check_sources()
    assert not report["all_available"]
    row = next(x for x in report["checks"] if "Polymarket" in x["source"])
    assert row["status"] == "NO_LIQUIDITY"


@pytest.mark.parametrize("value", [float("nan"), -1])
def test_bad_depth_is_not_classified_as_empty_liquidity(monkeypatch, value):
    source_fixture(monkeypatch, {"up": [{"price": value, "size": 5}], "down": []})
    with pytest.raises(DataUnavailable, match="Повреждён стакан"):
        health.check_polymarket()


@pytest.mark.parametrize("down", [
    DataUnavailable("DOWN book unavailable"),
    [{"price": "nan", "size": "50"}],
    [{"price": "-0.1", "size": "50"}],
])
def test_healthy_up_cannot_hide_invalid_down(monkeypatch, down):
    closed, calls = source_fixture(
        monkeypatch, {"up": [{"price": "0.4", "size": "50"}], "down": down}
    )
    with pytest.raises(DataUnavailable, match="Стаканы текущего рынка недоступны"):
        health.check_polymarket()
    assert calls == ["up", "down"]
    assert closed == [True]


def test_health_pass_checks_both_liquid_outcomes(monkeypatch):
    closed, calls = source_fixture(monkeypatch, {
        "up": [{"price": "0.4", "size": "50"}],
        "down": [{"price": "0.6", "size": "50"}],
    })
    result = health.check_polymarket()
    assert result["outcome"] == "UP"
    assert result["checked_outcomes"]["DOWN"]["best_ask"] == 0.6
    assert calls == ["up", "down"] and closed == [True]


def test_healthy_up_with_empty_down_is_diagnosed_not_hidden(monkeypatch):
    _, calls = source_fixture(monkeypatch, {
        "up": [{"price": "0.4", "size": "50"}], "down": []
    })
    result = health.check_polymarket()
    assert result["checked_outcomes"]["DOWN"]["status"] == "NO_LIQUIDITY"
    assert calls == ["up", "down"]
