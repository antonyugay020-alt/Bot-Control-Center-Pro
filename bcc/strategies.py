"""Strategy decisions. Public data enters here; no strategy can submit real orders."""

import hashlib
import json
from copy import deepcopy

from .config import coin_section, effective, merge
from .coin_allocation import COIN_MODES, coin_policy, remaining_coin_budget
from .indicators import evaluate
from .margin import MODEL, entry_reason, pending_reserve
from .liquidation import is_liquidating, status as liquidation_status
from .shared_risk import allocation_reason
from .trading import (
    add_order,
    available_margin,
    enter,
    equity,
    event,
    exit_position,
    fill_price,
    mark_pnl,
    plan,
    protective_exit,
)


def signal(state, key, spec, bars, now_ms):
    signature = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    old = state["clocks"].get(key, {})
    same = old.get("signature") == signature
    result = evaluate(spec, bars, now_ms, old.get("values", {}) if same else {})
    if old and not same:
        # Changing indicator/period/TF must never reinterpret the old live-bar
        # delta as a crossing in the new signal definition.
        result.matched = False
    state["clocks"][key] = {"signature": signature, "values": result.clocks}
    return result


def capital_margin(cfg, state, settings, symbol=None):
    if cfg["strategy"] == "VELES":
        # A per-coin budget belongs to the coin (including all active RSI
        # ranges and DCA grids), never to each range independently.
        if symbol is not None and coin_policy(cfg, symbol)["allocation_mode"] in COIN_MODES:
            return remaining_coin_budget(cfg, state, symbol)
        base = cfg["capital"] + (
            state["strategy"].get("veles_reinvested", 0) if cfg["reinvest"] else 0
        )
        return base * settings["allocation_pct"] / 100
    if cfg["reinvest"]:
        value, unknown = equity(state)
        if unknown:
            return 0.0  # Unpriced open risk cannot increase allocation.
        base = max(0, value)
    else:
        base = cfg["capital"]
    return base * settings["allocation_pct"] / 100


