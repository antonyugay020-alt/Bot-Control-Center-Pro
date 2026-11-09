"""PAPER positions, capital reservations, fee accounting and order planning."""

import math
import uuid
from copy import deepcopy
from decimal import Decimal

from .control import deal_closed
from .execution import ExecutionRejected, PaperExecutor, floor_quantity
from .market_clock import market_now, quote_is_recent
from .margin import (
    MODEL,
    MarginUnavailable,
    close_fee,
    entry_reason,
    requirements,
    reserved_position,
    snapshot,
    entry_reserve,
    dca_free_required,
    dca_extra_value,
    pending_reserve,
)
from .shared_risk import allocation_reason
from .liquidation import BLOCKED, is_liquidating
from .position_journal import cycle_reference, receipt, terms_digest


def plan(orders, side, anchor):
    sign = -1 if side == "LONG" else 1
    if orders["mode"] == "custom":
        return [
            {
                "price": anchor * (1 + sign * r["offset_pct"] / 100),
                "weight": r["volume_pct"] / 100,
                "anchor": anchor,
            }
            for r in orders["custom"]
        ]
    n = orders["count"]
    ratio = 1 + orders["martingale_pct"] / 100
    weights = [ratio**i for i in range(n)]
    total = sum(weights)
    # A logarithmic curve >1 concentrates price levels near the first order.
    offsets = [
        orders["first_offset_pct"]
        + orders["coverage_pct"]
        * ((i / (n - 1)) ** orders["logarithmic"] if n > 1 else 0)
        for i in range(n)
    ]
    return [
        {
            "price": anchor * (1 + sign * offset / 100),
            "weight": w / total,
            "anchor": anchor,
        }
        for offset, w in zip(offsets, weights)
    ]


def reserved(state):
    return sum(
        reserved_position(state, p) if p.get("kind") != "POLYMARKET" else 0
        for p in state["positions"].values()
    ) + sum(pending_reserve(state, p) for p in state["pending"].values())


def mark_pnl(p, price):
    return (price - p["average"]) * p["qty"] * (1 if p["side"] == "LONG" else -1)


def position_unrealized(state, p, now=None):
    """One valuation for both Qt monitors; no stale or future quote display."""
    if p.get("kind") != "POLYMARKET" and state.get("margin_model") == MODEL:
        try:
            _, mark = snapshot(state, p["symbol"])
            return mark_pnl(p, mark)
        except MarginUnavailable:
            return None
    quote = state["marks"].get(p.get("token", p["symbol"]))
    if not quote_is_recent(quote, now=now):
        return None
    if p.get("kind") == "POLYMARKET":
        return p["shares"] * quote["bid"] - p["cost"]
    return mark_pnl(p, quote["bid"] if p["side"] == "LONG" else quote["ask"])


def position_initial_margin(state, p):
    if p.get("kind") == "POLYMARKET" or state.get("margin_model") != MODEL:
        return p["margin"]
    try:
        return requirements(state, p)["initial"]
    except MarginUnavailable:
        return None


def percent_stop_price(p):
    """Veles: coverage 15% + stop 5% means 20% from the grid anchor."""
    sign = 1 if p["side"] == "LONG" else -1
    orders = p["settings"]["orders"]
    offset = (
        orders["custom"][0]["offset_pct"]
        if orders["mode"] == "custom"
        else orders["first_offset_pct"]
    )
    anchor = p["orders"][0].get(
        "anchor", p["orders"][0]["price"] / (1 - sign * offset / 100)
    )
    last = p["last_fill"] if orders["mode"] == "signal" else p["orders"][-1]["price"]
    return last - sign * anchor * p["settings"]["stop"]["pct"] / 100


