"""Fail-closed UP/DOWN metadata handling and official PAPER settlement."""

from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from bcc.config import validate
from bcc.market import (DataUnavailable, PolymarketData,
                        canonical_up_down_tokens)
from bcc.storage import Store, initial_state
from bcc.worker import process_polymarket
from test_poly_contract_seconds import ScheduledSource, cfg


def small_cfg():
    config = cfg()
    config["symbols"] = ["BTC"]
    config["polymarket"]["durations"] = [300]
    return validate(config)


def test_exact_binary_tokens_only():
    assert canonical_up_down_tokens({"UP": "up-token", "DOWN": "down-token"}) == {
        "UP": "up-token", "DOWN": "down-token"
    }
    invalid = [
        None, [], {}, {"UP": "a"}, {"UP": "a", "DOWN": "a"},
        {"UP": "a", "DOWN": "b", "OTHER": "c"},
        {"UP": "", "DOWN": "b"}, {"UP": " a", "DOWN": "b"},
        {"UP": "a", "DOWN": "b\nc"},
        {"Up": "a", "DOWN": "b"}, {"UP": 123, "DOWN": "b"},
    ]
    for value in invalid:
        with pytest.raises(DataUnavailable):
            canonical_up_down_tokens(value)


@pytest.mark.parametrize("change", [
    {"tokens": {"UP": "same", "DOWN": "same"}},
    {"tokens": {"UP": "up-token"}},
    {"tokens": {"UP": "up-token", "DOWN": "down-token", "OTHER": "other"}},
    {"tokens": {"UP": "up-token", "DOWN": 7}},
    {"condition": ""}, {"condition": " wrong "},
    {"slug": ""}, {"start": float("nan")},
    {"end": float("inf")}, {"start": 10**12, "end": 1},
])
def test_corrupt_market_rejected_before_any_paper_event(change, tmp_path):
    source = ScheduledSource(42)
    original = source.discover
    def changed(*args):
        market = deepcopy(original(*args))
        market.update(change)
        return market
    source.discover = changed
    config = small_cfg()
    store = Store(tmp_path, 500)
    before = store.read()
    with store.transaction() as (state, events):
        process_polymarket(config, state, events, source, source.now)
        assert not events
        assert state["cash"] == before["cash"]
        assert not state["positions"]
        assert not state["done"]
        assert state["diagnostics"]["POLYMARKET"]
    reopened = Store(tmp_path)
    reopened.audit()
    assert reopened.history() == []
    assert reopened.read()["cash"] == 500


def test_gamma_discovery_maps_reversed_outcomes_and_rejects_ambiguous(monkeypatch):
    from bcc.market import clock
    monkeypatch.setattr(clock, "ensure", lambda: None)
    feed = PolymarketData()
    start = 1_766_000_000 // 300 * 300
    now = float(start + 42)
    base = {
        "id": "123", "conditionId": "condition", "slug": f"btc-updown-5m-{start}",
        "question": "Bitcoin Up or Down - 5 minutes",
        "endDate": datetime.fromtimestamp(start + 300, timezone.utc).isoformat(),
        "closed": False, "acceptingOrders": True,
        "outcomes": json.dumps(["DOWN", "UP"]),
        "clobTokenIds": json.dumps(["down-token", "up-token"]),
        "orderMinSize": 1, "orderPriceMinTickSize": 0.01,
    }
    monkeypatch.setattr(feed, "get", lambda *args, **kwargs: [deepcopy(base)])
    market = feed.discover("BTC", 300, now)
    assert market["tokens"] == {"UP": "up-token", "DOWN": "down-token"}
    # Ambiguous Gamma records must not silently select a token or first market.
    for outcomes, tokens in [
        (["UP", "UP"], ["a", "b"]),
        (["UP", "DOWN"], ["a", "a"]),
        (["UP", "DOWN", "DRAW"], ["a", "b", "c"]),
        (["UP", "DOWN"], ["a"]),
        (["UP", "DOWN"], ["a", "b", "c"]),
    ]:
        row = dict(base, outcomes=json.dumps(outcomes), clobTokenIds=json.dumps(tokens))
        monkeypatch.setattr(feed, "get", lambda *args, value=row, **kwargs: [value])
        with pytest.raises(DataUnavailable, match="единственный активный"):
            feed.discover("BTC", 300, now)
    monkeypatch.setattr(feed, "get", lambda *args, **kwargs: [base, base])
    with pytest.raises(DataUnavailable, match="единственный активный"):
        feed.discover("BTC", 300, now)


@pytest.mark.parametrize("rows,expect", [
    ([{"asset_id": "token", "resolved": True, "final_price": "1"}], 1.0),
    ([{"asset_id": "token", "resolved": True, "final_price": "0"}], 0.0),
    ([{"asset_id": "token", "resolved": False, "final_price": "1"}], None),
    ([{"asset_id": "token", "resolved": True, "final_price": "nan"}], None),
    ([{"asset_id": "other", "resolved": True, "final_price": "1"}], None),
    ([{"asset_id": "token", "resolved": True, "final_price": "1"},
      {"asset_id": "token", "resolved": False, "final_price": "1"}], None),
])
def test_official_payout_requires_resolved_matching_token(monkeypatch, rows, expect):
    feed = PolymarketData()
    monkeypatch.setattr(feed, "get", lambda *args, **kwargs: {"data": rows})
    assert feed.payout("token") == expect


def test_conflicting_official_records_do_not_settle(monkeypatch):
    feed = PolymarketData()
    monkeypatch.setattr(feed, "get", lambda *args, **kwargs: {"data": [
        {"asset_id": "token", "resolved": True, "final_price": 1},
        {"asset_id": "token", "resolved": True, "final_price": 0},
    ]})
    with pytest.raises(DataUnavailable, match="Противоречивые"):
        feed.payout("token")


def test_unresolved_position_persists_until_official_result_after_restart(tmp_path):
    config = small_cfg()
    source = ScheduledSource(42)
    store = Store(tmp_path, 500)
    with store.transaction() as (state, events):
        process_polymarket(config, state, events, source, source.now)
    assert len(store.read()["positions"]) == 1
    with Store(tmp_path).transaction() as (state, events):
        process_polymarket(config, state, events, source, source.now + 50)
    assert len(Store(tmp_path).read()["positions"]) == 1
    source.resolved = True
    with Store(tmp_path).transaction() as (state, events):
        process_polymarket(config, state, events, source, source.now + 51)
    settled = Store(tmp_path)
    assert not settled.read()["positions"]
    assert len([e for e in settled.history() if e["action"] == "SETTLE"]) == 1
    with Store(tmp_path).transaction() as (state, events):
        process_polymarket(config, state, events, source, source.now + 52)
    settled.audit()
    assert len([e for e in Store(tmp_path).history() if e["action"] == "SETTLE"]) == 1
