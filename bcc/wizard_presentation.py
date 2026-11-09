"""Pure, UI-only presentation helpers for the functional Modern Qt wizard.

Trading decisions never depend on this module. Values displayed here are
read from the exact configuration being saved, not a duplicate set of inputs.
"""

from html import escape

from .config import STRATEGY_NAMES, resolve_poly_market, validate_poly_rule, effective
from .coin_allocation import nominal_budget
from .section_hierarchy import grouped_ranges


def summary_rows(config):
    """Return human-readable facts about a draft without implying live execution."""
    kind = config["strategy"]
    symbols = config["symbols"]
    rows = [
        ("Название", config.get("name") or "Новый бот"),
        ("Режим", "PAPER"),
        ("Стратегия", STRATEGY_NAMES[kind]),
        ("Монеты", ", ".join(symbols[:4]) + (f" +{len(symbols)-4}" if len(symbols) > 4 else "") or "Не выбраны"),
        ("Депозит", f"{config['capital']:g} USDT"),
    ]
    if kind == "POLYMARKET":
        p = config["polymarket"]
        seconds = p["entry_seconds_before_close"]
        when = (f"За {seconds:g} сек до закрытия"
                if p["entry_signal_mode"] == "before_close" else "В любое время")
        rows.extend([
            ("Настроено (не действует)", f"{seconds:g} сек")
            if p["entry_signal_mode"] == "anytime" else ("Режим времени", "Запланированный вход"),
            ("Рынки", ", ".join(f"{x // 60} мин" if x < 3600 else "1 час" for x in p["durations"])),
            ("Ставка", f"{p['stake_pct']:g}%"),
            ("ASK", f"{p['ask_min']:g} — {p['ask_max']:g}"),
            ("Импульс", f"{p['impulse']:g} {'USD' if p['impulse_mode'] == 'usd' else '%'}"),
            ("RSI/EMA/объём", "Фильтр И/ИЛИ активен" if p.get("indicator_filter") is not None else "Выключен"),
            ("Время входа", when),
            ("Окно входа", f"{p['entry_window_seconds']:g} сек"),
            ("Защитный порог", f"{p['entry_min_seconds_left']:g} сек"),
            ("Комиссия", "Публичная формула" if p['fee_mode'] == 'public' else "Своя оценка"),
        ])
    elif kind in ("VELES", "EMA", "MACD", "BOLLINGER", "EMA_RSI_VOLUME"):
        s = config["settings"]
        rows.extend([
            ("Маржа", "Cross" if s["margin_mode"] == "CROSS" else "Isolated"),
            ("Плечо", f"{s['leverage']:g}×"),
        ])
        if kind == "VELES":
            mode = s.get("allocation_mode", "section_pct")
            amount = (f"{s['allocation_usdt']:g} USDT на монету"
                      if mode == "coin_fixed" else
                      f"{s['allocation_pct']:g}% депозита на монету"
                      if mode == "coin_pct" else
                      f"{s['allocation_pct']:g}% депозита на секцию")
            rows.append(("Бюджет", amount))
            # The tree groups flat execution rows under author-visible parents.
            rows.extend([
                ("Секции", str(len(grouped_ranges(config["sections"])))),
                ("Диапазоны RSI", str(len(config["sections"]))),
            ])
        else:
            rows.append(("Секции", str(len(config["sections"]))))
        rows.extend([
            ("Ордера DCA", str(s["orders"]["count"])),
            ("Перекрытие", f"{s['orders']['coverage_pct']:g}%"),
            ("Мартингейл", f"{s['orders']['martingale_pct']:g}%"),
        ])
    elif kind == "GRID":
        g = config["grid"]
        rows.extend([("Сторона", g["side"]), ("Уровни", str(g["levels"])),
                     ("Шаг", f"{g['spacing_pct']:g}%")])
    elif kind == "PUMP_DUMP":
        p = config["pump"]
        rows.extend([("Импульс", f"{p['move_pct']:g}%"),
                     ("Объём", f"{p['volume_ratio']:g}×")])
    return rows


def summary_html(config, *, light=False):
    """Escaped, passive HTML for the summary rail; no UI data becomes executable."""
    rows = summary_rows(config)
    chunks = []
    muted = "#49647a" if light else "#a1b6cf"
    bright = "#19344b" if light else "#ecf5ff"
    accent = "#00786e" if light else "#36e2d0"
    border = "#cadbe7" if light else "#274058"
    for label, value in rows:
        value_color = accent if label == "Время входа" else bright
        chunks.append(
            "<tr><td style='color:" + muted + ";border-bottom:1px solid " + border + ";"
            "padding:7px 3px;vertical-align:top'>" + escape(str(label)) + "</td>"
            "<td style='color:" + value_color + ";border-bottom:1px solid " + border + ";"
            "padding:7px 3px;text-align:right;vertical-align:top'>" + escape(str(value)) + "</td></tr>"
        )
    return "<table cellspacing='0' cellpadding='0' width='100%'>" + "".join(chunks) + "</table>"


