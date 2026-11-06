"""Durable PAPER risk reduction; price discovery and accounting stay external.

One operation records an absolute remaining quantity. Lot rounding, IOC chunks
and process restarts cannot turn that target into another percentage reduction.
After reducing one public tier, fresh collateral is checked before a full exit.
"""

from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR
import uuid
import re
import math

from .margin import (
    MarginUnavailable,
    active_order_value,
    close_fee,
    effective_value,
    numeric,
    requirements,
    snapshot,
    tier_for,
)
from .market_clock import market_now
from .execution import ceil_quantity, floor_quantity

REASONS = {"PAPER_LIQUIDATION", "PAPER_CROSS_LIQUIDATION"}
BLOCKED = "Выполняется ликвидация PAPER; новые входы и DCA приостановлены"


def validate_operation(p):
    if "liquidation" not in p:
        return
    op = p["liquidation"]
    if (
        not isinstance(op, dict)
        or not isinstance(op.get("id"), str)
        or re.fullmatch(r"[0-9a-f]{32}", op["id"]) is None
        or op.get("reason") not in REASONS
        or op.get("stage") not in ("ASSESS", "REDUCE", "RECHECK", "FULL")
    ):
        raise MarginUnavailable("Повреждено состояние сокращения риска")
    for key in ("started", "initial_qty"):
        v = op.get(key)
        if type(v) not in (int, float) or not math.isfinite(v) or v <= 0:
            raise MarginUnavailable("Повреждено состояние сокращения риска: " + key)
    if op["initial_qty"] + max(1e-12, op["initial_qty"] * 1e-10) < p["qty"]:
        raise MarginUnavailable("Объём увеличен во время сокращения риска")
    if op["stage"] != "ASSESS":
        value = op.get("target_remaining_qty")
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 <= value <= op["initial_qty"]
        ):
            raise MarginUnavailable("Повреждён целевой остаток сокращения риска")


def is_liquidating(p):
    return bool(
        p.get("liquidation") or p.get("exit_pending", {}).get("reason") in REASONS
    )


def status(p):
    op = p.get("liquidation")
    if not op:
        return p.get("exit_pending", {}).get("reason", "") if is_liquidating(p) else ""
    stage = {
        "ASSESS": "ожидание данных",
        "REDUCE": "частичное сокращение",
        "RECHECK": "проверка обеспечения",
        "FULL": "полное закрытие",
    }.get(op["stage"], op["stage"])
    target = op.get("target_remaining_qty")
    return (
        "Ликвидация: "
        + stage
        + (f"; целевой остаток {target:g}" if target is not None else "")
    )


def _cancel_grid(state, events, key, p):
    from .trading import event

    if p.get("order_margin", 0) or p["next_order"] < len(p["orders"]):
        p["order_margin"] = 0
        p["next_order"] = len(p["orders"])
        p["active_until"] = len(p["orders"])
        event(
            events,
            p["symbol"],
            p["section"],
            "CANCEL_GRID",
            cash_delta=0,
            net_pnl=0,
            reason="LIQUIDATION_RISK",
            side=p["side"],
        )


def _cancel_cross_orders(state, events):
    from .trading import event

    for key, p in state["positions"].items():
        if p.get("kind") != "POLYMARKET" and p["settings"]["margin_mode"] == "CROSS":
            _cancel_grid(state, events, key, p)
    for key, p in list(state["pending"].items()):
        if p["settings"]["margin_mode"] == "CROSS":
            del state["pending"][key]
            event(
                events,
                p["symbol"],
                key,
                "CANCEL_GRID",
                cash_delta=0,
                net_pnl=0,
                reason="CROSS_LIQUIDATION_RISK",
                side=p["side"],
            )


def _cross_metrics(state):
    infos = {
        key: requirements(state, p)
        for key, p in state["positions"].items()
        if p.get("kind") != "POLYMARKET" and p["settings"]["margin_mode"] == "CROSS"
    }
    isolated = sum(
        p["margin"] + close_fee(p)
        for p in state["positions"].values()
        if p.get("kind") != "POLYMARKET" and p["settings"]["margin_mode"] == "ISOLATED"
    )
    collateral = state["cash"] - isolated + sum(x["unrealized"] for x in infos.values())
    need = sum(x["maintenance"] + x["fee_to_close"] for x in infos.values())
    order_values = {}
    for pending, items in [
        (False, state["positions"].values()),
        (True, state["pending"].values()),
    ]:
        for p in items:
            if p.get("kind") == "POLYMARKET" or p["settings"]["margin_mode"] != "CROSS":
                continue
            if not p.get("orders"):
                continue
            data, mark = snapshot(state, p["symbol"])
            value = active_order_value(p, mark, pending=pending)
            if value:
                order_values[p["symbol"]] = order_values.get(p["symbol"], 0) + value
    for symbol, value in order_values.items():
        data, mark = snapshot(state, symbol)
        effective = effective_value(state, symbol, mark)
        tier = tier_for(data["tiers"], max(effective.values()))
        need += value * tier["maintenance_rate"]
    return collateral, need, infos


