"""Optional VELES capital ceiling for each coin across ALL its sections.

This is a nominal capital-allocation budget, not a substitute for the
portfolio risk manager, exchange risk tiers, current mark-price margin or
independent Bybit balance checks. It accounts for already open and PENDING
DCA grids so that adding a section cannot silently re-use the same coin
allocation. Existing configs without capital_limit remain unchanged.

All checks operate only on the current bot's PAPER state and carry no
process, network or filesystem side effects.
"""
from __future__ import annotations

import math

from .config import number


def validate_capital_limit(rule):
    if not isinstance(rule, dict) or set(rule) != {"mode", "value"}:
        raise ValueError("Лимит капитала монеты: ожидаются режим и значение")
    mode = rule["mode"]
    if mode not in ("percent", "fixed") or type(mode) is not str:
        raise ValueError("Лимит монеты: выберите процент депозита или USDT")
    number(rule["value"], 0.001 if mode == "percent" else 0.01,
           100 if mode == "percent" else 1e12,
           "Лимит капитала монеты")
    return rule


def limit_amount(cfg, symbol):
    """No rule => no cap, including when loading pre-existing VELES bots."""
    over = cfg.get("coin_overrides", {}).get(symbol, {})
    rule = over.get("capital_limit")
    if rule is None:
        return None
    validate_capital_limit(rule)
    if cfg["strategy"] != "VELES":
        raise ValueError("Лимит капитала монеты разрешён только для VELES")
    amount = (cfg["capital"] * rule["value"] / 100
              if rule["mode"] == "percent" else rule["value"])
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("Не удалось рассчитать лимит капитала монеты")
    return amount


def _nonnegative(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Недостоверное обеспечение монеты: " + label)
    return value


def committed(state, symbol):
    """Nominal assigned deposit: filled + remaining DCA + pending grids.

    Existing positions keep their original reservations. Market-dependent
    risks and actual free cash are validated independently by margin.py.
    """
    total = 0.0
    for p in state["positions"].values():
        if p.get("symbol") != symbol:
            continue
        if p.get("kind") == "POLYMARKET":
            raise ValueError("Лимит VELES не применяется к Polymarket")
        total += _nonnegative(p.get("margin"), "открытая позиция")
        total += _nonnegative(p.get("order_margin", 0), "оставшаяся DCA-сетка")
    for p in state["pending"].values():
        if p.get("symbol") == symbol:
            total += _nonnegative(p.get("margin"), "ожидающая сетка")
    if not math.isfinite(total):
        raise ValueError("Переполнение капитала монеты")
    return total


def admission_reason(state, symbol, proposed_margin, cap):
    """Explain admission denial without mutating any position or ledger."""
    if cap is None:
        return ""
    try:
        amount = _nonnegative(proposed_margin, "новая секция")
        budget = _nonnegative(cap, "предел монеты")
        occupied = committed(state, symbol)
    except (ValueError, TypeError, KeyError) as exc:
        return "Лимит капитала монеты: " + str(exc)
    # Monetary comparison tolerates only floating-point accumulation error,
    # not a broad 1% overrun that would allow extra positions.
    if occupied + amount > budget + max(1e-8, budget * 1e-12):
        return (f"Лимит капитала {symbol}: уже {occupied:g} USDT, "
                f"новая секция {amount:g} USDT, предел {budget:g} USDT")
    return ""