def daily_ok(cfg, state, symbol, settings, now_ms):
    value, unknown = equity(state)
    if unknown:
        # A provisional equity estimate is NOT an admissible daily baseline.
        # Even where the Bybit runtime independently blocks new positions on
        # stale symbols, writing this value into state["daily"] corrupts the
        # following day's loss limit after fresh marks become available.
        # This guard is shared by all strategies, including Polymarket.
        return False
    day = str(now_ms // 86400000)
    if state["daily"].get("day") != day:
        state["daily"] = {"day": day, "start": value}
    return value >= state["daily"]["start"] * (
        1 - settings["daily_loss_pct"] / 100
    )


def simple_rsi_boundary(spec):
    """Return a simple, closed-bar RSI crossing, never a composite/filter.

    A handoff must not silently bypass additional indicator conditions. Entry
    and exit have independent timeframes; their RSI *period* and crossing
    direction must still agree for the boundary to represent one range edge.
    """
    if (not isinstance(spec, dict) or spec.get("operator") != "AND"
            or spec.get("groups") or len(spec.get("conditions", [])) != 1):
        return None
    c = spec["conditions"][0]
    if not isinstance(c, dict) or any((
        c.get("indicator") != "RSI",
        c.get("reference") != "number",
        c.get("output", "value") != "value",
        c.get("trigger") != "bar_close",
        c.get("comparison") not in ("cross_up", "cross_down"),
        c.get("shift", 0) != 0,
        bool(c.get("params", {})),
    )):
        return None
    return (c["comparison"], c["period"], c["value"])


def rsi_handoff_target(cfg, symbol, source_key, source_side, exit_spec):
    """Find a UNIQUE adjacent RSI range. Ambiguous/custom routes fail closed."""
    if cfg["strategy"] != "VELES":
        return None
    edge = simple_rsi_boundary(exit_spec)
    if edge is None or edge[0] != ("cross_up" if source_side == "LONG" else "cross_down"):
        return None
    over = cfg["coin_overrides"].get(symbol, {})
    matches = []
    for original in cfg["sections"]:
        section = coin_section(cfg, symbol, original)
        key = symbol + ":" + section["id"]
        if key == source_key or section["side"] != source_side:
            continue
        patch = over.get("sections", {}).get(section["id"], {})
        entry_spec = patch.get("entry", over.get("entry", section["entry"]))
        if simple_rsi_boundary(entry_spec) == edge:
            matches.append(key)
    return matches[0] if len(matches) == 1 else None


def startup_rsi_range(cfg, state, symbol, bars, now_ms):
    """Select one unambiguous active VELES band at startup, never a synthetic cross.

    Only pure RSI sections are eligible; composite rules must produce their own
    actual entry signals. A closed 2-minute RSI establishes direction, while
    the configured entry and exit thresholds define the current range.
    """
    from .indicators import aggregate, series

    if cfg["strategy"] != "VELES":
        return None
    if state["strategy"].get("veles_bootstrapped", {}).get(symbol):
        return None
    if any(p["symbol"] == symbol for p in state["positions"].values()):
        return None
    if any(p.get("symbol") == symbol for p in state["pending"].values()):
        return None
    candidates = []
    for original in cfg["sections"]:
        sec = coin_section(cfg, symbol, original)
        settings = effective(cfg, symbol, sec)
        if not sec["enabled"] or settings["disabled"]:
            continue
        over = cfg["coin_overrides"].get(symbol, {})
        patch = over.get("sections", {}).get(sec["id"], {})
        entry = patch.get("entry", over.get("entry", sec["entry"]))
        end = patch.get("exit", over.get("exit", sec["exit"]))
        start_edge, end_edge = simple_rsi_boundary(entry), simple_rsi_boundary(end)
        direction = "cross_up" if sec["side"] == "LONG" else "cross_down"
        if not start_edge or not end_edge or start_edge[0] != direction or end_edge[0] != direction:
            continue
        if start_edge[1] != end_edge[1]:
            continue
        lo, hi = start_edge[2], end_edge[2]
        if (sec["side"] == "LONG" and not lo < hi or
                sec["side"] == "SHORT" and not lo > hi):
            continue
        frames = aggregate(bars, entry["conditions"][0]["timeframe"], now_ms)
        if len(frames) < start_edge[1] + 2:
            continue
        vals = series("RSI", frames, start_edge[1])
        a, b = vals[-2:]
        if a is None or b is None:
            continue
        inside = lo <= b < hi if sec["side"] == "LONG" else hi < b <= lo
        moving = b > a if sec["side"] == "LONG" else b < a
        if inside and moving:
            candidates.append(symbol + ":" + sec["id"])
    return candidates[0] if len(candidates) == 1 else None


def startup_rsi_rejection(cfg, state, symbol, section, bars, now_ms, selected):
    """Explain a skipped startup RSI admission without changing a trade decision.

    This is read-only and deliberately never treats composites as eligible.
    """
    from .indicators import aggregate, series

    if state["strategy"].get("veles_bootstrapped", {}).get(symbol):
        return "Начальная синхронизация RSI уже выполнена"
    if any(p["symbol"] == symbol for p in state["positions"].values()):
        return "По монете уже открыта позиция"
    if any(p.get("symbol") == symbol for p in state["pending"].values()):
        return "По монете ожидает исполнение сетка"
    over = cfg["coin_overrides"].get(symbol, {})
    patch = over.get("sections", {}).get(section["id"], {})
    entry = patch.get("entry", over.get("entry", section["entry"]))
    end = patch.get("exit", over.get("exit", section["exit"]))
    a, b = simple_rsi_boundary(entry), simple_rsi_boundary(end)
    direction = "cross_up" if section["side"] == "LONG" else "cross_down"
    if a is None or b is None:
        return "Начальный вход недоступен: составной или нестандартный RSI-сигнал"
    if a[0] != direction or b[0] != direction or a[1] != b[1]:
        return "Начальный вход недоступен: несовместимые границы RSI"
    lo, hi = a[2], b[2]
    if (section["side"] == "LONG" and lo >= hi) or (section["side"] == "SHORT" and lo <= hi):
        return "Начальный вход недоступен: неверный порядок границ RSI"
    frames = aggregate(bars, entry["conditions"][0]["timeframe"], now_ms)
    if len(frames) < a[1] + 2:
        return f"Недостаточно закрытых RSI-свечей: {len(frames)} из {a[1]+2}"
    vals = series("RSI", frames, a[1])
    previous, current = vals[-2:]
    if previous is None or current is None:
        return "RSI ещё не рассчитан на закрытых свечах"
    inside = lo <= current < hi if section["side"] == "LONG" else hi < current <= lo
    if not inside:
        return f"RSI {current:.2f} вне диапазона {min(lo,hi):g}–{max(lo,hi):g}"
    moving = current > previous if section["side"] == "LONG" else current < previous
    if not moving:
        return f"RSI внутри диапазона, но нет движения {section['side']}: {previous:.2f} → {current:.2f}"
    if selected is None:
        return "Начальный вход не выбран: несколько подходящих RSI-диапазонов"
    if selected != symbol + ":" + section["id"]:
        return "Начальный вход назначен другой секции"
    return "Стартовый RSI-сигнал принят; проверяются риск и исполнение"


class Veles:
    def process(self, cfg, state, events, symbol, bars, bid, ask, now_ms):
        over = cfg["coin_overrides"].get(symbol, {})
        diagnostics = []
        handoff_targets = set()
        startup_target = startup_rsi_range(cfg, state, symbol, bars, now_ms)
        # All exits precede any entry, including a shared RSI boundary.
        for key, p in list(state["positions"].items()):
            if p["symbol"] != symbol:
                continue
            # A pending close is replayed by process_tick before strategy
            # evaluation. Do not let signal stops, TP/SL or DCA overwrite it.
            if p.get("exit_pending") or is_liquidating(p):
                diagnostics.append({
                    "section": p["section"] + " / выход",
                    "reason": "Ожидание исполнения ранее принятого выхода",
                })
                continue
            if protective_exit(state, events, key, bid, ask):
                continue
            p = state["positions"].get(key)
            if not p:
                continue
            settings = p["settings"]
            gain = (
                100
                * mark_pnl(p, bid if p["side"] == "LONG" else ask)
                / (p["average"] * p["qty"])
            )
            if settings["stop"]["mode"] in ("signal", "both"):
                st = settings["stop"]
                res = signal(state, key + ":stop", st["signal"], bars, now_ms)
                after_grid = p["next_order"] >= len(p["orders"])
                reference = (
                    p["last_fill"] if st["reference"] == "last" else p["average"]
                )
                delta = (
                    100
                    * ((bid if p["side"] == "LONG" else ask) / reference - 1)
                    * (1 if p["side"] == "LONG" else -1)
                )
                if (
                    res.matched
                    and (st["reference"] != "last" or after_grid)
                    and (st["minimum_pct"] is None or delta <= st["minimum_pct"])
                ):
                    exit_position(
                        state,
                        events,
                        key,
                        fill_price(p["side"], "EXIT", bid, ask, settings),
                        "SIGNAL_STOP",
                    )
                    continue
            if p.get("exit_pending"):
                continue
            spec = p["exit_signal"]
            result = signal(state, key + ":exit", spec, bars, now_ms)
            minimum = settings["profit"]["minimum_pct"]
            if settings["profit"]["mode"] == "signal":
                why = (
                    "Ожидание условий выхода"
                    if result.available
                    else "Недостаточно истории для выхода"
                )
                if result.matched and minimum is not None and gain < minimum:
                    why = f"Сигнал выхода есть; PnL {gain:.3f}% ниже {minimum:g}%"
                diagnostics.append(
                    {
                        "section": p["section"] + " / выход",
                        "reason": why,
                        "conditions": result.observations,
                    }
                )
            else:
                diagnostics.append(
                    {
                        "section": p["section"] + " / выход",
                        "reason": f"TP: {settings['profit']['mode']}; движение от средней {gain:+.3f}%"
                        + (
                            "; " + p["tp_deferred"]["reason"]
                            if p.get("tp_deferred")
                            else ""
                        ),
                    }
                )
            if (
                settings["profit"]["mode"] == "signal"
                and result.matched
                and (minimum is None or gain >= minimum)
            ):
                exit_position(
                    state,
                    events,
                    key,
                    fill_price(p["side"], "EXIT", bid, ask, settings),
                    "SIGNAL_EXIT",
                )
                # Only a fully executed CLOSE transfers the confirmed signal.
                # A deferred/partial IOC exit must not open a second position.
                if key not in state["positions"]:
                    target = rsi_handoff_target(
                        cfg, symbol, key, p["side"], spec
                    )
                    if target is not None:
                        handoff_targets.add(target)
                continue
            orders = settings["orders"]
            if p["next_order"] < len(p["orders"]):
                quote = ask if p["side"] == "LONG" else bid
                sign = 1 if p["side"] == "LONG" else -1
                if orders["mode"] == "signal":
                    res = signal(state, key + ":dca", orders["signal"], bars, now_ms)
                    anchor = (
                        p["last_fill"]
                        if orders["signal_reference"] == "last"
                        else p["entry"]
                    )
                    hit = (
                        res.matched
                        and (anchor - quote) * sign
                        >= anchor * orders["signal_spacing_pct"] / 100
                    )
                else:
                    hit = (quote - p["orders"][p["next_order"]]["price"]) * sign <= 0
                # Partial placement controls the visible window of pending levels;
                # only executable quotes produce fills, never retrospective highs.
                if hit:
                    active = p.get(
                        "active_until",
                        min(
                            len(p["orders"]), p["next_order"] + orders["partial_count"]
                        ),
                    )
                    while p["next_order"] < active:
                        if orders["mode"] == "signal" and p["next_order"] > 0:
                            if not add_order(
                                state,
                                events,
                                key,
                                fill_price(p["side"], "ENTRY", bid, ask, settings),
                            ):
                                break
                            break
                        if (quote - p["orders"][p["next_order"]]["price"]) * sign > 0:
                            break
                        if not add_order(
                            state,
                            events,
                            key,
                            fill_price(p["side"], "ENTRY", bid, ask, settings),
                        ):
                            break
                    p["active_until"] = min(
                        len(p["orders"]), p["next_order"] + orders["partial_count"]
                    )
                    protective_exit(state, events, key, bid, ask)
                if p.get("dca_block_reason"):
                    diagnostics.append(
                        {
                            "section": p["section"] + " / DCA",
                            "reason": p["dca_block_reason"],
                        }
                    )
        for original in cfg["sections"]:
            section = coin_section(cfg, symbol, original)
            key = symbol + ":" + section["id"]
            settings = effective(cfg, symbol, section)
            if key in state["positions"]:
                continue
            pending = state["pending"].get(key)
            if pending:
                quote = ask if pending["side"] == "LONG" else bid
                sign = 1 if pending["side"] == "LONG" else -1
                if (quote - pending["orders"][0]["price"]) * sign <= 0:
                    del state["pending"][key]
                    ok, why = enter(
                        state,
                        events,
                        key,
                        symbol,
                        pending["side"],
                        pending["settings"],
                        fill_price(
                            pending["side"], "ENTRY", bid, ask, pending["settings"]
                        ),
                        pending["margin"],
                        pending["orders"],
                        section["id"],
                        reinvest_pct=pending.get("reinvest_pct", 0),
                        existing_grid=True,
                    )
                    if ok:
                        state["positions"][key]["exit_signal"] = pending["exit_signal"]
                    else:
                        state["pending"][key] = pending
                    diagnostics.append({"section": section["label"], "reason": why})
                    continue
                pull = pending["settings"]["orders"]["pull_up_pct"]
                if pull > 0 and (quote / pending["anchor"] - 1) * sign >= pull / 100:
                    del state["pending"][key]
                    event(
                        events,
                        symbol,
                        section["id"],
                        "CANCEL_GRID",
                        reason="PULL_UP",
                        cash_delta=0,
                        net_pnl=0,
                    )
                else:
                    diagnostics.append(
                        {
                            "section": section["label"],
                            "reason": "Ожидание первого лимитного ордера",
                        }
                    )
                    continue
            # Disabling new signals must not strand an already accepted grid.
            # Its saved settings still govern execution and pull-up cancellation.
            if (
                not section["enabled"]
                or settings["disabled"]
                or state["done"].get(key)
                and settings["stop_after_cycle"]
                or state["done"].get(key) == "STOP_LOSS"
                and settings["stop"].get("stop_after")
            ):
                diagnostics.append(
                    {
                        "section": section["label"],
                        "reason": (
                            "Новые входы отключены"
                            if not section["enabled"] or settings["disabled"]
                            else (
                                "Секция остановлена после стоп-лосса"
                                if state["done"].get(key) == "STOP_LOSS"
                                else "Секция остановлена после завершения сделки"
                            )
                        ),
                    }
                )
                continue
            if state["strategy"].get("draining"):
                diagnostics.append(
                    {
                        "section": section["label"],
                        "reason": "Бот завершает текущие сделки; новые входы отключены",
                    }
                )
                continue
            sec_over = over.get("sections", {}).get(section["id"], {})
            spec = sec_over.get("entry", over.get("entry", section["entry"]))
            result = signal(state, key + ":entry", spec, bars, now_ms)
            # A previous section closed on this exact RSI boundary. Do not
            # wait for the independent entry timeframe to cross it again.
            handoff = key in handoff_targets
            bootstrap = key == startup_target
            why = (
                "Недостаточно истории"
                if not result.available
                else "Ожидание условий входа"
            )
            if state.get("_entry_blocked"):
                diagnostics.append(
                    {"section": section["label"], "reason": state["_entry_blocked"]}
                )
                continue
            if (result.matched or handoff or bootstrap) and daily_ok(cfg, state, symbol, settings, now_ms):
                margin = capital_margin(cfg, state, settings, symbol)
                if margin <= 0:
                    diagnostics.append({
                        "section": section["label"],
                        "reason": "Бюджет капитала монеты исчерпан",
                    })
                    continue
                anchor = ask if section["side"] == "LONG" else bid
                order_plan = plan(settings["orders"], section["side"], anchor)
                margin_reason = entry_reason(
                    state, symbol, section["side"], settings, margin, order_plan
                )
                if margin_reason:
                    diagnostics.append(
                        {"section": section["label"], "reason": margin_reason}
                    )
                    continue
                exit_signal = deepcopy(
                    sec_over.get("exit", over.get("exit", section["exit"]))
                )
                if (
                    settings["orders"]["first_offset_pct"] > 0
                    or settings["orders"]["mode"] == "custom"
                    and settings["orders"]["custom"][0]["offset_pct"] > 0
                ):
                    reservation = (
                        pending_reserve(state, dict(margin=margin, settings=settings))
                        if state.get("margin_model") == MODEL
                        else margin * (1 + settings["leverage"] * settings["fee_rate"])
                    )
                    risk_reason = allocation_reason(
                        state,
                        reservation if state.get("margin_model") == MODEL else margin,
                    )
                    if risk_reason:
                        why = risk_reason
                    elif available_margin(state) >= reservation:
                        state["pending"][key] = dict(
                            symbol=symbol,
                            side=section["side"],
                            settings=deepcopy(settings),
                            orders=order_plan,
                            margin=margin,
                            anchor=anchor,
                            exit_signal=exit_signal,
                            reinvest_pct=(
                                settings.get("reinvest_pct", 20)
                                if cfg["strategy"] == "VELES" and cfg["reinvest"]
                                else 0
                            ),
                        )
                        why = "Сетка выставлена; ожидание первого ордера"
                        if bootstrap:
                            state["strategy"].setdefault("veles_bootstrapped", {})[symbol] = True
                    else:
                        why = "Недостаточно свободного капитала"
                else:
                    ok, why = enter(
                        state,
                        events,
                        key,
                        symbol,
                        section["side"],
                        settings,
                        fill_price(section["side"], "ENTRY", bid, ask, settings),
                        margin,
                        order_plan,
                        section["id"],
                        reinvest_pct=(
                            settings.get("reinvest_pct", 20)
                            if cfg["strategy"] == "VELES" and cfg["reinvest"]
                            else 0
                        ),
                    )
                    if ok:
                        state["positions"][key]["exit_signal"] = exit_signal
                        if bootstrap:
                            state["strategy"].setdefault("veles_bootstrapped", {})[symbol] = True
            elif result.matched:
                why = "Дневной лимит убытка"
            if (not result.matched and not handoff and not bootstrap
                    and why == "Ожидание условий входа"):
                why = startup_rsi_rejection(
                    cfg, state, symbol, section, bars, now_ms, startup_target
                )
            diagnostics.append(
                {
                    "section": section["label"],
                    "reason": why,
                    "conditions": result.observations,
                }
            )
        state["diagnostics"][symbol] = diagnostics



def record_completed_exit(cfg, state, key, position, now_ms):
    """Apply strategy re-entry cooldown ONLY on a fully executed PAPER close.

    Normal strategy exits and deferred IOC retries must share this lifecycle
    event. An unfilled close request cannot start a cooldown prematurely.
    """
    if key in state["positions"]:
        return False
    if cfg["strategy"] == "GRID":
        state["strategy"].setdefault(position["symbol"], {}).setdefault(
            "last_exit", {}
        )[key] = now_ms
    elif cfg["strategy"] == "PUMP_DUMP":
        state["strategy"].setdefault(position["symbol"], {}).setdefault(
            "cooldowns", {}
        )[position["section"]] = now_ms / 1000
    return True


class Grid:
    def process(self, cfg, state, events, symbol, bars, bid, ask, now_ms):
        settings = effective(cfg, symbol)
        diagnostics = []
        g = merge(
            cfg["grid"], cfg["coin_overrides"].get(symbol, {}).get("parameters", {})
        )
        side = g["side"]
        sign = 1 if side == "LONG" else -1
        runtime = state["strategy"].setdefault(symbol, {})
        anchor = runtime.setdefault("anchor", ask if side == "LONG" else bid)
        for key, p in list(state["positions"].items()):
            if p["symbol"] != symbol:
                continue
            # The runtime retries the original IOC/close intent first.
            # A subsequent TP/SL must not overwrite an unfilled close.
            if p.get("exit_pending") or is_liquidating(p):
                continue
            active_g = p.get("strategy_params", g)
            active_sign = 1 if p["side"] == "LONG" else -1
            active_quote = bid if active_sign == 1 else ask
            gain = 100 * (active_quote / p["average"] - 1) * active_sign
            if (
                gain >= active_g["take_profit_pct"]
                or gain <= -active_g["stop_loss_pct"]
            ):
                exit_position(
                    state,
                    events,
                    key,
                    fill_price(p["side"], "EXIT", bid, ask, p["settings"]),
                    "GRID_TP" if gain > 0 else "GRID_SL",
                )
                record_completed_exit(cfg, state, key, p, now_ms)
        for i in range(g["levels"]):
            key = f"{symbol}:grid_{i}"
            target = anchor * (1 - sign * i * g["spacing_pct"] / 100)
            price = ask if side == "LONG" else bid
            if (
                key in state["positions"]
                or runtime.get("last_exit", {}).get(key, 0) + 1000 > now_ms
                or settings["disabled"]
                or state["done"].get(key)
                and settings["stop_after_cycle"]
            ):
                continue
            if (price - target) * sign <= 0 and daily_ok(
                cfg, state, symbol, settings, now_ms
            ):
                ok, why = enter(
                    state,
                    events,
                    key,
                    symbol,
                    side,
                    settings,
                    fill_price(side, "ENTRY", bid, ask, settings),
                    g["order_usdt"] / settings["leverage"],
                    [{"price": target, "weight": 1}],
                    f"grid_{i}",
                    entry_limit=target,
                )
                if ok:
                    state["positions"][key]["strategy_params"] = deepcopy(g)
                else:
                    diagnostics.append({"section": f"GRID {i + 1}", "reason": why})
        state["diagnostics"][symbol] = [
            {"section": "GRID", "reason": f"Якорь {anchor:g}; уровней {g['levels']}"}
        ] + diagnostics


class PumpDump:
    def process(self, cfg, state, events, symbol, bars, bid, ask, now_ms):
        settings = effective(cfg, symbol)
        params = merge(
            cfg["pump"], cfg["coin_overrides"].get(symbol, {}).get("parameters", {})
        )
        runtime = state["strategy"].setdefault(symbol, {})
        for key, p in list(state["positions"].items()):
            if p["symbol"] != symbol:
                continue
            # Do not replace a persisted manual, liquidation or partial exit.
            if p.get("exit_pending") or is_liquidating(p):
                continue
            mode = p.get("strategy_params", params[p["section"]])
            quote = bid if p["side"] == "LONG" else ask
            gain = 100 * (quote / p["average"] - 1) * (1 if p["side"] == "LONG" else -1)
            if (
                gain >= mode["tp_pct"]
                or gain <= -mode["sl_pct"]
                or now_ms / 1000 - p["opened"] >= mode["max_hold_seconds"]
            ):
                exit_position(
                    state,
                    events,
                    key,
                    fill_price(p["side"], "EXIT", bid, ask, p["settings"]),
                    "PUMP_EXIT",
                )
                record_completed_exit(cfg, state, key, p, now_ms)
        closed = [b for b in bars if b["t"] + 60000 <= now_ms]
        n = params["window_minutes"]
        if len(closed) < n + 20:
            return
        change = 100 * (closed[-1]["close"] / closed[-n]["open"] - 1)
        volume = sum(b["volume"] for b in closed[-n:]) / n
        baseline = sum(b["volume"] for b in closed[-n - 20 : -n]) / 20
        relative = volume / baseline if baseline else 0
        if abs(change) >= params["move_pct"] and relative >= params["volume_ratio"]:
            impulse_side = "SHORT" if change > 0 else "LONG"
            old = runtime.get("impulse")
            if (
                old is None
                or old["side"] != impulse_side
                or old["bar"] != closed[-1]["t"]
            ):
                runtime["impulse"] = dict(
                    side=impulse_side,
                    extreme=bid if impulse_side == "SHORT" else ask,
                    at=now_ms / 1000,
                    bar=closed[-1]["t"],
                    fired=[],
                )
        impulse = runtime.get("impulse")
        why = "Ожидание импульса"
        if impulse and now_ms / 1000 - impulse["at"] <= 240:
            short = impulse["side"] == "SHORT"
            price = bid if short else ask
            impulse["extreme"] = (
                max(impulse["extreme"], price)
                if short
                else min(impulse["extreme"], price)
            )
            rebound = (
                100 * (1 - price / impulse["extreme"])
                if short
                else 100 * (price / impulse["extreme"] - 1)
            )
            why = f"Импульс {change:.2f}%; объём {relative:.2f}×; отскок {rebound:.2f}%"
            for mode in params["modes"]:
                key = f"{symbol}:{mode}"
                if (
                    mode in impulse["fired"]
                    or key in state["positions"]
                    or impulse["side"] not in params["sides"]
                    or settings["disabled"]
                    or state["done"].get(key)
                    and settings["stop_after_cycle"]
                ):
                    continue
                if (
                    now_ms / 1000 - runtime.get("cooldowns", {}).get(mode, 0)
                    < params["cooldown_seconds"]
                ):
                    continue
                if rebound >= params[mode]["rebound_pct"] and daily_ok(
                    cfg, state, symbol, settings, now_ms
                ):
                    ok, why_entry = enter(
                        state,
                        events,
                        key,
                        symbol,
                        impulse["side"],
                        settings,
                        fill_price(impulse["side"], "ENTRY", bid, ask, settings),
                        params["stake_usdt"] / settings["leverage"],
                        [{"price": price, "weight": 1}],
                        mode,
                    )
                    if ok:
                        impulse["fired"].append(mode)
                        state["positions"][key]["strategy_params"] = deepcopy(
                            params[mode]
                        )
                    else:
                        why = why_entry
        state["diagnostics"][symbol] = [{"section": "PUMP&DUMP", "reason": why}]


ENGINES = {
    "VELES": Veles,
    "GRID": Grid,
    "PUMP_DUMP": PumpDump,
    "EMA": Veles,
    "MACD": Veles,
    "BOLLINGER": Veles,
    "EMA_RSI_VOLUME": Veles,
}