def available_margin(state):
    """Return spendable PAPER margin only with fully priced open exposure.

    A missing, invalid or stale valuation is not zero unrealized loss. Refuse
    every new entry/DCA reservation until the position can be valued safely,
    regardless of whether the strategy runtime applied its own interlock.
    """
    value, unknown = equity(state)
    # Never spend a provisional valuation. An absent quote is just as
    # unverified as an expired one; the calling runtime is not the only entry
    # point (DCA and imports may invoke this accounting layer directly).
    if unknown:
        return -math.inf
    # A corrupted balance/reservation must never turn a comparison with NaN
    # into a successful PAPER admission (NaN < limit is always false).
    if type(value) not in (int, float) or not math.isfinite(value):
        return -math.inf
    try:
        held = reserved(state)
    except (TypeError, ValueError, KeyError, OverflowError):
        return -math.inf
    if type(held) not in (int, float) or not math.isfinite(held) or held < 0:
        return -math.inf
    # There is one authoritative collateral price per financial model.
    # Cross/isolated USDT margin is valued using independently validated
    # public Bybit mark-price and risk tiers (same source as liquidation).
    # Executable orderbook prices are NOT collateral marks. A stale depth
    # for an unrelated coin cannot invalidate an otherwise fully priced
    # margin portfolio or silently supply a guessed liquidation value.
    # Legacy/POLYMARKET accounts continue using their own strict quote check.
    for position in state["positions"].values():
        if position.get("kind") != "POLYMARKET" and state.get("margin_model") == MODEL:
            try:
                snapshot(state, position["symbol"])
            except (MarginUnavailable, KeyError, ValueError, TypeError):
                return -math.inf
            continue
        token = (position.get("token") if position.get("kind") == "POLYMARKET"
                 else position.get("symbol"))
        if token in state["marks"]:
            quote = state["marks"][token]
            if not valid_position_mark(position, quote) or not quote_is_recent(quote):
                return -math.inf
    return value - held


def update_equity_metrics(state):
    value, unknown = equity(state)
    if not unknown:
        peak = max(state.get("equity_peak", state["initial"]), value)
        state["equity_peak"] = peak
        state["max_drawdown"] = max(
            state.get("max_drawdown", 0), (peak - value) / peak * 100 if peak > 0 else 0
        )


def valid_position_mark(position, quote):
    """Strict numeric sanity for a quote used in PAPER financial decisions.

    Freshness is a separate requirement: a well-formed quote can still be stale.
    The same check guards liquidation and equity valuation.  Some historical
    quote records contain no receipt evidence and are therefore not trusted
    merely because they are persisted to SQLite.
    """
    if not isinstance(quote, dict):
        return False
    bid, ask = quote.get("bid"), quote.get("ask")
    if (type(bid) not in (int, float) or type(ask) not in (int, float)
            or not math.isfinite(bid) or not math.isfinite(ask)
            or bid > ask):
        return False
    if position.get("kind") == "POLYMARKET":
        return 0 <= bid <= ask <= 1
    return 0 < bid <= ask


def equity(state):
    """Return provisional equity and whether any position lacks a safe mark.

    Provisional equity is useful for diagnostics, not for collateral admission.
    ``available_margin`` and entry/reinvestment decisions must fail closed on
    the unknown flag rather than treating missing unrealized PnL as zero.
    """
    total = state["cash"]
    unknown = False
    for p in state["positions"].values():
        key = p.get("token") if p.get("kind") == "POLYMARKET" else p.get("symbol")
        quote = state["marks"].get(key)
        if p.get("kind") == "POLYMARKET":
            # An unresolved outcome is not a realized gain. Without a current,
            # valid bid, keep its cost as a provisional asset value.
            if valid_position_mark(p, quote) and quote_is_recent(quote):
                amount = p.get("shares", math.nan) * quote["bid"]
            else:
                amount = p.get("cost", math.nan)
                unknown = True
        elif state.get("margin_model") == MODEL:
            try:
                _, mark = snapshot(state, p["symbol"])
                amount = mark_pnl(p, mark)
            except MarginUnavailable:
                unknown = True
                continue
        elif valid_position_mark(p, quote):
            amount = mark_pnl(
                p, quote["bid"] if p.get("side") == "LONG" else quote["ask"]
            )
            if not quote_is_recent(quote):
                unknown = True
        else:
            # Never allow a NaN/Infinity from persisted market data to poison
            # equity and bypass a later numeric capital-limit comparison.
            unknown = True
            continue
        if type(amount) not in (int, float) or not math.isfinite(amount):
            unknown = True
            continue
        total += amount
    if type(total) not in (int, float) or not math.isfinite(total):
        return state["cash"], True
    return total, unknown



