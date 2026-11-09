"""Side-effect-free effective-rule review for the Modern Qt constructors.

Compare what the worker would receive after save, not merely the raw override
JSON.  The review is passive: it does not normalize/mutate drafts, access a
repository, start workers, or submit PAPER/real orders.
"""
from .config import coin_section, effective, resolve_poly_market
from .coin_allocation import nominal_budget

# All persisted effective fields belong to an explicit review category. The
# UI must not show "no entry changes" when a user changes entry mode/window
# without changing the numeric seconds field.
POLY_REVIEW_FIELDS = {
    "entry_policy_changed": frozenset((
        "entry_signal_mode", "entry_seconds_before_close",
        "entry_window_seconds", "entry_min_seconds_left")),
    "impulse_changed": frozenset(("impulse_mode", "impulse")),
    "ask_changed": frozenset(("ask_min", "ask_max")),
    "stake_changed": frozenset(("stake_pct",)),
    "fees_changed": frozenset(("fee_mode", "fee_rate")),
    "availability_changed": frozenset(("disabled",)),
    "signals_changed": frozenset(("indicator_filter",)),
}
VELES_REVIEW_FIELDS = {
    "risk_changed": frozenset((
        "leverage", "margin_mode", "allocation_pct", "allocation_mode",
        "allocation_usdt", "reinvest_pct",
        "daily_loss_pct", "disabled", "stop_after_cycle")),
    "execution_changed": frozenset((
        "fee_rate", "slippage_pct", "execution_policy")),
    "orders_changed": frozenset(("orders",)),
    "exits_changed": frozenset(("profit", "stop")),
}


def _changed_categories(before, after, categories):
    return [name for name, fields in categories.items()
            if any(before.get(field) != after.get(field) for field in fields)]


def _check_review_schema(categories, fields):
    """Fail closed when a future trading field has no visible review category."""
    counted = [field for group in categories.values() for field in group]
    if len(counted) != len(set(counted)) or set(counted) != set(fields):
        raise ValueError("Неполный перечень полей проверки изменений конструктора")



