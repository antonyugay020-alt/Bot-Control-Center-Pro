"""Coin-level VELES margin budgeting, distinct from global risk and leverage.

The legacy model uses a percentage per *section*. New coin-level budgets are
shared by all ranges, active DCA grids and pending entries of the same coin.
They limit committed *margin*, not notional volume: leverage acts afterwards.
No network calls, worker processes, or side effects occur here.
"""
import math

from .config import effective


COIN_MODES = frozenset({"coin_pct", "coin_fixed"})


def coin_policy(cfg, symbol):
    """Get one canonical budget policy, ignoring section-level settings.

    An individual range must never redefine the coin cap and thereby spend
    the same budget more than once. The config validator bans such overrides.
    """
    return effective(cfg, symbol)


def nominal_budget(cfg, settings, state=None):
    """Maximum margin in USDT before existing reservations are deducted."""
    mode = settings.get("allocation_mode", "section_pct")
    if mode == "coin_fixed":
        return settings["allocation_usdt"]
    base = cfg["capital"]
    if cfg["strategy"] == "VELES" and cfg.get("reinvest") and state is not None:
        base += state.get("strategy", {}).get("veles_reinvested", 0)
    return base * settings["allocation_pct"] / 100


def committed_coin_margin(state, symbol):
    """Count existing positions' full DCA budget plus pending reservations."""
    total = 0.0
    for p in state.get("positions", {}).values():
        if p.get("symbol") == symbol:
            # PAPER's actual fill may be rounded down to the exchange's qty
            # step. The unfilled dust is *still* part of the range's original
            # coin reservation until that position is fully closed. DCA and
            # partial exits must not trick another range into double-spending.
            spent = p["margin"] + p.get("order_margin", 0)
            original = p.get("budget")
            if original is not None:
                leverage = p["settings"]["leverage"]
                spent = max(spent, original / leverage)
            total += spent
    for p in state.get("pending", {}).values():
        if p.get("symbol") == symbol:
            total += p["margin"]
    if not math.isfinite(total) or total < 0:
        raise ValueError("Неверные резервы капитала монеты")
    return total


def remaining_coin_budget(cfg, state, symbol):
    """Return unreserved coin margin. No cross-coin pool or silent borrowing."""
    policy = coin_policy(cfg, symbol)
    if policy.get("allocation_mode", "section_pct") not in COIN_MODES:
        raise ValueError("Для секционного режима нет единого лимита монеты")
    limit = nominal_budget(cfg, policy, state)
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("Неверный бюджет монеты")
    used = committed_coin_margin(state, symbol)
    remainder = max(0.0, limit - used)
    # Actual PAPER fills are quantized to exchange quantity steps. The tiny
    # unspent dust must not launch a second range with an unusable budget.
    return 0.0 if remainder < 0.01 else remainder