def fill_price(side, leg, bid, ask, settings):
    if (
        not all(
            type(x) in (int, float) and math.isfinite(x) and x > 0 for x in (bid, ask)
        )
        or bid > ask
    ):
        raise ValueError("Повреждена котировка")
    buy = (side == "LONG") == (leg == "ENTRY")
    price = ask if buy else bid
    return price * (1 + settings["slippage_pct"] / 100 * (1 if buy else -1))


def event(events, symbol, section, action, **values):
    item = dict(
        id=uuid.uuid4().hex,
        at=market_now(),
        symbol=symbol,
        section=section,
        action=action,
        **values,
    )
    events.append(item)
    return item


def enter(
    state,
    events,
    key,
    symbol,
    side,
    settings,
    price,
    margin,
    orders,
    section,
    *,
    reinvest_pct=0.0,
    existing_grid=False,
    entry_limit=None,
):
    if state.get("_entry_blocked"):
        return False, state["_entry_blocked"]
    if any(is_liquidating(p) for p in state["positions"].values()):
        return False, BLOCKED
    if state["strategy"].get("draining") and not existing_grid:
        return False, "Бот завершает текущие сделки; новые входы отключены"
    if key in state["positions"]:
        return False, "Позиция уже открыта"
    # Defend the low-level executor even if a caller bypasses config validation.
    if (type(margin) not in (int, float) or not math.isfinite(margin)
            or margin <= 0 or type(price) not in (int, float)
            or not math.isfinite(price) or price <= 0):
        return False, "Некорректная сумма или цена входа"
    first = margin * settings["leverage"] * orders[0]["weight"]
    fee = first * settings["fee_rate"]
    if (not math.isfinite(first) or first <= 0
            or not math.isfinite(fee) or fee < 0):
        return False, "Некорректный объём или комиссия входа"
    if state.get("margin_model") != MODEL and available_margin(state) < margin + fee:
        return False, "Недостаточно свободного капитала"
    if margin <= 0:
        return False, "Нулевая сумма секции"
    if not existing_grid:
        risk_reason = allocation_reason(state, margin)
        if risk_reason:
            return False, risk_reason
    qty = first / price
    if symbol not in state.get("_books", {}):
        return False, "Нет свежего стакана для исполнения"
    if symbol in state.get("_books", {}):
        resting = (
            settings["orders"]["first_offset_pct"] > 0
            or settings["orders"]["mode"] == "custom"
            and settings["orders"]["custom"][0]["offset_pct"] > 0
        )
        try:
            executor = PaperExecutor(state["_books"][symbol])
            limit = (
                entry_limit
                if entry_limit is not None
                else orders[0]["price"] if resting else None
            )
            if state.get("margin_model") == MODEL:
                preview = executor.estimate(
                    side, "ENTRY", settings, notional=first, limit=limit, consume=False
                )
                reason = entry_reason(
                    state, symbol, side, settings, margin, orders, first_fill=preview
                )
                if reason:
                    return False, reason
                required = entry_reserve(
                    state, side, settings, margin, orders, preview, symbol
                )
                free = available_margin(state)
                if free < required:
                    # Distinguish *exhausted capital* from *unverified prices*.
                    # Never turn an unknown collateral valuation into a number
                    # or bypass the original margin protection.
                    if not math.isfinite(free):
                        return False, (
                            "Нет достоверной оценки свободной маржи: "
                            "проверьте свежесть mark-price всех открытых позиций"
                        )
                    return (
                        False,
                        "Недостаточно маржи с учётом mark-price, глубины и комиссий: "
                        f"свободно {free:.2f} USDT, требуется {required:.2f} USDT "
                        "(включая резерв DCA)",
                    )
                _, mark = snapshot(state, symbol)
                upnl = mark_pnl(
                    dict(qty=preview.quantity, average=preview.price, side=side), mark
                )
                if not existing_grid:
                    risk_reason = allocation_reason(
                        state, required - preview.fee - max(0, -upnl)
                    )
                    if risk_reason:
                        return False, risk_reason
            fill = executor.estimate(
                side, "ENTRY", settings, notional=first, limit=limit
            )
        except (ExecutionRejected, MarginUnavailable) as e:
            return False, str(e)
        price = fill.price
        qty = fill.quantity
        first = fill.notional
        fee = fill.fee
    state["cash"] -= fee
    p = dict(
        position_id=uuid.uuid4().hex,
        symbol=symbol,
        side=side,
        section=section,
        opened=market_now(),
        entry=price,
        last_fill=price,
        average=price,
        qty=qty,
        fees=fee,
        funding_total=0.0,
        reinvest_pct=reinvest_pct,
        margin=first / settings["leverage"],
        order_margin=(
            margin * sum(o["weight"] for o in orders[1:]) if len(orders) > 1 else 0
        ),
        budget=margin * settings["leverage"],
        spent=first,
        settings=deepcopy(settings),
        orders=deepcopy(orders),
        next_order=1,
        dca_fills=0,
        active_until=min(len(orders), 1 + settings["orders"]["partial_count"]),
        original_qty=qty,
        realized=0.0,
        targets_done=[],
        trail_peak=None,
        breakeven=False,
        quantity_timeline=[{"at": market_now(), "delta": qty}],
        execution_rules={
            k: state["_books"][symbol][k]
            for k in ("qty_step", "min_qty", "max_qty")
            if k in state["_books"][symbol]
        },
    )
    state["positions"][key] = p
    state["done"].pop(key, None)
    event(
        events,
        symbol,
        section,
        "OPEN",
        price=price,
        qty=p["qty"],
        fee=fee,
        cash_delta=-fee,
        net_pnl=-fee,
        side=side,
        fill_status="PARTIAL_IOC" if fill.partial else "FILLED",
        leverage=settings['leverage'],
        margin_mode=settings['margin_mode'],
        position_terms_sha256=terms_digest(p),
        **receipt(p, key),
        **fill.evidence(),
    )
    return True, "Открыто"