def constructor_change_summary(original, draft, *, sample_limit=8):
    """Compare two VELES/Polymarket drafts, including inherited rules.

    Only combinations present in both drafts are compared for modifications.
    Newly added and removed combinations are reported separately.  This
    prevents an inherited per-coin entry cutoff from being overlooked when a
    global setting changes. No full matrix of resolved rules is allocated.
    """
    if not isinstance(original, dict) or not isinstance(draft, dict):
        raise ValueError("Ожидаются две конфигурации")
    strategy = original.get("strategy")
    if strategy != draft.get("strategy") or strategy not in ("VELES", "POLYMARKET"):
        raise ValueError("Можно сравнить только одну стратегию VELES/Polymarket")
    if type(sample_limit) is not int or not 0 <= sample_limit <= 100:
        raise ValueError("Неверный предел примеров")

    from .config import defaults, poly_rule_keys
    if strategy == "POLYMARKET":
        _check_review_schema(POLY_REVIEW_FIELDS, poly_rule_keys())
    else:
        _check_review_schema(VELES_REVIEW_FIELDS, defaults("VELES")["settings"])

    common_fields = ("name", "capital", "settings", "reinvest", "stop_after_deals")
    changed_general = [key for key in common_fields
                       if original.get(key) != draft.get(key)]
    old_symbols, new_symbols = set(original["symbols"]), set(draft["symbols"])
    coins_with_changed_overrides = sum(
        original.get("coin_overrides", {}).get(symbol) !=
        draft.get("coin_overrides", {}).get(symbol)
        for symbol in old_symbols | new_symbols
    )
    result = {
        "strategy": strategy,
        "general_changed": changed_general,
        "coin_overrides_changed": coins_with_changed_overrides,
        "combinations_added": 0,
        "combinations_removed": 0,
        "combinations_modified": 0,
        "entry_seconds_changed": 0,
        "signals_changed": 0,
        "entry_policy_changed": 0,
        "impulse_changed": 0,
        "ask_changed": 0,
        "stake_changed": 0,
        "fees_changed": 0,
        "availability_changed": 0,
        "risk_changed": 0,
        "execution_changed": 0,
        "orders_changed": 0,
        "exits_changed": 0,
        "section_state_changed": 0,
        "samples": [],
    }

    if strategy == "POLYMARKET":
        old_poly, new_poly = original["polymarket"], draft["polymarket"]
        old_durations = set(old_poly["durations"])
        new_durations = set(new_poly["durations"])
        old_base = {key: value for key, value in old_poly.items()
                    if key not in ("durations", "overrides")}
        new_base = {key: value for key, value in new_poly.items()
                    if key not in ("durations", "overrides")}
        if old_base != new_base:
            changed_general.append("polymarket")
        result["combinations_added"] = (len(new_symbols) * len(new_durations) -
            len(old_symbols & new_symbols) * len(old_durations & new_durations))
        result["combinations_removed"] = (len(old_symbols) * len(old_durations) -
            len(old_symbols & new_symbols) * len(old_durations & new_durations))
        for symbol in sorted(old_symbols & new_symbols):
            for duration in sorted(old_durations & new_durations):
                old = resolve_poly_market(original, symbol, duration)
                new = resolve_poly_market(draft, symbol, duration)
                if old == new:
                    continue
                result["combinations_modified"] += 1
                if old["entry_seconds_before_close"] != new["entry_seconds_before_close"]:
                    result["entry_seconds_changed"] += 1
                for category in _changed_categories(old, new, POLY_REVIEW_FIELDS):
                    result[category] += 1
                if len(result["samples"]) < sample_limit:
                    result["samples"].append(f"{symbol}:{duration}")
    else:
        old_sections = {section["id"]: section for section in original["sections"]}
        new_sections = {section["id"]: section for section in draft["sections"]}
        old_ids, new_ids = set(old_sections), set(new_sections)
        if original["sections"] != draft["sections"]:
            changed_general.append("sections")
        shared = len(old_symbols & new_symbols) * len(old_ids & new_ids)
        result["combinations_added"] = len(new_symbols) * len(new_ids) - shared
        result["combinations_removed"] = len(old_symbols) * len(old_ids) - shared
        for symbol in sorted(old_symbols & new_symbols):
            for section_id in sorted(old_ids & new_ids):
                old_section = coin_section(original, symbol, old_sections[section_id])
                new_section = coin_section(draft, symbol, new_sections[section_id])
                old_settings = effective(original, symbol, old_sections[section_id])
                new_settings = effective(draft, symbol, new_sections[section_id])
                # Raw section overrides are not effective policy: a per-coin
                # override can shadow them. Compare resolved behavior only.
                old_section.pop("overrides", None)
                new_section.pop("overrides", None)
                if old_section == new_section and old_settings == new_settings:
                    continue
                result["combinations_modified"] += 1
                if any(old_section[field] != new_section[field]
                       for field in ("entry", "exit")):
                    result["signals_changed"] += 1
                if any(old_section.get(field) != new_section.get(field)
                       for field in set(old_section) | set(new_section)
                       if field not in ("entry", "exit")):
                    result["section_state_changed"] += 1
                for category in _changed_categories(
                        old_settings, new_settings, VELES_REVIEW_FIELDS):
                    result[category] += 1
                if len(result["samples"]) < sample_limit:
                    result["samples"].append(f"{symbol}/{section_id}")
    return result


