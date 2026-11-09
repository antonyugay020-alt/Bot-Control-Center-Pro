"""Durable public mark-price refresh independent of executable orderbooks.

Only the authoritative account owner writes market evidence into PAPER state.
Invalid/missing data never authorizes entry or changes risk tiers.
"""
from copy import deepcopy

from .market_clock import quote_is_recent


def commit_portfolio_marks(store, marks):
    """Atomically refresh all currently exposed symbols from one public packet."""
    if not isinstance(marks, dict):
        raise ValueError("Invalid mark-price packet")
    with store.transaction(include_charts=False) as (state, events):
        active = {p["symbol"] for p in state["positions"].values()
                  if p.get("kind") != "POLYMARKET"}
        active.update(p["symbol"] for p in state.get("pending", {}).values()
                      if p.get("kind") != "POLYMARKET")
        # A partial response is never allowed to refresh a subset of the
        # portfolio and make a later per-position decision appear complete.
        if not active <= marks.keys():
            return 0
        # Recheck at commit time: a delayed future cannot revive a stale quote.
        if any(not quote_is_recent(marks[symbol], maximum=3) for symbol in active):
            return 0
        changed = 0
        for symbol in active:
            data = state.get("margin_data", {}).get(symbol)
            if isinstance(data, dict) and data.get("symbol") == symbol:
                data["quote"] = deepcopy(marks[symbol])
                changed += 1
        return changed