def add_order(state, events, key, price):
    if state.get("_entry_blocked"):
        return False
    p = state["positions"][key]
    if is_liquidating(p):
        p["dca_block_reason"] = BLOCKED
        return False
    index = p["next_order"]
    settings = p["settings"]
    reason = entry_reason(state, p["symbol"], p["side"], settings, reserved=True)
    if reason:
        p["dca_block_reason"] = reason
        return False
    p.pop("dca_block_reason", None)
    if index >= len(p["orders"]):
        return False
    amount = p["budget"] * p["orders"][index]["weight"]
    fee = amount * settings["fee_rate"]
    if (not math.isfinite(amount) or amount <= 0
            or not math.isfinite(fee) or fee < 0):
        return False
    if state.get("margin_model") != MODEL and available_margin(state) < fee:
        return False
    qty = amount / price
    if p["symbol"] not in state.get("_books", {}):
        return False
    if p["symbol"] in state.get("_books", {}):
        limit = (
            p["orders"][index]["price"]
            if settings["orders"]["mode"] != "signal"
            else None
        )
        try:
            executor = PaperExecutor(state["_books"][p["symbol"]])
            if state.get("margin_model") == MODEL:
                preview = executor.estimate(
                    p["side"],
                    "ENTRY",
                    settings,
                    notional=amount,
                    limit=limit,
                    consume=False,
                )
                reason = entry_reason(
                    state,
                    p["symbol"],
                    p["side"],
                    settings,
                    reserved=True,
                    extra_value=dca_extra_value(state, p, preview),
                )
                if reason or available_margin(state) + 1e-8 < dca_free_required(
                    state, p, preview
                ):
                    p["dca_block_reason"] = (
                        reason
                        or "Недостаточно маржи для DCA с учётом mark-price и комиссий"
                    )
                    return False
            fill = executor.estimate(
                p["side"], "ENTRY", settings, notional=amount, limit=limit
            )
        except (ExecutionRejected, MarginUnavailable):
            return False
        price = fill.price
        qty = fill.quantity
        amount = fill.notional
        fee = fill.fee
    trail_price = None
    if p["trail_peak"] is not None:
        sign = 1 if p["side"] == "LONG" else -1
        trail_price = p["average"] * (1 + sign * p["trail_peak"] / 100)
    p["average"] = (p["average"] * p["qty"] + amount) / (p["qty"] + qty)
    if trail_price is not None:
        p["trail_peak"] = 100 * (trail_price / p["average"] - 1) * sign
    p["qty"] += qty
    p["original_qty"] += qty
    p["fees"] += fee
    p["spent"] += amount
    p["next_order"] += 1
    p["dca_fills"] = p.get("dca_fills", 0) + 1
    stats = state["stats"].setdefault(
        key, {"cycles": 0, "wins": 0, "net_pnl": 0, "max_dca": 0}
    )
    stats["max_dca"] = max(stats.get("max_dca", 0), p["dca_fills"])
    p["margin"] += amount / settings["leverage"]
    p["order_margin"] = (
        p["budget"]
        / settings["leverage"]
        * sum(o["weight"] for o in p["orders"][p["next_order"] :])
    )
    p["last_fill"] = price
    state["cash"] -= fee
    p.setdefault("quantity_timeline", []).append({"at": market_now(), "delta": qty})
    event(
        events,
        p["symbol"],
        p["section"],
        "DCA",
        price=price,
        qty=qty,
        fee=fee,
        cash_delta=-fee,
        net_pnl=-fee,
        index=index,
        side=p["side"],
        fill_status="PARTIAL_IOC" if fill.partial else "FILLED",
        **receipt(p, key),
        **fill.evidence(),
    )
    return True