def constructor_change_review_text(original, draft):
    """Compact Russian-language preview of *effective* changes before save."""
    info = constructor_change_summary(original, draft)
    unit = "рынков" if info["strategy"] == "POLYMARKET" else "диапазонов"
    rows = [
        "Изменения относительно открытой конфигурации (без применения):",
        f"Добавлено {unit}: {info['combinations_added']}; "
        f"удалено: {info['combinations_removed']}; "
        f"изменено: {info['combinations_modified']}.",
        f"Изменены индивидуальные настройки монет: {info['coin_overrides_changed']}.",
    ]
    if info["strategy"] == "POLYMARKET":
        rows.append(f"Изменён вход за N секунд: {info['entry_seconds_changed']} рынков.")
        rows.append(f"Изменены правила времени входа: {info['entry_policy_changed']} рынков.")
        rows.append(f"Импульс: {info['impulse_changed']}; ASK: {info['ask_changed']}; "
                    f"ставка: {info['stake_changed']}; комиссия: {info['fees_changed']}; "
                    f"блокировка входа: {info['availability_changed']} рынков.")
        rows.append(f"Изменены индикаторные фильтры: {info['signals_changed']} рынков.")
    else:
        rows.append(f"Изменены сигналы входа/выхода: {info['signals_changed']} диапазонов.")
        rows.append(f"Риск и маржа: {info['risk_changed']}; исполнение: "
                    f"{info['execution_changed']}; DCA/ордера: {info['orders_changed']}; "
                    f"выходы/стопы: {info['exits_changed']}; "
                    f"состояние секций: {info['section_state_changed']} диапазонов.")
    if info["general_changed"]:
        rows.append("Изменены общие поля: " + ", ".join(info["general_changed"]))
    if info["samples"]:
        rows.append("Примеры: " + ", ".join(info["samples"]))
    return "\n".join(rows)


def effective_risk_review_text(draft, *, sample_limit=8):
    """Stable, bounded review of resolved VELES risk across selected combinations.

    Unlike the interactive grid preview, this report is a pure function of the
    publishable draft. It cannot change when the operator browses a different
    coin or section. The limit bounds *display*, never the bot's capacity.
    """
    if draft.get("strategy") != "VELES":
        raise ValueError("Эффективный риск секций доступен только для VELES")
    if type(sample_limit) is not int or not 0 <= sample_limit <= 100:
        raise ValueError("Неверный предел примеров риска")
    symbols = sorted(set(draft["symbols"]))
    sections = sorted(draft["sections"], key=lambda item: item["id"])
    total = len(symbols) * len(sections)
    lines = [f"Примеры эффективных параметров риска: до {sample_limit} из {total} диапазонов."]
    shown = 0
    for symbol in symbols:
        for section in sections:
            if shown >= sample_limit:
                break
            resolved = coin_section(draft, symbol, section)
            settings = effective(draft, symbol, section)
            policy = effective(draft, symbol)
            margin = (nominal_budget(draft, policy) if
                      policy["allocation_mode"] != "section_pct" else
                      nominal_budget(draft, settings))
            notional = margin * settings["leverage"]
            stop = settings["stop"]
            stop_text = (f"{stop['pct']:g}%"
                         if stop["mode"] in ("percent", "both")
                         else stop["mode"])
            admission = ("вход отключён" if not resolved["enabled"] or settings["disabled"]
                         else "вход разрешён")
            lines.append(
                f"{symbol} / {resolved['label']}: {resolved['side']}, "
                f"{settings['margin_mode']}, плечо {settings['leverage']:g}x; "
                f"условная маржа {margin:g} USDT; номинал {notional:g} USDT; "
                f"SL {stop_text}; {admission}."
            )
            shown += 1
        if shown >= sample_limit:
            break
    if total > shown:
        lines.append(f"Остальные {total - shown} диапазонов доступны в отдельных настройках.")
    return "\n".join(lines)


def constructor_review_text(original, draft, *, dropped_overrides=(),
                            include_change_audit=False):
    """Build a selector-independent review from persisted configuration only.

    The interactive grid preview belongs to a separate widget. Mixing its
    selected coin/section into this audit made review text depend on navigation
    and could hide a different combination's risk.
    """
    rows = [
        "Проверьте параметры и сохраните бота. Он появится в MY BOTS "
        "и будет готов к отдельному запуску."
    ]
    if dropped_overrides:
        excluded = ", ".join(dropped_overrides[:8])
        if len(dropped_overrides) > 8:
            excluded += f" (и ещё {len(dropped_overrides) - 8})"
        rows.append("При сохранении будут исключены настройки удалённых "
                    "монет, рынков и секций: " + excluded)
    if include_change_audit and draft["strategy"] in ("VELES", "POLYMARKET"):
        rows.append(constructor_change_review_text(original, draft))
        if draft["strategy"] == "VELES":
            rows.append(effective_risk_review_text(draft))
    return "\n\n".join(rows)
