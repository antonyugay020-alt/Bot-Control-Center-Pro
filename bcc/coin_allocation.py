"""One authoritative PAPER position budget for section and per-coin modes.

The historic default is independent percentage allocation per section. The
new opt-in coin modes reserve a *single* planned principal-margin budget
across all open VELES ranges of the same symbol, including pending grids.
This is per PAPER bot/account, not a substitute for cross-bot shared risk,
available-margin checks, liquidation controls or real exchange limits.

All functions are pure and read committed/in-transaction state supplied by
the caller. No network, SQLite access, locks or change to existing positions.
"""
from __future__ import annotations

import math

SECTION_PCT = "section_pct"
COIN_PCT = "coin_pct"
COIN_FIXED = "coin_fixed"
ALLOCATION_MODES = (SECTION_PCT, COIN_PCT, COIN_FIXED)
COIN_MODES = frozenset((COIN_PCT, COIN_FIXED))


class AllocationUnavailable(ValueError):
    """Unreadable exposure must deny new PAPER entries, not free a budget."""


def _finite(value, label, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise AllocationUnavailable("Некорректная " + label)
    if positive and value <= 0 or not positive and value < 0:
        raise AllocationUnavailable("Некорректная " + label)
    return float(value)


def allocation_mode(settings):
    mode = settings.get("allocation_mode", SECTION_PCT)
    if mode not in ALLOCATION_MODES:
        raise AllocationUnavailable("Неизвестный режим распределения капитала")
    return mode


def planned_margin(base_capital, settings):
    """Target principal margin for a section or the entire symbol.

    'Fixed' always means principal margin in USDT, not notional with
    leverage; fees, buffers and cash availability are separate risk guards.
    """
    mode = allocation_mode(settings)
    if mode == COIN_FIXED:
        return _finite(
            settings.get("allocation_fixed_usdt"), "фиксированная маржа USDT",
            positive=True,
        )
    base = _finite(base_capital, "база капитала")
    percent = _finite(settings.get("allocation_pct"), "доля капитала", positive=True)
    if percent > 100:
        raise AllocationUnavailable("Доля капитала превышает 100%")
    target = base * percent / 100.0
    if not math.isfinite(target):
        raise AllocationUnavailable("Переполнение размера позиции")
    return target


def allocation_signature(settings):
    """Active coin budget identity; dormant fields do not cause conflicts."""
    mode = allocation_mode(settings)
    if mode == SECTION_PCT:
        return None
    if mode == COIN_FIXED:
        return mode, _finite(settings.get("allocation_fixed_usdt"),
                             "фиксированная маржа", positive=True)
    return mode, _finite(settings.get("allocation_pct"),
                         "доля капитала", positive=True)


def _position_planned_margin(position):
    if not isinstance(position, dict):
        raise AllocationUnavailable("Повреждена сохранённая позиция")
    direct = _finite(position.get("margin"), "маржа позиции")
    future = _finite(position.get("order_margin", 0), "резерв ордеров")
    principal = direct + future
    if not math.isfinite(principal):
        raise AllocationUnavailable("Переполнение резерва ордеров")
    if "budget" in position:
        settings = position.get("settings")
        if not isinstance(settings, dict):
            raise AllocationUnavailable("Нет настроек сохранённой позиции")
        leverage = _finite(settings.get("leverage"), "плечо", positive=True)
        planned = _finite(position["budget"], "номинал плана") / leverage
        # A complete planned grid can be larger than the currently active
        # limit orders. We do not free its unused principal to another range.
        principal = max(principal, planned)
    return principal


def occupied_coin_margin(state, symbol):
    """Conservative principal budget used by open and pending range plans."""
    if not isinstance(symbol, str) or not symbol:
        raise AllocationUnavailable("Не выбрана монета")
    if not isinstance(state, dict):
        raise AllocationUnavailable("Недоступен PAPER-счёт")
    positions, pending = state.get("positions"), state.get("pending")
    if not isinstance(positions, dict) or not isinstance(pending, dict):
        raise AllocationUnavailable("Недоступна книга открытых позиций")
    amounts = []
    for p in positions.values():
        if not isinstance(p, dict):
            raise AllocationUnavailable("Повреждена открытая позиция")
        if p.get("symbol") == symbol:
            if p.get("kind") == "POLYMARKET":
                raise AllocationUnavailable("Неизвестная позиция в VELES-счёте")
            amounts.append(_position_planned_margin(p))
    for p in pending.values():
        if not isinstance(p, dict):
            raise AllocationUnavailable("Повреждена ожидающая сетка")
        if p.get("symbol") == symbol:
            amounts.append(_finite(p.get("margin"), "маржа ожидающей сетки",
                                   positive=True))
    occupied = math.fsum(amounts)
    if not math.isfinite(occupied):
        raise AllocationUnavailable("Переполнение распределённой маржи")
    return occupied


def available_margin_for_coin(state, symbol, budget, settings):
    """Choose remaining per-symbol capacity; legacy mode is unchanged.

    Used inside the existing atomic Store transaction in Veles.process,
    so two eligible ranges on the same candle share the same state view.
    No new second ledger or cross-process balance snapshot is introduced.
    """
    target = _finite(budget, "выделенная маржа")
    if allocation_mode(settings) == SECTION_PCT:
        return target
    occupied = occupied_coin_margin(state, symbol)
    # Avoid a tiny positive floating-point residue being treated as a real
    # order and raising execution fees on a virtually exhausted budget.
    if occupied >= target - max(1e-9, target * 1e-12):
        return 0.0
    return max(0.0, target - occupied)