def exit_position(
    state,
    events,
    key,
    price,
    reason,
    ratio=1.0,
    *,
    limit=None,
    target_index=None,
    execution_policy=None,
):
    p = state["positions"].get(key)
    if not p:
        return
    ratio = min(1.0, max(0.0, ratio))
    if ratio <= 0:
        return
    if reason == "MANUAL_PAPER_CLOSE" and p.get("liquidation"):
        old = p.pop("liquidation")
        old.update(stage="SUPERSEDED_BY_MANUAL_CLOSE", finished=market_now())
        p["last_liquidation"] = old
    qty = p["qty"] * ratio
    requested_qty = qty
    pending = {
        "reason": reason,
        "ratio": ratio,
        "limit": limit,
        "target_index": target_index,
    }
    if execution_policy is not None:
        if execution_policy not in ("ioc", "fok"):
            raise ValueError("Неверная политика исполнения выхода")
        pending["execution_policy"] = execution_policy
    liquidation_context = (
        deepcopy(p.get("liquidation", {}))
        if reason in ("PAPER_LIQUIDATION", "PAPER_CROSS_LIQUIDATION")
        else {}
    )
    p.pop("tp_deferred", None)
    if p["symbol"] not in state.get("_books", {}):
        p["exit_pending"] = pending
        return False
    if p["symbol"] in state.get("_books", {}):
        book = state["_books"][p["symbol"]]
        if (
            reason == "MULTI_TP"
            and target_index is not None
            and target_index < len(p["settings"]["profit"]["targets"]) - 1
        ):
            aligned = floor_quantity(qty, Decimal(str(book["qty_step"])))
            if aligned <= 0 or aligned < book["min_qty"] - 1e-12:
                p.pop("exit_pending", None)
                p["tp_deferred"] = {
                    "target": target_index,
                    "reason": "Объём тейка ниже биржевого минимума; переносится на следующий тейк",
                }
                return False
        try:
            if (
                type(book.get("qty_step")) not in (int, float)
                or not math.isfinite(book["qty_step"])
                or book["qty_step"] <= 0
            ):
                raise ExecutionRejected("Повреждён шаг количества выхода")
            requested_qty = floor_quantity(qty, Decimal(str(book["qty_step"])))
            physical_qty = requested_qty
            maximum = book.get("max_qty")
            if maximum is not None:
                if (
                    type(maximum) not in (int, float)
                    or not math.isfinite(maximum)
                    or maximum <= 0
                ):
                    raise ExecutionRejected("Повреждён максимум объёма выхода")
                physical_qty = min(
                    physical_qty,
                    floor_quantity(maximum, Decimal(str(book["qty_step"]))),
                )
            fill = PaperExecutor(state["_books"][p["symbol"]]).estimate(
                p["side"],
                "EXIT",
                {
                    **p["settings"],
                    "execution_policy": execution_policy
                    or p["settings"]["execution_policy"],
                },
                quantity=physical_qty,
                limit=limit,
                position_quantity=p["qty"],
            )
        except ExecutionRejected:
            p["exit_pending"] = pending
            return False
        qty = fill.quantity
        price = fill.price
        ratio = qty / p["qty"]
    unfilled = max(0, requested_qty - qty)
    request_complete = unfilled <= max(1e-12, requested_qty * 1e-10)
    p.pop("exit_pending", None)
    gross = (price - p["average"]) * qty * (1 if p["side"] == "LONG" else -1)
    fee = price * qty * p["settings"]["fee_rate"]
    entry_fee = p["fees"] * ratio
    delta = gross - fee
    liquidation_coverage = 0.0
    if (
        state.get("margin_model") == MODEL
        and p["settings"]["margin_mode"] == "ISOLATED"
        and reason == "PAPER_LIQUIDATION"
    ):
        liquidation_coverage = max(0.0, -p["margin"] * ratio - delta)
        delta += liquidation_coverage
    net = delta - entry_fee
    state["cash"] += delta
    p["realized"] += net
    p["fees"] -= entry_fee
    p["qty"] -= qty
    if not request_complete and p["qty"] > 0:
        pending["ratio"] = min(1.0, unfilled / p["qty"])
        p["exit_pending"] = pending
    p["margin"] *= 1 - ratio
    p["order_margin"] = 0
    p.setdefault("quantity_timeline", []).append({"at": market_now(), "delta": -qty})
    p["next_order"] = len(
        p["orders"]
    )  # Taking profit cancels every remaining averaging order.
    if (
        request_complete
        and target_index is not None
        and target_index not in p["targets_done"]
    ):
        p["targets_done"].append(target_index)
        profit = p["settings"]["profit"]
        sign = 1 if p["side"] == "LONG" else -1
        if profit["breakeven"]:
            p["breakeven"] = True
            base = p["average"]
            if target_index > 0 and profit["breakeven_reference"] == "take_profit":
                base *= 1 + sign * profit["targets"][target_index - 1]["pct"] / 100
            p["breakeven_price"] = base * (
                1 + sign * profit["breakeven_offset_pct"] / 100
            )
    close_event = event(
        events,
        p["symbol"],
        p["section"],
        "CLOSE" if ratio >= 1 - 1e-10 else "PARTIAL",
        price=price,
        qty=qty,
        fee=fee,
        cash_delta=delta,
        net_pnl=delta,
        cycle_net_pnl=net,
        reason=reason,
        margin_model=state.get('margin_model'),
        side=p["side"],
        fill_status="PARTIAL_IOC" if fill.partial else "FILLED",
        **receipt(p, key),
        **fill.evidence(),
    )
    if p.get("margin_evidence"):
        close_event["margin_evidence"] = deepcopy(p["margin_evidence"])
    if liquidation_context:
        close_event["liquidation"] = liquidation_context
    if (
        reason == "MANUAL_PAPER_CLOSE"
        and p.get("last_liquidation", {}).get("stage") == "SUPERSEDED_BY_MANUAL_CLOSE"
    ):
        close_event["superseded_liquidation"] = deepcopy(p["last_liquidation"])
    if liquidation_coverage:
        close_event.update(
            execution_cash_delta=gross - fee,
            paper_isolated_loss_coverage=liquidation_coverage,
        )
    if ratio >= 1 - 1e-10:
        deal_closed(state)
        trading_net = p["realized"] + p.get("funding_total", 0)
        reinvested = max(0, trading_net) * p.get("reinvest_pct", 0) / 100
        if reinvested:
            state["strategy"]["veles_reinvested"] = (
                state["strategy"].get("veles_reinvested", 0) + reinvested
            )
        close_event.update(trading_net_pnl=trading_net, reinvested=reinvested)
        stats = state["stats"].setdefault(key, {"cycles": 0, "wins": 0, "net_pnl": 0})
        stats["cycles"] += 1
        stats["wins"] += int(p["realized"] > 0)
        stats["net_pnl"] += p["realized"]
        # The public funding rate may be published after this trade is closed.
        # Retain its exposure until a later funding timestamp confirms coverage.
        state.setdefault("funding_archive", {})[cycle_reference(key, p)] = {
            "key": key,
            "symbol": p["symbol"],
            "section": p["section"],
            "side": p["side"],
            "opened": p["opened"],
            "closed": market_now(),
            "cycle_pnl": p["realized"],
            "quantity_timeline": deepcopy(p["quantity_timeline"]),
            **receipt(p, key),
        }
        if p["settings"]["stop_after_cycle"]:
            state["done"][key] = True
        if p["settings"]["stop"].get("stop_after") and reason in (
            "STOP_LOSS",
            "SIGNAL_STOP",
        ):
            state["done"][key] = "STOP_LOSS"
        del state["positions"][key]
    return True


