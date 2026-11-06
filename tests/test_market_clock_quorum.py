"""Independent public UTC quorum: fail closed on outages, ambiguity and stale evidence.

The probes below are deterministic responses, not claims of live provider access.
"""
import pytest

from bcc import market_clock as mc


@pytest.fixture
def clock_probe(monkeypatch):
    wall = {"unix": 1000.0, "mono": 100.0}
    monkeypatch.setattr(mc.time, "time", lambda: wall["unix"])
    monkeypatch.setattr(mc.time, "monotonic", lambda: wall["mono"])
    clock = mc.MarketClock()
    answers = {}
    called = []

    def sample(offset, **changes):
        row = {
            "http_status": 200,
            "cached_response": False,
            "local_clock_changed_during_request": False,
            "server_unix": wall["unix"] + offset,
            "server_precision_seconds": 0.001,
            "round_trip_seconds": 0.25,
            "local_received_unix": wall["unix"],
            "received_monotonic": wall["mono"],
        }
        row.update(changes)
        return row

    def healthy(name, offset):
        row = sample(offset)
        answers[name] = {"source": name, "samples": [row.copy(), row.copy()]}

    def blocked(name, code=403):
        answers[name] = {
            "source": name,
            "samples": [{"http_status": code, "error": "HTTP unavailable"}] * 2,
        }

    def probe(name):
        called.append(name)
        return answers[name]

    monkeypatch.setattr(mc, "probe_source", probe)
    return clock, wall, answers, called, healthy, blocked, sample


def test_primary_independent_pair_never_needs_alternate_probe(clock_probe):
    clock, wall, answers, called, healthy, blocked, sample = clock_probe
    healthy("Bybit", 0.60)
    healthy("Binance", 0.62)
    clock.ensure()
    assert set(called) == {"Bybit", "Binance"}
    assert clock.evidence["selected_sources"] == ["Binance", "Bybit"]
    assert clock.evidence["fallback_used"] is False
    assert clock.evidence["os_clock_adjusted"] is False
    assert wall["unix"] == 1000.0


def test_one_primary_unavailable_uses_two_independent_valid_clocks(clock_probe):
    clock, wall, answers, called, healthy, blocked, sample = clock_probe
    blocked("Bybit")
    healthy("Binance", 0.62)
    healthy("OKX", 0.63)
    blocked("Bitget")
    clock.ensure()
    assert clock.evidence["status"] == "PASS"
    assert clock.evidence["selected_sources"] == ["Binance", "OKX"]
    assert clock.evidence["fallback_used"] is True
    assert set(called) == {"Bybit", "Binance", "OKX", "Bitget"}
    low, high = clock.bounds()
    assert low == pytest.approx(1000.63)
    assert high == pytest.approx(1000.871)


def test_both_primary_unavailable_still_requires_two_alternates(clock_probe):
    clock, wall, answers, called, healthy, blocked, sample = clock_probe
    blocked("Bybit")
    blocked("Binance", 451)
    healthy("OKX", 0.60)
    healthy("Bitget", 0.61)
    clock.ensure()
    assert clock.evidence["selected_sources"] == ["Bitget", "OKX"]
    assert clock.evidence["fallback_used"] is True


@pytest.mark.parametrize("bad", ["no_second_provider", "cached", "slow", "jump", "coarse"])
def test_single_usable_clock_never_authorizes_paper(clock_probe, bad):
    clock, wall, answers, called, healthy, blocked, sample = clock_probe
    blocked("Bybit")
    healthy("Binance", 0.60)
    blocked("Bitget")
    if bad == "no_second_provider":
        blocked("OKX")
    else:
        field, value = {
            "cached": ("cached_response", True),
            "slow": ("round_trip_seconds", 1.5),
            "jump": ("local_clock_changed_during_request", True),
            "coarse": ("server_precision_seconds", 1.0),
        }[bad]
        answers["OKX"] = {
            "source": "OKX",
            "samples": [sample(0.61, **{field: value})] * 2,
        }
    with pytest.raises(mc.ClockUnavailable, match="Нет двух независимых"):
        clock.ensure()
    assert clock.evidence["status"] == "UNAVAILABLE"
    assert clock._reference is None


def test_disjoint_two_against_two_is_ambiguous_not_success(clock_probe):
    clock, wall, answers, called, healthy, blocked, sample = clock_probe
    healthy("Bybit", 0.60)
    healthy("Binance", 1.60)
    healthy("OKX", 0.62)
    healthy("Bitget", 1.62)
    with pytest.raises(mc.ClockUnavailable, match="Неоднозначный кворум"):
        clock.ensure()
    assert clock.evidence["status"] == "UNAVAILABLE"


def test_unique_three_provider_consensus_rejects_one_outlier(clock_probe):
    clock, wall, answers, called, healthy, blocked, sample = clock_probe
    healthy("Bybit", 0.60)
    healthy("Binance", 1.60)
    healthy("OKX", 0.62)
    healthy("Bitget", 0.63)
    clock.ensure()
    assert clock.evidence["selected_sources"] == ["Bitget", "Bybit", "OKX"]
    assert "Binance" not in clock.evidence["selected_sources"]


def test_spoofed_source_and_inconsistent_samples_fail_closed(clock_probe):
    clock, wall, answers, called, healthy, blocked, sample = clock_probe
    healthy("Bybit", 0.60)
    blocked("Binance")
    answers["OKX"] = {"source": "Binance", "samples": [sample(0.61)] * 2}
    answers["Bitget"] = {
        "source": "Bitget",
        "samples": [sample(0.62), sample(1.62)],
    }
    with pytest.raises(mc.ClockUnavailable):
        clock.ensure()
    assert "OKX" in clock.evidence["provider_errors"]
    assert "Bitget" in clock.evidence["provider_errors"]


def test_alternate_reference_expires_and_does_not_permit_stale_books(clock_probe):
    clock, wall, answers, called, healthy, blocked, sample = clock_probe
    blocked("Bybit")
    blocked("Binance")
    healthy("OKX", 0.60)
    healthy("Bitget", 0.61)
    clock.ensure()
    wall["unix"] += 61
    wall["mono"] += 61
    with pytest.raises(mc.ClockUnavailable, match="Истёк срок"):
        clock.bounds()
