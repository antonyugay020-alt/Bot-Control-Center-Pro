"""USDT-linear margin requirements from public Bybit risk tiers and mark price.

Sources: Bybit v5 market/risk-limit, market/tickers; help-center articles
Maintenance-Margin-USDT-Contract and Risk-Limit-Perpetual-and-Expiry-Contracts.
Rates are fractions, not display percentages. No guessed tier is substituted
when public data is missing. Existing accounting and execution stay in trading.
"""

import math
from copy import deepcopy

from .market_clock import market_now, quote_is_recent

MODEL = "BYBIT_USDT_MARK_TIERS_V1"


class MarginUnavailable(ValueError):
    pass


def numeric(value, label, *, minimum=0, maximum=1e18):
    if isinstance(value, bool):
        raise MarginUnavailable("Неверное значение маржи: " + label)
    try:
        value = float(value)
    except (ValueError, TypeError) as exc:
        raise MarginUnavailable("Неверное значение маржи: " + label) from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise MarginUnavailable("Неверное значение маржи: " + label)
    return value


def parse_tiers(rows, symbol):
    tiers = []
    for row in rows:
        if row.get("symbol") != symbol:
            raise MarginUnavailable("Ступени риска другой монеты")
        identifier = numeric(row.get("id"), "ID", minimum=1)
        if not identifier.is_integer():
            raise MarginUnavailable("ID ступени риска должен быть целым")
        tiers.append(
            dict(
                id=int(identifier),
                limit=numeric(
                    row.get("riskLimitValue"), "riskLimitValue", minimum=1e-12
                ),
                maintenance_rate=numeric(
                    row.get("maintenanceMargin"),
                    "maintenanceMargin",
                    minimum=0,
                    maximum=1,
                ),
                initial_rate=numeric(
                    row.get("initialMargin"), "initialMargin", minimum=1e-12, maximum=1
                ),
                max_leverage=numeric(
                    row.get("maxLeverage"), "maxLeverage", minimum=1, maximum=1000
                ),
                deduction=(
                    None
                    if row.get("mmDeduction") in (None, "")
                    else numeric(row["mmDeduction"], "mmDeduction")
                ),
            )
        )
    if not tiers or len(tiers) > 1000:
        raise MarginUnavailable("Нет полной таблицы ступеней риска")
    tiers.sort(key=lambda item: item["limit"])
    previous_limit = previous_rate = previous_deduction = 0.0
    ids = set()
    for tier in tiers:
        if (
            tier["id"] in ids
            or tier["limit"] <= previous_limit
            or tier["maintenance_rate"] < previous_rate
            or tier["maintenance_rate"] >= tier["initial_rate"]
            or not math.isclose(
                tier["initial_rate"] * tier["max_leverage"], 1, rel_tol=0.05
            )
        ):
            raise MarginUnavailable("Противоречивая таблица ступеней риска")
        expected = previous_deduction + previous_limit * (
            tier["maintenance_rate"] - previous_rate
        )
        if tier["deduction"] is None:
            tier["deduction"] = expected
        elif not math.isclose(tier["deduction"], expected, rel_tol=1e-6, abs_tol=1e-6):
            raise MarginUnavailable(
                "Вычет maintenance margin не согласуется со ступенями"
            )
        ids.add(tier["id"])
        previous_limit, previous_rate, previous_deduction = (
            tier["limit"],
            tier["maintenance_rate"],
            tier["deduction"],
        )
    return tiers


def tier_for(tiers, value):
    value = numeric(value, "номинал")
    for tier in tiers:
        if value <= tier["limit"]:
            return tier
    raise MarginUnavailable("Номинал превышает максимальную публичную ступень риска")


def snapshot(state, symbol):
    data = state.get("margin_data", {}).get(symbol)
    if not isinstance(data, dict) or data.get("symbol") != symbol:
        raise MarginUnavailable("Нет публичных параметров маржи " + symbol)
    if not quote_is_recent(data.get("quote"), maximum=3):
        raise MarginUnavailable("Устарел mark-price " + symbol)
    age = market_now() - numeric(data.get("risk_at"), "метка ступеней риска")
    if not -0.5 <= age <= 300:
        raise MarginUnavailable("Устарели публичные ступени риска " + symbol)
    tiers = data.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        raise MarginUnavailable("Нет публичных ступеней риска " + symbol)
    mark = numeric(data["quote"].get("bid"), "mark-price", minimum=1e-12)
    return data, mark


