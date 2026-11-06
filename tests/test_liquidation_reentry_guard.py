"""Liquidation must finish the tick; no same-tick RSI re-admission."""
import pytest

from bcc.config import defaults
from bcc.runtime import process_tick
from bcc.scheduler import MarketTick
from bcc.storage import Store
from bcc.trading import enter, plan
from test_core import attach, book


@pytest.mark.parametrize("margin_mode", ["ISOLATED", "CROSS"])
def test_fully_executed_liquidation_prevents_new_entry_in_same_tick(tmp_path, margin_mode):
    cfg = defaults("VELES")
    cfg["symbols"] = ["BTCUSDT"]
    settings = cfg["settings"]
    settings.update(leverage=20, margin_mode=margin_mode, slippage_pct=0)
    settings["orders"]["count"] = 1
    store = Store(tmp_path, 500)
    with store.transaction() as (state, events):
        attach(state)
        ok, why = enter(state, events, "BTCUSDT:one", "BTCUSDT", "LONG", settings,
                        100, 50, plan(settings["orders"], "LONG", 100), "one")
        assert ok, why
        state.pop("_books")

    class SpyEngine:
        calls = 0
        def process(self, *_args):
            self.calls += 1

    engine = SpyEngine()
    process_tick(cfg, store, engine, MarketTick("BTCUSDT", [], book(50), [], {}))
    actions = [x["action"] for x in store.history()]
    assert "CLOSE" in actions
    assert not store.read()["positions"]
    assert engine.calls == 0, "No new signals are admissible on a liquidation tick"
    store.audit()