def market_preview_rows(config, symbol=None, duration=None):
    """Explain saved PAPER eligibility rules, never pretend to observe a live signal.

    This preview uses resolve_poly_market and validate_poly_rule from the same
    contract as the worker. It is not a trading trigger or a quote source.
    """
    if config["strategy"] != "POLYMARKET":
        return []
    symbols = config.get("symbols", [])
    durations = config["polymarket"].get("durations", [])
    if not symbols or not durations:
        return [("Рынок", "Выберите монету и длительность свечи")]
    if symbol not in symbols:
        symbol = symbols[0]
    if duration not in durations:
        duration = durations[0]
    rule = resolve_poly_market(config, symbol, duration)
    unit = "USD" if rule["impulse_mode"] == "usd" else "%"
    if rule["entry_signal_mode"] == "before_close":
        hi = rule["entry_seconds_before_close"]
        lo = (hi - rule["entry_window_seconds"] if rule["entry_window_seconds"]
              else rule["entry_min_seconds_left"])
        timing = f"от {hi:g} до {lo:g} сек до закрытия"
    else:
        timing = "В любое время до защитного порога; без расписания"
    rows = [
        ("Рынок", f"{symbol} · {duration:g} сек"),
        ("Направление", "UP / DOWN — по знаку импульса"),
        ("Условие цены", f"Импульс от {rule['impulse']:g} {unit}"),
        ("Условие ASK", f"{rule['ask_min']:g} — {rule['ask_max']:g}"),
        ("Фильтр индикаторов", "И/ИЛИ по закрытым свечам Binance"
         if rule.get("indicator_filter") is not None else "Не применяется"),
        ("Время", timing),
        ("Защитный порог", f"не ближе {rule['entry_min_seconds_left']:g} сек"),
        ("Ставка", f"{rule['stake_pct']:g}% депозита"),
        ("Состояние", "Рынок отключён" if rule["disabled"] else "PAPER, без реальных ордеров"),
    ]
    try:
        validate_poly_rule(rule, duration)
    except (ValueError, KeyError, TypeError) as exc:
        rows.append(("Проверка правила", "Ошибка: " + str(exc)))
    else:
        rows.append(("Проверка правила", "Параметры допустимы; рыночный сигнал не проверялся"))
    return rows


def market_preview_html(config, symbol=None, duration=None, *, light=False):
    """Safe HTML view of the effective market policy for the center panel."""
    rows = market_preview_rows(config, symbol, duration)
    fg = "#17364b" if light else "#dceefb"
    dim = "#516a81" if light else "#a9c4d9"
    border = "#cadbe7" if light else "#29475c"
    return "<table width='100%' cellspacing='0' cellpadding='0'>" + "".join(
        "<tr><td style='color:" + dim + ";border-bottom:1px solid " + border
        + ";padding:7px 5px'>" + escape(str(label)) + "</td><td style='color:" + fg
        + ";border-bottom:1px solid " + border + ";padding:7px 5px;text-align:right'>"
        + escape(str(value)) + "</td></tr>" for label,value in rows
    ) + "</table>"


def grid_preview_data(config, symbol, section_id):
    """Resolve one selected VELES grid without mutating the publishable draft.

    The interactive selection is intentionally *not* part of the constructor
    change audit.  The preview model is UI-free and uses the same effective
    coin/section policy as execution.  None means that the selection is no
    longer present in the current draft (for example after deleting a coin).
    """
    from .config import coin_section, effective
    from .trading import percent_stop_price, plan

    if symbol not in config["symbols"]:
        return None
    original = next(
        (section for section in config["sections"] if section["id"] == section_id),
        None,
    )
    if original is None:
        return None

    section = coin_section(config, symbol, original)
    settings = effective(config, symbol, original)
    planned = plan(settings["orders"], section["side"], 100)
    coin_settings = effective(config, symbol)
    is_coin_budget = coin_settings.get("allocation_mode") != "section_pct"
    margin = nominal_budget(config, coin_settings if is_coin_budget else settings)
    budget = margin * settings["leverage"]
    rows = tuple(
        (
            str(i + 1),
            f"{row['price']:.8g}",
            f"{row['weight'] * 100:.5g}",
            f"{budget * row['weight']:.4f}",
        )
        for i, row in enumerate(planned)
    )
    status = (
        f"{symbol} · {section['label']} · {section['side']}\n"
        f"{'Максимальная маржа на монету' if is_coin_budget else 'Маржа на секцию'}: "
        f"{margin:g} USDT; "
        f"номинал сетки с плечом: {budget:g} USDT."
    )
    if settings["stop"]["mode"] in ("percent", "both"):
        if settings["orders"]["mode"] == "signal":
            status += "\nSL рассчитывается после фактического последнего усреднения."
        else:
            stop = percent_stop_price(
                {"settings": settings, "side": section["side"], "orders": planned}
            )
            status += f"\nУсловный SL: {stop:.8g} после исполнения сетки."
    if not section["enabled"] or settings["disabled"]:
        status += "\nНовые входы этой секции отключены."
    return {"rows": rows, "status": status}
