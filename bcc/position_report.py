"""One conservative PAPER position valuation for every Qt monitoring view.

These are display-only estimates, never trade prices or official outcomes.
"""

import math

from .market_clock import quote_is_recent
from .trading import mark_pnl


def position_mark_pnl(state, position, now=None):
    # Display-only unrealized PnL must follow the verified executable
    # bid/ask arriving through the Bybit WS book. Margin calculations retain
    # their independent exchange mark-price and risk-tier requirements;
    # this display estimate must never authorize entries or collateral.
    quote = state.get("marks", {}).get(position.get("token", position.get("symbol")))
    maximum = 15 if position.get("kind") == "POLYMARKET" else 3
    if not quote or not quote_is_recent(quote, maximum=maximum, now=now):
        return None
    try:
        bid, ask = quote["bid"], quote["ask"]
        if (type(bid) not in (float, int) or type(ask) not in (float, int)
                or not math.isfinite(bid) or not math.isfinite(ask)
                or bid > ask):
            return None
        if position.get("kind") == "POLYMARKET":
            if not 0 <= bid <= ask <= 1:
                return None
            result = position["shares"] * bid - position["cost"]
        else:
            if bid <= 0 or ask <= 0:
                return None
            result = mark_pnl(
                position, bid if position["side"] == "LONG" else ask,
            )
        if type(result) not in (int, float) or not math.isfinite(result):
            return None
        return result
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None