def protective_exit(state, events, key, bid, ask):
    p = state["positions"][key]
    # A saved exit (manual close, stop, partial IOC, liquidation or TP) must be
    # completed by the serial retry path. Never replace its reason/quantity or
    # limit with a fresh signal while the first request remains outstanding.
    if p.get("exit_pending"):
        return False
    s = p["settings"]
    sign = 1 if p["side"] == "LONG" else -1
    quote = bid if sign == 1 else ask
    gain = 100 * (quote / p["average"] - 1) * sign
    profit = s["profit"]
    stop = s["stop"]
    reason = None
    if p["breakeven"] and (quote - p.get("breakeven_price", p["average"])) * sign <= 0:
        reason = "BREAKEVEN"
    if stop["mode"] in ("percent", "both") and p["next_order"] >= len(p["orders"]):
        limit = percent_stop_price(p)
        if (quote - limit) * sign <= 0:
            reason = "STOP_LOSS"
    if profit["trailing"] and gain >= profit["activation_pct"]:
        p["trail_peak"] = max(
            gain, p["trail_peak"] if p["trail_peak"] is not None else gain
        )
    if p["trail_peak"] is not None and gain <= p["trail_peak"] - profit["distance_pct"]:
        reason = "TRAILING"
    if (
        reason is None
        and profit["mode"] == "simple"
        and gain >= profit["pct"]
        and not profit["trailing"]
    ):
        reason = "TAKE_PROFIT"
    if reason:
        exit_position(
            state,
            events,
            key,
            fill_price(p["side"], "EXIT", bid, ask, s),
            reason,
            limit=(
                p["average"] * (1 + sign * profit["pct"] / 100)
                if reason == "TAKE_PROFIT"
                else None
            ),
        )
        return True
    if p.get("exit_pending"):
        return False
    if profit["mode"] == "custom":
        for idx, target in enumerate(profit["targets"]):
            if idx in p["targets_done"] or gain < target["pct"]:
                continue
            # Individual partial targets round down to the lot step. The final
            # target closes the actual remainder, including accumulated dust.
            cumulative = (
                p["original_qty"]
                * sum(t["volume_pct"] for t in profit["targets"][: idx + 1])
                / 100
            )
            needed = cumulative - (p["original_qty"] - p["qty"])
            if needed <= 1e-12:
                continue
            fraction = (
                1 if idx == len(profit["targets"]) - 1 else min(1, needed / p["qty"])
            )
            completed = exit_position(
                state,
                events,
                key,
                fill_price(p["side"], "EXIT", bid, ask, s),
                "MULTI_TP",
                fraction,
                limit=p["average"] * (1 + sign * target["pct"] / 100),
                target_index=idx,
            )
            if not completed:
                if p.get("tp_deferred", {}).get("target") == idx:
                    continue
                return True
            if key not in state["positions"] or p.get("exit_pending"):
                return True
    return False