def close_fee(p):
    settings = p["settings"]
    sign = -1 if p["side"] == "LONG" else 1
    return (
        p["qty"]
        * p["average"]
        * (1 + sign / settings["leverage"])
        * settings["fee_rate"]
    )


def active_order_value(p, mark, *, pending=False):
    orders = p.get("orders", [])
    if p["settings"]["orders"]["mode"] == "signal" and not pending:
        return 0.0
    first = 0 if pending else p.get("next_order", len(orders))
    last = min(
        len(orders),
        (
            1 + p["settings"]["orders"]["partial_count"]
            if pending
            else p.get("active_until", len(orders))
        ),
    )
    if p["settings"]["orders"]["mode"] == "signal":
        last = min(last, 1)
    budget = p["margin"] * p["settings"]["leverage"] if pending else p.get("budget", 0)
    # The existing engine sizes each planned order by nominal allocation and
    # limit price. Convert to its intended quantity before valuing at mark.
    return sum(
        budget * order["weight"] / order["price"] * mark for order in orders[first:last]
    )


def effective_value(state, symbol, mark, *, include_orders=True):
    sides = {"LONG": 0.0, "SHORT": 0.0}
    for p in state["positions"].values():
        if p["symbol"] == symbol and p.get("kind") != "POLYMARKET":
            sides[p["side"]] += p["qty"] * mark
            if include_orders:
                sides[p["side"]] += active_order_value(p, mark)
    if include_orders:
        for p in state["pending"].values():
            if p["symbol"] == symbol:
                sides[p["side"]] += active_order_value(p, mark, pending=True)
    return sides


def entry_reason(
    state,
    symbol,
    side,
    settings,
    margin=0,
    orders=None,
    *,
    reserved=False,
    first_fill=None,
    extra_value=0,
):
    if state.get("margin_model") != MODEL:
        return ""
    try:
        other_error = margin_status(state)
        if other_error:
            raise MarginUnavailable(other_error)
        data, mark = snapshot(state, symbol)
        sides = effective_value(state, symbol, mark)
        if not reserved:
            plans = (orders or [])[
                : min(len(orders or []), 1 + settings["orders"]["partial_count"])
            ]
            if settings["orders"]["mode"] == "signal":
                plans = plans[:1]
            if first_fill is not None and plans:
                sides[side] += first_fill.quantity * mark
                plans = plans[1:]
            sides[side] += sum(
                margin * settings["leverage"] * row["weight"] / row["price"] * mark
                for row in plans
            )
        sides[side] += extra_value
        tier = tier_for(data["tiers"], max(sides.values()))
        if settings["leverage"] > tier["max_leverage"] + 1e-9:
            return f"Плечо {settings['leverage']:g} превышает максимум {tier['max_leverage']:g} для ступени {tier['id']}"
        # A lower-leverage existing section of the same account cannot make a
        # new order valid for a sibling that already exceeds the selected tier.
        for p in state["positions"].values():
            if (
                p["symbol"] == symbol
                and p["settings"]["leverage"] > tier["max_leverage"] + 1e-9
            ):
                return f"Новая экспозиция превышает ступень риска открытой секции {p['section']}"
    except (MarginUnavailable, KeyError, TypeError) as exc:
        return "Маржа Bybit: " + str(exc)
    return ""


def requirements(state, p):
    data, mark = snapshot(state, p["symbol"])
    sides = effective_value(state, p["symbol"], mark, include_orders=False)
    tier = tier_for(data["tiers"], sides[p["side"]])
    same_side_value = sides[p["side"]]
    own_value = p["qty"] * mark
    # An exchange side is split into independent accounting sections in the
    # constructor. Allocate the tier deduction once, not once per section.
    deduction = (
        tier["deduction"] * own_value / same_side_value if same_side_value else 0
    )
    mm = max(0, own_value * tier["maintenance_rate"] - deduction)
    return dict(
        mark_price=mark,
        mark_at=data["quote"]["at"],
        tier_id=tier["id"],
        maintenance_rate=tier["maintenance_rate"],
        deduction=deduction,
        maintenance=mm,
        fee_to_close=close_fee(p),
        initial=(
            own_value / p["settings"]["leverage"]
            if p["settings"]["margin_mode"] == "CROSS"
            else p["margin"]
        ),
        unrealized=(mark - p["average"])
        * p["qty"]
        * (1 if p["side"] == "LONG" else -1),
    )


