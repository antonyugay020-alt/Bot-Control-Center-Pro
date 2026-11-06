"""A failed market refresh immediately invalidates collateral for existing positions.

Isolated synthetic PAPER ledgers only; never exchange accounts or user MY_BOTS.
"""
import time
from pathlib import Path

from bcc.market import DataUnavailable
from bcc.runtime import process_tick, run_loop
from bcc.scheduler import MarketTick
from bcc.storage import Store
from bcc.trading import available_margin, enter, plan
from test_core import attach, book, config


def opened_account(tmp_path):
    store = Store(tmp_path, 500)
    cfg = config()
    cfg["settings"]["orders"]["count"] = 1
    with store.transaction() as (state, events):
        attach(state)
        ok, reason = enter(state, events, "BTCUSDT:one", "BTCUSDT", "LONG",
                           cfg["settings"], 100, 25,
                           plan(cfg["settings"]["orders"], "LONG", 100), "one")
        assert ok, reason
        state["marks"]["BTCUSDT"] = {
            "bid": 99.99, "ask": 100.01, "at": time.time(),
        }
    assert available_margin(store.read()) > 0
    return cfg, store


class NeverTrades:
    def process(self, *args):
        pass


def test_missing_book_revokes_fresh_mark_before_other_paper_admission(tmp_path):
    cfg, store = opened_account(tmp_path)
    original_history = store.history()
    process_tick(cfg, store, NeverTrades(), MarketTick(
        "BTCUSDT", [], None, [], {}, quote_error="public book request failed"))
    current = store.read()
    assert "BTCUSDT" not in current["marks"]
    assert available_margin(current) == float("-inf")
    assert "public book request failed" in current["diagnostics"]["BTCUSDT"][0]["reason"]
    assert current["positions"]
    assert store.history() == original_history
    store.audit()

    # A *new* validated book restores the mark; the interlock is not permanent.
    process_tick(cfg, store, NeverTrades(), MarketTick(
        "BTCUSDT", [], book(100), [], {}))
    refreshed = store.read()
    assert available_margin(refreshed) > 0
    store.audit()


def test_scheduler_exception_revokes_fresh_mark(tmp_path, monkeypatch):
    cfg, store = opened_account(tmp_path)
    import bcc.runtime as runtime

    class FailedScheduler:
        def __init__(self, *args):
            self.seen = False
            self.closed = False

        def collect(self, *args):
            if self.seen:
                raise AssertionError("loop did not stop after the diagnostic")
            self.seen = True
            return [("BTCUSDT", None, DataUnavailable("orderbook unavailable"))]

        def close(self):
            self.closed = True

    scheduler = FailedScheduler()
    monkeypatch.setattr(runtime, "MarketScheduler", lambda *args, **kwargs: scheduler)

    def stopped():
        return bool(store.read()["diagnostics"].get("BTCUSDT"))

    run_loop(Path(tmp_path), cfg, store, object(), NeverTrades(),
             {"heartbeat": 0, "draining": False}, stopped,
             lambda *args: 150, lambda *args: None)
    current = store.read()
    assert scheduler.seen and scheduler.closed
    assert current["diagnostics"]["BTCUSDT"][0]["reason"] == "orderbook unavailable"
    assert "BTCUSDT" not in current["marks"]
    assert available_margin(current) == float("-inf")
    store.audit()
