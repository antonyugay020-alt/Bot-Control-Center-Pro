"""An outstanding close cannot be replaced by margin-engine liquidation.

The fill/retry path owns the original close and its user-selected limit/reason.
A margin breach may liquidate *other* positions, but never rewrite that
already-committed close intent.
"""
from copy import deepcopy

import pytest

from bcc.config import defaults
from bcc.storage import initial_state
from bcc.trading import liquidations


@pytest.mark.parametrize("margin_mode", ["ISOLATED", "CROSS"])
def test_liquidation_does_not_replace_existing_pending_exit(monkeypatch, margin_mode):
    import bcc.trading as trading

    monkeypatch.setattr(trading, "quote_is_recent", lambda q: q is not None)
    state = initial_state(500)
    intent = {"reason": "MANUAL_PAPER_CLOSE", "ratio": 0.4,
              "limit": 75.0, "target_index": None}
    position = {
        "symbol": "BTCUSDT", "side": "LONG", "qty": 10.0,
        "average": 100.0, "margin": 50.0,
        "settings": {"margin_mode": margin_mode, "fee_rate": 0.001,
                     "slippage_pct": 0.0},
        "exit_pending": deepcopy(intent),
    }
    state["positions"]["BTCUSDT:test"] = position
    state["marks"]["BTCUSDT"] = {"bid": 5.0, "ask": 5.1}
    # Under CROSS the collateral calculation must be below maintenance.
    state["cash"] = 5.0
    liquidations(state, [], maintenance=0.005)
    assert state["positions"]["BTCUSDT:test"]["exit_pending"] == intent


def test_cross_liquidation_keeps_pending_manual_but_closes_other_exposure(monkeypatch):
    import bcc.trading as trading

    monkeypatch.setattr(trading, "quote_is_recent", lambda q: q is not None)
    state = initial_state(100)
    state["cash"] = 10.0
    settings = {"margin_mode": "CROSS", "fee_rate": 0.001,
                "slippage_pct": 0.0}
    for symbol in ("BTCUSDT", "ETHUSDT"):
        state["positions"][symbol] = {
            "symbol": symbol, "side": "LONG", "qty": 1.0, "average": 100.0,
            "margin": 10.0, "settings": deepcopy(settings),
        }
        state["marks"][symbol] = {"bid": 1.0, "ask": 1.1}
    saved = {"reason": "MANUAL_PAPER_CLOSE", "ratio": 1.0,
             "limit": None, "target_index": None}
    state["positions"]["BTCUSDT"]["exit_pending"] = deepcopy(saved)
    liquidations(state, [], maintenance=0.005)
    assert state["positions"]["BTCUSDT"]["exit_pending"] == saved
    assert state["positions"]["ETHUSDT"]["exit_pending"]["reason"] == "PAPER_CROSS_LIQUIDATION"


def test_pending_exit_priority_survives_sqlite_restart_and_audit(tmp_path, monkeypatch):
    """The account remains recoverable without inventing a liquidation fill."""
    import bcc.trading as trading
    from bcc.storage import Store
    from bcc.trading import enter, plan
    from test_core import attach

    monkeypatch.setattr(trading, "quote_is_recent", lambda q: q is not None)
    store = Store(tmp_path, 500)
    settings = defaults("VELES")["settings"]
    settings.update(margin_mode="ISOLATED", leverage=2)
    settings["orders"]["count"] = 1
    saved = {"reason": "MANUAL_PAPER_CLOSE", "ratio": 1.0,
             "limit": None, "target_index": None}
    with store.transaction() as (state, events):
        attach(state)
        ok, why = enter(state, events, "BTCUSDT:main", "BTCUSDT", "LONG",
                        settings, 100, 50, plan(settings["orders"], "LONG", 100),
                        "main")
        assert ok, why
        state["positions"]["BTCUSDT:main"]["exit_pending"] = deepcopy(saved)
        state["marks"]["BTCUSDT"] = {"bid": 1.0, "ask": 1.1}
        state.pop("_books")
        liquidations(state, events)
    restarted = Store(tmp_path)
    assert restarted.read()["positions"]["BTCUSDT:main"]["exit_pending"] == saved
    assert [event["action"] for event in restarted.history()] == ["OPEN"]
    restarted.audit()


def test_isolated_liquidation_without_pending_still_queues_close(monkeypatch):
    import bcc.trading as trading

    monkeypatch.setattr(trading, "quote_is_recent", lambda q: q is not None)
    state = initial_state(100)
    state["positions"]["BTCUSDT:one"] = {
        "symbol": "BTCUSDT", "side": "LONG", "qty": 10,
        "average": 100, "margin": 10,
        "settings": {"margin_mode": "ISOLATED", "fee_rate": 0.001,
                     "slippage_pct": 0},
    }
    state["marks"]["BTCUSDT"] = {"bid": 1.0, "ask": 1.1}
    liquidations(state, [])
    assert state["positions"]["BTCUSDT:one"]["exit_pending"]["reason"] == "PAPER_LIQUIDATION"