def reserved_position(state, p):
    initial = p["margin"]
    if state.get("margin_model") == MODEL:
        if p["settings"]["margin_mode"] == "CROSS":
            try:
                _, mark = snapshot(state, p["symbol"])
                initial = p["qty"] * mark / p["settings"]["leverage"]
            except MarginUnavailable:
                # Keep a conservative display when data expires. Allocation is
                # separately blocked; missing data never means zero reserve.
                initial = max(
                    initial, p.get("margin_evidence", {}).get("initial", initial)
                )
        return (
            initial
            + close_fee(p)
            + future_reserve(p.get("order_margin", 0), p["settings"])
        )
    return initial + p.get("order_margin", 0)


def future_reserve(margin, settings):
    # Full future principal plus entry fees and a conservative closing fee.
    return margin * (1 + (2 * settings["leverage"] + 1) * settings["fee_rate"])


def pending_reserve(state, p):
    return (
        future_reserve(p["margin"], p["settings"])
        if state.get("margin_model") == MODEL
        else p["margin"]
    )


def entry_reserve(state, side, settings, margin, orders, fill, symbol):
    _, mark = snapshot(state, symbol)
    p = dict(qty=fill.quantity, average=fill.price, side=side, settings=settings)
    initial = (
        fill.quantity * mark / settings["leverage"]
        if settings["margin_mode"] == "CROSS"
        else fill.notional / settings["leverage"]
    )
    remaining = margin * sum(row["weight"] for row in orders[1:])
    upnl = (mark - fill.price) * fill.quantity * (1 if side == "LONG" else -1)
    return (
        initial
        + future_reserve(remaining, settings)
        + close_fee(p)
        + fill.fee
        + max(0, -upnl)
    )


def project_dca(p, fill):
    next_p = deepcopy(p)
    qty = p["qty"] + fill.quantity
    next_p.update(
        qty=qty,
        average=(p["average"] * p["qty"] + fill.notional) / qty,
        margin=p["margin"] + fill.notional / p["settings"]["leverage"],
        order_margin=p["budget"]
        / p["settings"]["leverage"]
        * sum(row["weight"] for row in p["orders"][p["next_order"] + 1 :]),
    )
    next_p["next_order"] = p["next_order"] + 1
    next_p["active_until"] = min(
        len(p["orders"]),
        next_p["next_order"] + p["settings"]["orders"]["partial_count"],
    )
    return next_p


def dca_extra_value(state, p, fill):
    _, mark = snapshot(state, p["symbol"])
    return (
        fill.quantity * mark
        + active_order_value(project_dca(p, fill), mark)
        - active_order_value(p, mark)
    )


def dca_free_required(state, p, fill):
    _, mark = snapshot(state, p["symbol"])
    next_p = project_dca(p, fill)
    upnl_delta = (
        (mark - fill.price) * fill.quantity * (1 if p["side"] == "LONG" else -1)
    )
    return max(
        0,
        fill.fee
        - upnl_delta
        + reserved_position(state, next_p)
        - reserved_position(state, p),
    )


def margin_status(state):
    if state.get("margin_model") != MODEL:
        return ""
    errors = []
    for symbol in sorted(
        {
            p["symbol"]
            for p in state["positions"].values()
            if p.get("kind") != "POLYMARKET"
        }
    ):
        try:
            snapshot(state, symbol)
        except MarginUnavailable as exc:
            errors.append(str(exc))
    return "; ".join(errors)


def attach_snapshot(state, symbol, book):
    if book.get("margin_required"):
        state["margin_model"] = MODEL
    if book.get("margin_data"):
        state.setdefault("margin_data", {})[symbol] = deepcopy(book["margin_data"])
    for sym, data in state.get("margin_data", {}).items():
        quote = book.get("margin_marks", {}).get(sym)
        if quote:
            data["quote"] = deepcopy(quote)
    retained = (
        {symbol}
        | {p["symbol"] for p in state["positions"].values()}
        | {p["symbol"] for p in state["pending"].values()}
    )
    if "margin_data" in state:
        state["margin_data"] = {
            s: data for s, data in state["margin_data"].items() if s in retained
        }