def _healthy(state, key, p):
    if p["settings"]["margin_mode"] == "CROSS":
        collateral, need, _ = _cross_metrics(state)
        return collateral > need
    info = requirements(state, p)
    p["margin_evidence"] = info
    return p["margin"] + info["unrealized"] > info["maintenance"]


def _recover(state, events, key, p):
    from .trading import event

    evidence = requirements(state, p)
    account = None
    if p["settings"]["margin_mode"] == "CROSS":
        collateral, need, _ = _cross_metrics(state)
        account = dict(collateral=collateral, maintenance_required=need)
    p["margin_evidence"] = evidence
    op = p.pop("liquidation")
    if p.get("exit_pending", {}).get("reason") in REASONS:
        p.pop("exit_pending")
    op.update(stage="RECOVERED", finished=market_now(), final_margin=evidence)
    if account is not None:
        op["final_cross_account"] = account
    p["last_liquidation"] = deepcopy(op)
    event(
        events,
        p["symbol"],
        p["section"],
        "LIQUIDATION_RECOVERED",
        cash_delta=0,
        net_pnl=0,
        reason="MARGIN_RESTORED",
        liquidation=op,
    )


def _rules(state, p):
    book = state.get("_books", {}).get(p["symbol"], {})
    rules = book or p.get("execution_rules", {})
    step = numeric(
        rules.get("qty_step"), "шаг количества для ликвидации", minimum=1e-18
    )
    minimum = numeric(
        rules.get("min_qty"), "минимум количества для ликвидации", minimum=1e-18
    )
    return book, step, minimum


def _plan(state, p, op):
    data, mark = snapshot(state, p["symbol"])
    info = requirements(state, p)
    tiers = data["tiers"]
    index = next(i for i, tier in enumerate(tiers) if tier["id"] == info["tier_id"])
    p["margin_evidence"] = info
    op.update(trigger=deepcopy(info), from_tier=info["tier_id"])
    if index == 0:
        op.update(stage="FULL", target_remaining_qty=0.0)
        return
    _, step, minimum = _rules(state, p)
    total = sum(
        x["qty"]
        for x in state["positions"].values()
        if x.get("kind") != "POLYMARKET"
        and x["symbol"] == p["symbol"]
        and x["side"] == p["side"]
    )
    lower = tiers[index - 1]
    allowed = floor_quantity(
        Decimal(str(lower["limit"])) / Decimal(str(mark)), Decimal(str(step))
    )
    reduction = max(0, total - allowed)
    quantity = min(
        p["qty"],
        max(
            ceil_quantity(reduction, Decimal(str(step))),
            ceil_quantity(minimum, Decimal(str(step))),
        ),
    )
    remaining = max(0, float(Decimal(str(p["qty"])) - Decimal(str(quantity))))
    if remaining < minimum - step * 1e-8:
        remaining = 0.0
    op.update(
        stage="REDUCE" if remaining else "FULL",
        target_remaining_qty=remaining,
        to_tier=lower["id"],
        target_side_value=lower["limit"],
    )


def _execute(state, events, key, p):
    from .trading import exit_position, fill_price

    op = p["liquidation"]
    target = op["target_remaining_qty"]
    remaining = max(0, p["qty"] - target)
    tolerance = max(1e-12, p["qty"] * 1e-10)
    if remaining <= tolerance:
        op["stage"] = "RECHECK"
        return
    book = state.get("_books", {}).get(p["symbol"], {})
    # An absolute operation survives a missing book; the pending ratio is only
    # the next physical IOC request and is regenerated from the operation.
    quantity = remaining
    if book:
        _, step, minimum = _rules(state, p)
        maximum = book.get("max_qty")
        if maximum is not None:
            cap = float(
                (
                    Decimal(str(numeric(maximum, "максимум количества", minimum=step)))
                    / Decimal(str(step))
                ).to_integral_value(rounding=ROUND_FLOOR)
                * Decimal(str(step))
            )
            if cap < minimum:
                raise MarginUnavailable("Максимальный объём выхода меньше минимального")
            quantity = min(quantity, cap)
        quantity = min(p["qty"], ceil_quantity(quantity, Decimal(str(step))))
        if quantity < minimum and quantity < p["qty"] - tolerance:
            quantity = min(p["qty"], ceil_quantity(minimum, Decimal(str(step))))
    q = state["marks"].get(p["symbol"], {})
    bid, ask = q.get("bid", p["average"]), q.get("ask", p["average"])
    exit_position(
        state,
        events,
        key,
        fill_price(p["side"], "EXIT", bid, ask, p["settings"]),
        op["reason"],
        quantity / p["qty"],
        execution_policy="ioc",
    )
    if key not in state["positions"]:
        return
    tolerance = max(1e-12, p["qty"] * 1e-10)
    if p["qty"] <= target + tolerance:
        p.pop("exit_pending", None)
        op["stage"] = "RECHECK"
    if op["stage"] in ("REDUCE", "RECHECK") and _healthy(state, key, p):
        _recover(state, events, key, p)


