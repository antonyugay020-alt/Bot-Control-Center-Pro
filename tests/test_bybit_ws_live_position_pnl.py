"""Executable public Bybit WS bid/ask drives display PnL, not stale risk mark.

These are display-only paths. Financial admission, liquidation, margin tiers
and account equity continue to use their own independent verified evidence.
"""
import time
from pathlib import Path

import pytest

from bcc.position_report import position_mark_pnl
from bcc.margin import MODEL


def position(side="LONG"):
    return {
        "kind": "VELES", "symbol": "BTCUSDT", "side": side,
        "qty": 2.0, "average": 100.0,
    }


def test_active_bybit_long_displays_verified_executable_bid_with_missing_risk_mark():
    now = time.time()
    state = {
        "margin_model": MODEL,
        "margin_data": {},  # No authority to infer exchange mark price.
        "marks": {"BTCUSDT": {"bid": 103., "ask": 104., "at": now}},
    }
    assert position_mark_pnl(state, position(), now) == pytest.approx(6.)
    assert position_mark_pnl(state, position("SHORT"), now) == pytest.approx(-8.)


@pytest.mark.parametrize("age", [3.5, 4., 20., 100.])
def test_display_fails_closed_on_old_bybit_ws_books(age):
    now = time.time()
    state = {
        "margin_model": MODEL,
        "marks": {"BTCUSDT": {"bid": 103., "ask": 104., "at": now - age}},
    }
    assert position_mark_pnl(state, position(), now) is None


def test_old_bybit_mark_does_not_override_new_ws_quote():
    now = time.time()
    state = {
        "margin_model": MODEL,
        "margin_data": {"BTCUSDT": {
            "symbol": "BTCUSDT", "quote": {"bid": 70., "ask": 70., "at": now - 15},
        }},
        "marks": {"BTCUSDT": {"bid": 105., "ask": 106., "at": now}},
    }
    assert position_mark_pnl(state, position(), now) == pytest.approx(10.)


def test_qt_live_update_is_ledger_only_and_fast_without_heavy_charts():
    root = Path(__file__).resolve().parents[1]
    source = (root / "bcc" / "ui.py").read_text(encoding="utf-8")
    section = source.split("    def refresh_live_positions(self):", 1)[1].split(
        "    def refresh_chart(self,", 1
    )[0]
    assert "read_paper_state(self.selected())" in section
    assert "position_mark_pnl(state, state[\"positions\"][key], now)" in section
    assert "self.positions.item(row, 6)" in section
    assert "self._render_live_finance_notice(" in section
    assert "self.live_positions_timer.setInterval(500)" in source
    assert "self.live_positions_timer.timeout.connect(self.refresh_live_positions)" in source
    assert "source.orderbook" not in section
    assert "self.refresh_chart()" not in section
    assert "store.history()" not in section
