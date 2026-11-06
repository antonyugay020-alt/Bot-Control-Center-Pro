"""A corrupted CLOB level must never become spendable PAPER equity."""
import math

import pytest

from bcc.market import DataUnavailable, PolymarketData


def _mark(monkeypatch, bids, asks):
    import bcc.market as market

    feed = PolymarketData()
    monkeypatch.setattr(market.clock, "ensure", lambda: None)
    monkeypatch.setattr(market, "book_is_fresh", lambda at: True)
    monkeypatch.setattr(market, "observed_quote", lambda quote: quote)
    monkeypatch.setattr(feed, "get", lambda url, params: {
        "asset_id": "up-token", "timestamp": "1234000", "bids": bids, "asks": asks,
    })
    return feed.outcome_quote("up-token")


@pytest.mark.parametrize("level", [
    {"price": "0.1", "size": "-1"},
    {"price": "0.1", "size": "nan"},
    {"price": "0.1", "size": "inf"},
    {"price": "0.1", "size": "broken"},
    {"price": "nan", "size": "2"},
    {"price": "-0.1", "size": "2"},
    {"price": "1.2", "size": "2"},
    {"price": "0.1"},
])
def test_corrupt_book_level_is_not_ignored_if_good_liquidity_exists(monkeypatch, level):
    with pytest.raises(DataUnavailable, match="Повреждён стакан"):
        _mark(monkeypatch,
              [level, {"price": "0.4", "size": "25"}],
              [{"price": "0.6", "size": "25"}])


def test_nonpositive_depth_with_valid_other_levels(monkeypatch):
    quote = _mark(monkeypatch,
                  [{"price": "0.2", "size": "0"}, {"price": "0.4", "size": "25"}],
                  [{"price": "0.6", "size": "25"}])
    assert quote["bid"] == pytest.approx(0.4)
    assert quote["ask"] == pytest.approx(0.6)


@pytest.mark.parametrize("attempt_new_entry", [False, True])
def test_failed_quote_refresh_invalidates_previous_spendable_mark(attempt_new_entry):
    import time
    from bcc.storage import initial_state
    from bcc.trading import equity, available_margin
    from bcc.worker import process_polymarket
    from test_poly_contract_seconds import ScheduledSource, cfg

    state = initial_state(500)
    state["cash"] = 475.0
    state["positions"]["old-market"] = {
        "kind": "POLYMARKET", "token": "old-token", "symbol": "BTC",
        "section": "BTC:300", "end": 9e12, "cost": 25.0,
        "shares": 50.0, "side": "UP", "fee": 0.0,
    }
    state["marks"]["old-token"] = {
        "bid": 0.5, "ask": 0.52, "at": time.time(),
        "seen_at": time.time(), "seen_mono": time.monotonic(),
        "source_age_low": 0.0, "source_age_high": 0.0,
    }
    assert equity(state)[1] is False
    source = ScheduledSource(42)
    source.outcome_quote = lambda token: (_ for _ in ()).throw(
        DataUnavailable("CLOB book rejected")
    )
    config = cfg()
    config["symbols"] = ["BTC"] if attempt_new_entry else []
    config["reinvest"] = True
    events = []
    process_polymarket(config, state, events, source, source.now)
    assert events == []
    # The previous mark was fresh a moment ago, but the new CLOB evidence is
    # invalid. Risk calculations must not keep treating it as spendable.
    assert "old-token" not in state["marks"]
    assert equity(state)[1] is True
    assert available_margin(state) == -math.inf