def _resume(state, events, key, p):
    op = p["liquidation"]
    state["_entry_blocked"] = BLOCKED
    if op["stage"] in ("ASSESS", "REDUCE", "RECHECK"):
        if _healthy(state, key, p):
            _recover(state, events, key, p)
            return
        if op["stage"] == "ASSESS":
            _plan(state, p, op)
        elif op["stage"] == "RECHECK":
            op.update(stage="FULL", target_remaining_qty=0.0)
    _execute(state, events, key, p)


def _start(state, events, key, p, reason):
    _cancel_grid(state, events, key, p)
    p["liquidation"] = dict(
        id=uuid.uuid4().hex,
        reason=reason,
        stage="ASSESS",
        started=market_now(),
        initial_qty=p["qty"],
    )
    pending = p.get("exit_pending", {})
    if (
        pending
        and pending.get("reason") not in REASONS
        and pending.get("ratio", 0) >= 1 - 1e-10
    ):
        p["liquidation"].update(
            stage="FULL",
            target_remaining_qty=0.0,
            preserved_full_exit=pending["reason"],
            trigger=deepcopy(p.get("margin_evidence", {})),
        )
    _resume(state, events, key, p)


def process_liquidations(state, events):
    handled = set()
    for key, p in list(state["positions"].items()):
        if p.get("kind") == "POLYMARKET":
            continue
        old = p.get("exit_pending", {})
        if old.get("reason") in REASONS and not p.get("liquidation"):
            p["liquidation"] = dict(
                id=uuid.uuid4().hex,
                reason=old["reason"],
                stage="FULL",
                target_remaining_qty=max(0, p["qty"] * (1 - old.get("ratio", 1))),
                started=market_now(),
                initial_qty=p["qty"],
                restored_legacy_request=True,
            )
        if not p.get("liquidation"):
            continue
        handled.add(key)
        try:
            _resume(state, events, key, p)
            p.pop("liquidation_error", None)
        except (MarginUnavailable, KeyError, TypeError) as exc:
            p["liquidation_error"] = str(exc)
            state["_entry_blocked"] = BLOCKED
    for key, p in list(state["positions"].items()):
        if (
            key in handled
            or p.get("kind") == "POLYMARKET"
            or p["settings"]["margin_mode"] != "ISOLATED"
        ):
            continue
        try:
            p["margin_evidence"] = requirements(state, p)
            p.pop("margin_data_error", None)
            if not _healthy(state, key, p):
                _start(state, events, key, p, "PAPER_LIQUIDATION")
        except (MarginUnavailable, KeyError, TypeError) as exc:
            p["margin_data_error"] = str(exc)
    if any(
        p.get("liquidation", {}).get("reason") == "PAPER_CROSS_LIQUIDATION"
        for p in state["positions"].values()
    ):
        state["_entry_blocked"] = BLOCKED
        return
    try:
        collateral, need, infos = _cross_metrics(state)
        for key, info in infos.items():
            state["positions"][key]["margin_evidence"] = info
            state["positions"][key].pop("margin_data_error", None)
        if collateral > need or not infos and not state["pending"]:
            return
        state["_entry_blocked"] = BLOCKED
        _cancel_cross_orders(state, events)
        collateral, need, infos = _cross_metrics(state)
        candidates = [key for key in infos if key not in handled]
        if collateral <= need and candidates:
            key = max(candidates, key=lambda k: (infos[k]["maintenance"], k))
            _start(
                state, events, key, state["positions"][key], "PAPER_CROSS_LIQUIDATION"
            )
    except (MarginUnavailable, KeyError, TypeError) as exc:
        state["_entry_blocked"] = "Маржа Cross: " + str(exc)
        for p in state["positions"].values():
            if (
                p.get("kind") != "POLYMARKET"
                and p["settings"]["margin_mode"] == "CROSS"
            ):
                p["margin_data_error"] = str(exc)