def _liquidation_exit(state, events, key, p, q, evidence, reason):
    # Cancel future increases once and preserve why the accepted grid stopped.
    if p.get("order_margin", 0) or p["next_order"] < len(p["orders"]):
        p["order_margin"] = 0
        p["next_order"] = len(p["orders"])
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
    p["margin_evidence"] = evidence
    state["_entry_blocked"] = (
        "Выполняется ликвидация PAPER; новые входы и DCA приостановлены"
    )
    # The trigger is the public mark, but an execution always consumes depth.
    # If there is no executable book, exit_position persists the pending exit.
    bid, ask = q.get("bid", p["average"]), q.get("ask", p["average"])
    exit_position(
        state,
        events,
        key,
        fill_price(p["side"], "EXIT", bid, ask, p["settings"]),
        reason,
    )


def liquidations(state, events, maintenance=0.005):
    if state.get("margin_model") == MODEL:
        from .liquidation import process_liquidations
        before = len(events)
        process_liquidations(state, events)
        return bool(state.get("_entry_blocked")) or any(
            e.get("reason") in ("PAPER_LIQUIDATION", "PAPER_CROSS_LIQUIDATION")
            for e in events[before:]
        )
    # Report a margin breach even if its close fills immediately. The caller
    # must not admit fresh positions on the same tick as forced liquidation.
    breached = False
    cross = []
    for key, p in list(state["positions"].items()):
        if p.get("kind") == "POLYMARKET":
            continue
        q = state["marks"].get(p["symbol"])
        if not valid_position_mark(p, q) or not quote_is_recent(q):
            continue
        quote = q["bid"] if p["side"] == "LONG" else q["ask"]
        upnl = mark_pnl(p, quote)
        need = p["qty"] * quote * (maintenance + p["settings"]["fee_rate"])
        if p["settings"]["margin_mode"] == "ISOLATED":
            # The serial exit retry owns an outstanding manual/TP/stop close.
            # Never replace its reason, ratio, limit or target during liquidation.
            if p["margin"] + upnl <= need:
                breached = True
                if p.get("exit_pending"):
                    continue
                exit_position(
                    state,
                    events,
                    key,
                    fill_price(p["side"], "EXIT", q["bid"], q["ask"], p["settings"]),
                    "PAPER_LIQUIDATION",
                )
        else:
            cross.append((key, p, upnl, need))
    isolated = sum(
        p["margin"]
        for p in state["positions"].values()
        if p.get("settings", {}).get("margin_mode") == "ISOLATED"
    )
    all_cross = [
        p
        for p in state["positions"].values()
        if p.get("settings", {}).get("margin_mode") == "CROSS"
    ]
    if (
        cross
        and len(cross) == len(all_cross)
        and state["cash"] - isolated + sum(x[2] for x in cross)
        <= sum(x[3] for x in cross)
    ):
        breached = True
        for key, p, _, _ in cross:
            if p.get("exit_pending"):
                continue
            q = state["marks"][p["symbol"]]
            exit_position(
                state,
                events,
                key,
                fill_price(p["side"], "EXIT", q["bid"], q["ask"], p["settings"]),
                "PAPER_CROSS_LIQUIDATION",
            )
    return breached

