"""One configuration contract for the editor, worker, and file operations."""

import math
import re
from copy import deepcopy

TIMEFRAMES = (1, 2, 5, 10, 15, 30)
STRATEGIES = (
    "VELES",
    "POLYMARKET",
    "PUMP_DUMP",
    "GRID",
    "EMA",
    "MACD",
    "BOLLINGER",
    "EMA_RSI_VOLUME",
)

STRATEGY_NAMES = {
    "VELES": "VELES",
    "POLYMARKET": "Polymarket",
    "PUMP_DUMP": "PUMP&DUMP",
    "GRID": "GRID",
    "EMA": "EMA",
    "MACD": "MACD",
    "BOLLINGER": "Bollinger Bands",
    "EMA_RSI_VOLUME": "EMA + RSI + Volume",
}


def is_secret_key(key):
    normalized = re.sub(r"[^a-z]", "", str(key).lower())
    # A PAPER export may contain fields added by external bots or imported
    # formats. Exact-name checks miss namespaced secrets such as
    # ``polymarket_relayer_api_key`` and ``walletPrivateKey``.
    # Do not reject bare ``token``: Polymarket outcome token IDs are PUBLIC.
    sensitive_endings = (
        "apikey", "apisecret", "clientsecret", "secretkey", "secret",
        "privatekey", "signingkey", "accesstoken", "authtoken",
        "refreshtoken", "bearertoken", "password", "passphrase",
        "mnemonic", "seedphrase",
    )
    return normalized == "credentials" or normalized.endswith(sensitive_endings)


def valid_symbol(symbol, strategy):
    if not isinstance(symbol, str):
        return False
    if strategy == "POLYMARKET":
        return re.fullmatch(r"[A-Z0-9]{1,24}", symbol) is not None
    if not symbol.endswith("USDT") or symbol != symbol.upper():
        return False
    base = symbol[:-4]
    return 1 <= len(base) <= 64 and base.isalnum()


def reject_private_data(value, depth=0):
    if depth > 40:
        raise ValueError("Слишком глубокая структура параметров")
    if isinstance(value, dict):
        for key, child in value.items():
            if is_secret_key(key):
                raise ValueError("PAPER-конфигурация не должна содержать ключи доступа")
            reject_private_data(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            reject_private_data(child, depth + 1)


def normalize_config(cfg):
    cfg = deepcopy(cfg)
    if isinstance(cfg, dict):
        cfg.setdefault("stop_after_deals", False)
    if isinstance(cfg, dict) and isinstance(cfg.get("settings"), dict):
        cfg["settings"].setdefault("reinvest_pct", 20.0)
        if isinstance(cfg["settings"].get("stop"), dict):
            cfg["settings"]["stop"].setdefault("stop_after", False)
    if isinstance(cfg, dict) and cfg.get("strategy") == "POLYMARKET":
        if isinstance(cfg.get("polymarket"), dict):
            cfg["polymarket"].setdefault("indicator_filter", None)
        _upgrade_poly_config(cfg)
    return cfg


def number(v, lo, hi, label, integer=False):
    if (
        type(v) not in (int, float)
        or not math.isfinite(v)
        or not lo <= v <= hi
        or integer
        and type(v) is not int
    ):
        raise ValueError(f"{label}: допустимо {lo}…{hi}")
    return v


def condition(indicator="RSI", value=30, comparison="cross_up", timeframe=2):
    return dict(
        indicator=indicator,
        period=14,
        timeframe=timeframe,
        trigger="bar_close",
        comparison=comparison,
        value=value,
        reference="number",
        output="value",
        params={},
    )


def group(*conditions):
    return {"operator": "AND", "conditions": list(conditions)}


def range_section(start=30, finish=40, side="LONG", entry_tf=2, exit_tf=1):
    direction = "cross_up" if side == "LONG" else "cross_down"
    return {
        "id": f"{side}_{start:g}_{finish:g}",
        "label": f"{side} RSI {start:g}–{finish:g}",
        "enabled": True,
        "side": side,
        "entry": group(
            condition(value=start, comparison=direction, timeframe=entry_tf)
        ),
        "exit": group(condition(value=finish, comparison=direction, timeframe=exit_tf)),
        "overrides": {},
    }


def defaults(strategy="VELES"):
    d = dict(
        schema=1,
        strategy=strategy,
        name="",
        mode="PAPER",
        exchange="POLYMARKET" if strategy == "POLYMARKET" else "BYBIT_LINEAR",
        symbols=["BTCUSDT", "ETHUSDT"],
        capital=500.0,
        reinvest=False,
        stop_after_deals=False,
        settings=dict(
            leverage=3.0,
            margin_mode="CROSS",
            allocation_pct=5.0,
            reinvest_pct=20.0,
            fee_rate=0.0006,
            slippage_pct=0.02,
            execution_policy="ioc",
            daily_loss_pct=100.0,
            disabled=False,
            orders=dict(
                mode="simple",
                count=10,
                coverage_pct=10.0,
                martingale_pct=5.0,
                first_offset_pct=0.0,
                logarithmic=1.2,
                partial_count=3,
                pull_up_pct=0.4,
                custom=[{"offset_pct": 0.0, "volume_pct": 100.0}],
                signal=group(condition()),
                signal_spacing_pct=0.5,
                signal_reference="last",
            ),
            profit=dict(
                mode="signal",
                pct=1.0,
                minimum_pct=None,
                targets=[
                    {"pct": 1.0, "volume_pct": 50.0},
                    {"pct": 2.0, "volume_pct": 50.0},
                ],
                breakeven=False,
                breakeven_reference="average",
                breakeven_offset_pct=0.0,
                trailing=False,
                activation_pct=1.0,
                distance_pct=0.5,
                currency="USDT",
            ),
            stop=dict(
                mode="none",
                stop_after=False,
                pct=3.0,
                reference="last",
                minimum_pct=None,
                signal=group(condition(value=20, comparison="<", timeframe=1)),
            ),
            stop_after_cycle=False,
        ),
        sections=[range_section(30, 40), range_section(40, 50)],
        coin_overrides={},
        grid=dict(
            levels=10,
            spacing_pct=0.5,
            take_profit_pct=0.7,
            stop_loss_pct=3.0,
            side="LONG",
            order_usdt=20.0,
        ),
        pump=dict(
            window_minutes=3,
            move_pct=4.0,
            volume_ratio=3.0,
            modes=["fast", "confirmed"],
            sides=["LONG", "SHORT"],
            cooldown_seconds=300,
            stake_usdt=25.0,
            fast=dict(rebound_pct=0.2, tp_pct=0.8, sl_pct=1.0, max_hold_seconds=1200),
            confirmed=dict(
                rebound_pct=1.0, tp_pct=2.0, sl_pct=2.0, max_hold_seconds=3600
            ),
        ),
        polymarket=dict(
            durations=[300, 900, 3600],
            impulse_mode="percent",
            impulse=0.1,
            entry_signal_mode="anytime",
            entry_seconds_before_close=30,
            entry_window_seconds=25,
            entry_min_seconds_left=5,
            ask_min=0.1,
            ask_max=0.8,
            stake_pct=5.0,
            fee_rate=0.0,
            fee_mode="public",
            indicator_filter=None,
            disabled=False,
            overrides={},
        ),
    )
    if strategy == "POLYMARKET":
        d["symbols"] = ["BTC", "ETH"]
    if strategy not in ("VELES", "POLYMARKET", "PUMP_DUMP", "GRID"):
        d["sections"] = [range_section()]
        sec = d["sections"][0]
        sec["id"] = "main"
        sec["label"] = strategy
        if strategy == "EMA":
            sec["entry"] = group(
                dict(
                    condition("EMA", 0, "cross_up", 5),
                    reference="indicator",
                    reference_period=26,
                    period=12,
                )
            )
            sec["exit"] = group(
                dict(
                    condition("EMA", 0, "cross_down", 5),
                    reference="indicator",
                    reference_period=26,
                    period=12,
                )
            )
        elif strategy == "MACD":
            sec["entry"] = group(condition("MACD", 0, "cross_up", 5))
            sec["exit"] = group(condition("MACD", 0, "cross_down", 5))
        elif strategy == "BOLLINGER":
            sec["entry"] = group(condition("BB_PERCENT", 0, "<", 5))
            sec["exit"] = group(condition("BB_PERCENT", 1, ">", 5))
        else:
            sec["entry"] = group(
                dict(condition("EMA", 0, "<", 5), reference="price"),
                condition("RSI", 40, "cross_up", 2),
                condition("REL_VOLUME", 3, ">", 1),
            )
            sec["exit"] = group(condition("RSI", 60, "cross_down", 1))
    return d


def merge(base, patch):
    result = deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = merge(result[k], v)
        else:
            result[k] = deepcopy(v)
    return result


def difference(base, value):
    """Store only overrides so later common changes continue to be inherited."""
    result = {}
    for key, v in value.items():
        if isinstance(v, dict) and isinstance(base.get(key), dict):
            nested = difference(base[key], v)
            if nested:
                result[key] = nested
        elif key not in base or base[key] != v:
            result[key] = deepcopy(v)
    return result


def merge_poly_rule(base, patch):
    """Merge policy fields while replacing an indicator tree as one value.

    Generic ``merge`` is appropriate for monetary settings, but NOT for
    executable condition trees. A market/coin editor can remove a nested
    ``groups`` branch: recursive dict merging would resurrect that deleted
    branch and silently change PAPER entry eligibility after saving.
    """
    result = merge(base, {key: value for key, value in patch.items()
                          if key != "indicator_filter"})
    if "indicator_filter" in patch:
        result["indicator_filter"] = deepcopy(patch["indicator_filter"])
    return result


def difference_poly_rule(base, value):
    """Minimal inherited policy patch with lossless condition-tree replace."""
    result = difference(base, {key: val for key, val in value.items()
                               if key != "indicator_filter"})
    if "indicator_filter" in value and value["indicator_filter"] != base.get("indicator_filter"):
        result["indicator_filter"] = deepcopy(value["indicator_filter"])
    return result




# A single immutable contract for editor, validator and PAPER runtime.
# Existing bots may still have the v17 legacy timing keys; upgrading them does
# not shift their actual entry window or rewrite their disk files during reads.
_POLY_OLD = {"timing", "seconds_before_close", "min_seconds_left"}
_POLY_TIMING = (
    "entry_signal_mode", "entry_seconds_before_close",
    "entry_window_seconds", "entry_min_seconds_left",
)


def _upgrade_poly_rule(parent, patch):
    patch = deepcopy(patch)
    rule = merge_poly_rule(parent, {k: v for k, v in patch.items() if k not in _POLY_OLD})
    for old, new in (
        ("timing", "entry_signal_mode"),
        ("seconds_before_close", "entry_seconds_before_close"),
        ("min_seconds_left", "entry_min_seconds_left"),
    ):
        if old in patch and new in patch and patch[old] != patch[new]:
            raise ValueError("Противоречивые параметры Polymarket: " + old + " / " + new)
        if old in patch and new not in patch:
            rule[new] = patch[old]
    if ("entry_window_seconds" not in patch and
        ("seconds_before_close" in patch or "min_seconds_left" in patch)):
        # The old schedule was [cutoff, T-N]; preserve both endpoints.
        rule["entry_window_seconds"] = (
            rule["entry_seconds_before_close"] - rule["entry_min_seconds_left"]
        )
    return rule


def _upgrade_poly_config(cfg):
    original = cfg.get("polymarket")
    if not isinstance(original, dict):
        return
    canonical = set(_POLY_TIMING).intersection(original)
    if canonical and canonical != set(_POLY_TIMING) and not _POLY_OLD.intersection(original):
        raise ValueError("Отсутствуют обязательные параметры времени Polymarket")
    coin_overrides = cfg.get("coin_overrides", {})
    market_overrides = original.get("overrides", {})
    if not isinstance(coin_overrides, dict) or not isinstance(market_overrides, dict):
        return
    patches = [original] + [
        x.get("parameters", {}) for x in coin_overrides.values() if isinstance(x, dict)
    ] + list(market_overrides.values())
    needs_upgrade = any(
        isinstance(v, dict) and bool(_POLY_OLD.intersection(v)) for v in patches
    ) or any(k not in original for k in _POLY_TIMING)
    if not needs_upgrade:
        return
    root = _upgrade_poly_rule(defaults("POLYMARKET")["polymarket"], {
        k: v for k, v in original.items() if k != "overrides"
    })
    root["overrides"] = {}
    for sym, item in coin_overrides.items():
        if not isinstance(item, dict) or not isinstance(item.get("parameters"), dict):
            continue
        upgraded = _upgrade_poly_rule(root, item["parameters"])
        item["parameters"] = difference_poly_rule(root, upgraded)
    for key, patch in market_overrides.items():
        if not isinstance(patch, dict):
            root["overrides"][key] = patch
            continue
        sym = str(key).split(":")[0]
        parent = merge_poly_rule(root, coin_overrides.get(sym, {}).get("parameters", {}))
        upgraded = _upgrade_poly_rule(parent, patch)
        root["overrides"][key] = difference_poly_rule(parent, upgraded)
    cfg["polymarket"] = root


def poly_rule_keys():
    """Single persisted Polymarket rule schema for all editor layers.

    Duration selection and per-market overrides are collections, not policy
    fields. The rest must be preserved through global, coin and market forms.
    """
    return frozenset(defaults("POLYMARKET")["polymarket"]) - {
        "durations", "overrides"
    }


def resolve_poly_market(cfg, symbol, duration):
    """Return only the effective PAPER rule for this coin and market.

    ``durations`` and ``overrides`` are constructor collections, never market
    policy. Copying the entire overrides matrix into every resolved rule made
    resolution quadratic in the number of configured markets and leaked other
    markets' edits into equality/roundtrip comparisons and worker payloads.
    """
    p = cfg["polymarket"]
    root = {key: value for key, value in p.items()
            if key not in ("durations", "overrides")}
    coin = cfg["coin_overrides"].get(symbol, {}).get("parameters", {})
    return merge_poly_rule(merge_poly_rule(root, coin),
                           p["overrides"].get(f"{symbol}:{duration}", {}))


def poly_window_allows(rule, start, end, low, high):
    """Allow an entry only when the ENTIRE trusted clock interval fits."""
    if not all(type(x) in (int, float) and math.isfinite(x) for x in
               (start, end, low, high)):
        return False
    if not start < end or not start <= low <= high < end:
        return False
    if high > end - rule["entry_min_seconds_left"]:
        return False
    if rule["entry_signal_mode"] == "anytime":
        return True
    if rule["entry_signal_mode"] != "before_close":
        return False
    before = rule["entry_seconds_before_close"]
    window = rule["entry_window_seconds"] or (before - rule["entry_min_seconds_left"])
    return low >= end - before and high <= end - before + window


def validate_poly_rule(rule, duration):
    for k, lo, hi in (
        ("impulse", 0.000001, 1e9),
        ("entry_seconds_before_close", 0, 3600),
        ("entry_window_seconds", 0, 3600),
        ("entry_min_seconds_left", 0, 3600),
        ("ask_min", 0.0001, 0.9999),
        ("ask_max", 0.0001, 0.9999),
        ("stake_pct", 0.001, 100),
        ("fee_rate", 0, 1),
    ):
        number(rule[k], lo, hi, k)
    if (rule["ask_min"] >= rule["ask_max"] or
        rule["entry_signal_mode"] not in ("anytime", "before_close") or
        rule["impulse_mode"] not in ("percent", "usd") or
        type(rule.get("disabled")) is not bool or
        rule.get("fee_mode") not in ("public", "estimate")):
        raise ValueError("Коридор ASK, комиссия или режим входа")
    if rule["entry_signal_mode"] == "before_close":
        before, window, cutoff = (rule[k] for k in (
            "entry_seconds_before_close", "entry_window_seconds",
            "entry_min_seconds_left"))
        if not (cutoff < before <= duration and (window == 0 or 0 < window <= before - cutoff)):
            raise ValueError(
                f"Вход за N секунд: для рынка {duration} с окно должно быть "
                "в пределах свечи и до защитного порога"
            )
    if rule["entry_min_seconds_left"] >= duration:
        raise ValueError("Защитный порог должен быть меньше длительности рынка")
    from .poly_indicators import validate_filter
    validate_filter(rule.get("indicator_filter"))

def effective(cfg, symbol, section=None):
    result = merge(
        cfg["settings"], cfg["coin_overrides"].get(symbol, {}).get("settings", {})
    )
    if section:
        result = merge(result, section.get("overrides", {}))
        result = merge(
            result,
            cfg["coin_overrides"]
            .get(symbol, {})
            .get("sections", {})
            .get(section["id"], {})
            .get("settings", {}),
        )
    return result


def coin_section(cfg, symbol, section):
    coin = cfg["coin_overrides"].get(symbol, {})
    patch = coin.get("sections", {}).get(section["id"], {})
    resolved = deepcopy(section)
    for key in ("label", "side", "enabled"):
        if key in patch:
            resolved[key] = patch[key]
    for key in ("entry", "exit"):
        resolved[key] = deepcopy(patch.get(key, coin.get(key, section[key])))
    return resolved


def validate_position_settings(settings, side):
    validate_settings(settings)
    if side == "LONG" and settings["stop"]["mode"] in ("percent", "both"):
        orders = settings["orders"]
        last = (
            orders["custom"][-1]["offset_pct"]
            if orders["mode"] == "custom"
            else orders["first_offset_pct"]
            + (orders["coverage_pct"] if orders["count"] > 1 else 0)
        )
        if last + settings["stop"]["pct"] >= 100:
            raise ValueError("Уровень стоп-лосса LONG должен оставаться выше нуля")


def validate_settings(s):
    base = defaults()["settings"]
    if set(s) - set(base):
        raise ValueError("Неизвестная настройка: " + ",".join(set(s) - set(base)))
    number(s["leverage"], 1, 100, "Плечо")
    number(s["allocation_pct"], 0.001, 100, "Доля капитала")
    number(s.get("reinvest_pct", 20.0), 0, 50, "Реинвестирование прибыли, %")
    number(s["fee_rate"], 0, 0.05, "Комиссия")
    number(s["slippage_pct"], 0, 5, "Проскальзывание")
    if s.get("execution_policy") not in ("ioc", "fok"):
        raise ValueError("Исполнение PAPER: IOC или FOK")
    number(s["daily_loss_pct"], 0.01, 100, "Дневной убыток")
    if s["margin_mode"] not in ("CROSS", "ISOLATED"):
        raise ValueError("Тип маржи")
    for key in ("disabled", "stop_after_cycle"):
        if type(s[key]) is not bool:
            raise ValueError(key + ": ожидается переключатель")
    o = s["orders"]
    if set(o) - set(base["orders"]):
        raise ValueError("Неизвестная настройка ордеров")
    if o["mode"] not in ("simple", "custom", "signal"):
        raise ValueError("Режим ордеров")
    number(o["count"], 1, 100, "Количество ордеров", True)
    number(o["partial_count"], 1, 100, "Частичное выставление", True)
    for key, lo, hi in [
        ("coverage_pct", 0.001, 90),
        ("martingale_pct", 0, 900),
        ("first_offset_pct", 0, 90),
        ("logarithmic", 0.1, 10),
        ("pull_up_pct", 0, 100),
        ("signal_spacing_pct", 0, 90),
    ]:
        number(o[key], lo, hi, key)
    if o["first_offset_pct"] + o["coverage_pct"] >= 100:
        raise ValueError("Сетка LONG должна оставаться выше нуля")
    if o["signal_reference"] not in ("last", "entry"):
        raise ValueError("Отсчёт отступа усреднения")
    validate_weights(o["custom"], "offset_pct", zero=True)
    validate_group(o["signal"])
    p = s["profit"]
    if set(p) - set(base["profit"]):
        raise ValueError("Неизвестная настройка тейк-профита")
    if p["mode"] not in ("simple", "custom", "signal"):
        raise ValueError("Режим тейк-профита")
    number(p["pct"], 0.001, 10000, "Тейк-профит")
    if p["minimum_pct"] is not None:
        number(p["minimum_pct"], -100, 10000, "Минимальный PnL")
    validate_weights(p["targets"], "pct")
    if p["breakeven_reference"] not in ("average", "take_profit"):
        raise ValueError("Отсчёт безубытка")
    number(p["breakeven_offset_pct"], -90, 10000, "Отклонение безубытка")
    for k in ("activation_pct", "distance_pct"):
        number(p[k], 0.001, 10000, k)
    for k in ("trailing", "breakeven"):
        if type(p[k]) is not bool:
            raise ValueError(k)
    if p["currency"] != "USDT":
        raise ValueError("Фьючерсный PAPER учитывает прибыль в USDT")
    st = s["stop"]
    if set(st) - set(base["stop"]):
        raise ValueError("Неизвестная настройка стоп-лосса")
    if st["mode"] not in ("none", "percent", "signal", "both"):
        raise ValueError("Режим стоп-лосса")
    if type(st.get("stop_after", False)) is not bool:
        raise ValueError("Остановка после стоп-лосса")
    number(st["pct"], 0.001, 90, "Стоп-лосс")
    if st["reference"] not in ("last", "average"):
        raise ValueError("Отсчёт стоп-лосса")
    if st["minimum_pct"] is not None:
        number(st["minimum_pct"], -90, 10000, "Отступ стоп-лосса")
    validate_group(st["signal"])


def validate_weights(rows, key, zero=False):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("Требуется 1…100 ордеров")
    last = -1
    for r in rows:
        number(r[key], 0 if zero else 0.001, 90 if zero else 10000, key)
        number(r["volume_pct"], 0.001, 100, "Объём ордера")
        if r[key] <= last:
            raise ValueError("Уровни ордеров должны возрастать")
        last = r[key]
    if abs(sum(r["volume_pct"] for r in rows) - 100) > 1e-6:
        raise ValueError("Сумма объёмов ордеров должна быть 100%")


def validate_group(g, depth=0):
    from .indicators import CATALOG, validate_params

    if depth > 4:
        raise ValueError("Не более четырёх уровней групп условий")
    if not isinstance(g, dict) or g.get("operator") not in ("AND", "OR"):
        raise ValueError("Условия: выберите И/ИЛИ")
    if (
        not isinstance(g.get("conditions"), list)
        or len(g["conditions"]) > 32
        or not g["conditions"]
        and not g.get("groups")
    ):
        raise ValueError("Требуется хотя бы один фильтр или группа; до 32 фильтров")
    if not isinstance(g.get("groups", []), list) or len(g.get("groups", [])) > 8:
        raise ValueError("Не более восьми групп")
    for child in g.get("groups", []):
        validate_group(child, depth + 1)
    for c in g["conditions"]:
        if c.get("indicator") not in CATALOG:
            raise ValueError("Неизвестный индикатор")
        number(c.get("period"), 2, 500, "Период", True)
        if type(c.get("timeframe")) is not int or c["timeframe"] not in TIMEFRAMES:
            raise ValueError("Таймфрейм")
        if c.get("trigger") not in ("bar_close", "intrabar"):
            raise ValueError("Момент сигнала")
        if c.get("comparison") not in (">", ">=", "<", "<=", "cross_up", "cross_down"):
            raise ValueError("Условие сравнения")
        number(c.get("value"), -1e12, 1e12, "Уровень")
        if c.get("reference") not in ("number", "price", "indicator"):
            raise ValueError("Источник сравнения")
        if c.get("reference") == "indicator":
            number(c.get("reference_period"), 2, 500, "Второй период", True)
        if c["indicator"] == "RSI" and c["reference"] == "number":
            number(c["value"], 0, 100, "Уровень RSI")
        if c.get("output") != "value":
            raise ValueError("Выход индикатора")
        validate_params(c["indicator"], c.get("params", {}))
        number(c.get("shift", 0), 0, 500, "Сдвиг", True)
        if c["reference"] == "indicator":
            if c.get("reference_indicator", c["indicator"]) not in CATALOG:
                raise ValueError("Второй индикатор")
            if (
                type(c.get("reference_timeframe", c["timeframe"])) is not int
                or c.get("reference_timeframe", c["timeframe"]) not in TIMEFRAMES
            ):
                raise ValueError("Второй таймфрейм")
            number(c.get("reference_shift", 0), 0, 500, "Второй сдвиг", True)
            validate_params(
                c.get("reference_indicator", c["indicator"]),
                c.get("reference_params", {}),
            )


def validate(cfg):
    reject_private_data(cfg)
    # Add only newly introduced fields; required monetary/strategy values are
    # still validated rather than silently replaced after an incomplete import.
    cfg = normalize_config(cfg)
    if (
        not isinstance(cfg, dict)
        or type(cfg.get("schema")) is not int
        or cfg.get("schema") != 1
        or cfg.get("mode") != "PAPER"
    ):
        raise ValueError("Неподдерживаемая конфигурация PAPER")
    if cfg.get("strategy") not in STRATEGIES:
        raise ValueError("Стратегия")
    if set(cfg) - set(defaults(cfg["strategy"])):
        raise ValueError("Неизвестные параметры конфигурации")
    if (
        not isinstance(cfg.get("name"), str)
        or not cfg["name"].strip()
        or len(cfg["name"]) > 80
    ):
        raise ValueError("Укажите название бота (до 80 символов)")
    number(cfg.get("capital"), 0.01, 1e9, "Депозит")
    expected_exchange = (
        "POLYMARKET" if cfg["strategy"] == "POLYMARKET" else "BYBIT_LINEAR"
    )
    if cfg.get("exchange") != expected_exchange:
        raise ValueError("Неверный источник данных для выбранной стратегии")
    if type(cfg.get("reinvest")) is not bool:
        raise ValueError("Реинвестирование")
    if type(cfg.get("stop_after_deals")) is not bool:
        raise ValueError("Завершение после сделок")
    symbols = cfg.get("symbols")
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 5000:
        raise ValueError("Выберите от 1 до 5000 торговых пар")
    if any(not isinstance(x, str) for x in symbols):
        raise ValueError("Торговые пары должны быть строками")
    seen, repeated = set(), []
    for symbol in symbols:
        if symbol in seen and symbol not in repeated:
            repeated.append(symbol)
        seen.add(symbol)
    if repeated:
        raise ValueError("Повторяющиеся торговые пары: " + ", ".join(repeated[:10]))
    invalid = [x for x in symbols if not valid_symbol(x, cfg["strategy"])]
    if invalid:
        raise ValueError(
            "Неверный формат торговых пар: "
            + ", ".join(repr(x) for x in invalid[:10])
            + (
                ". Для Polymarket укажите обозначения монет из списка доступных."
                if cfg["strategy"] == "POLYMARKET"
                else ". Для Bybit выберите бессрочные USDT-контракты из списка доступных."
            )
        )
    validate_settings(cfg["settings"])
    ids = set()
    parents = {}
    if (
        not isinstance(cfg.get("sections"), list)
        or not 1 <= len(cfg["sections"]) <= 100
    ):
        raise ValueError("Требуется 1…100 секций")
    for sec in cfg["sections"]:
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", sec.get("id", ""))
            or sec["id"] in ids
        ):
            raise ValueError("ID секции должен быть уникальным")
        ids.add(sec["id"])
        parent_id = sec.get("range_parent_id")
        parent_label = sec.get("range_parent_label")
        if parent_id is not None or parent_label is not None:
            if cfg["strategy"] != "VELES":
                raise ValueError("Иерархия секций доступна только в VELES")
            if (not isinstance(parent_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", parent_id)
                    or not isinstance(parent_label, str)
                    or not 1 <= len(parent_label.strip()) <= 120):
                raise ValueError("Укажите корректный ID и название родительской секции")
            identity = (parent_label, sec.get("side"))
            if parent_id in parents and parents[parent_id] != identity:
                raise ValueError("Диапазоны одной секции должны иметь общее название и направление")
            parents[parent_id] = identity
        if (
            not isinstance(sec.get("label"), str)
            or not sec["label"].strip()
            or len(sec["label"]) > 120
        ):
            raise ValueError("Название секции: от 1 до 120 символов")
        if (
            sec.get("side") not in ("LONG", "SHORT")
            or type(sec.get("enabled")) is not bool
        ):
            raise ValueError("Направление секции")
        validate_group(sec["entry"])
        validate_group(sec["exit"])
        validate_position_settings(
            merge(cfg["settings"], sec.get("overrides", {})), sec["side"]
        )
    if not isinstance(cfg.get("coin_overrides"), dict) or set(
        cfg["coin_overrides"]
    ) - set(symbols):
        raise ValueError("Настройки неизвестной монеты")
    for sym, over in cfg["coin_overrides"].items():
        if set(over) - {"settings", "sections", "entry", "exit", "parameters"}:
            raise ValueError("Неизвестная настройка монеты")
        if set(over.get("sections", {})) - ids:
            raise ValueError("Неизвестная секция монеты")
        for sec in cfg["sections"]:
            validate_position_settings(
                effective(cfg, sym, sec), coin_section(cfg, sym, sec)["side"]
            )
        for sec in over.get("sections", {}).values():
            if set(sec) - {"settings", "entry", "exit", "side", "enabled", "label"}:
                raise ValueError("Неизвестный параметр секции монеты")
            if "side" in sec and sec["side"] not in ("LONG", "SHORT"):
                raise ValueError("Направление секции монеты")
            if "enabled" in sec and type(sec["enabled"]) is not bool:
                raise ValueError("Включение секции монеты")
            if "label" in sec and (
                not isinstance(sec["label"], str)
                or not sec["label"].strip()
                or len(sec["label"]) > 120
            ):
                raise ValueError("Название секции монеты")
            for k in ("entry", "exit"):
                if k in sec:
                    validate_group(sec[k])
        for k in ("entry", "exit"):
            if k in over:
                validate_group(over[k])
        if "parameters" in over:
            block = {
                "GRID": "grid",
                "PUMP_DUMP": "pump",
                "POLYMARKET": "polymarket",
            }.get(cfg["strategy"])
            if (
                block is None
                or not isinstance(over["parameters"], dict)
                or set(over["parameters"]) - set(cfg[block])
            ):
                raise ValueError("Индивидуальные параметры стратегии")
            if block == "polymarket" and set(over["parameters"]) & {
                "overrides",
                "durations",
            }:
                raise ValueError("Длительности выбираются в общей таблице рынков")
            if block != "polymarket":
                candidate = deepcopy(cfg)
                candidate["coin_overrides"] = {}
                candidate[block] = merge(cfg[block], over["parameters"])
                validate(candidate)
            # Polymarket is checked below once for every ACTUAL coin:market.
            # Promoting one coin patch into global settings would incorrectly
            # apply it to other coins and reject otherwise valid configurations.
    g = cfg["grid"]
    for k, lo, hi, i in [
        ("levels", 1, 100, True),
        ("spacing_pct", 0.001, 20, False),
        ("take_profit_pct", 0.001, 100, False),
        ("stop_loss_pct", 0.001, 90, False),
        ("order_usdt", 0.01, 1e9, False),
    ]:
        number(g[k], lo, hi, k, i)
    if g["side"] not in ("LONG", "SHORT"):
        raise ValueError("Направление GRID")
    if g["spacing_pct"] * (g["levels"] - 1) >= 100:
        raise ValueError("Уровни GRID должны оставаться выше нуля")
    p = cfg["pump"]
    for k, lo, hi in [
        ("window_minutes", 1, 30),
        ("move_pct", 0.01, 100),
        ("volume_ratio", 1, 1000),
        ("cooldown_seconds", 0, 86400),
        ("stake_usdt", 0.01, 1e9),
    ]:
        number(p[k], lo, hi, k, k == "window_minutes")
    if (
        not p["modes"]
        or set(p["modes"]) - {"fast", "confirmed"}
        or not p["sides"]
        or set(p["sides"]) - {"LONG", "SHORT"}
    ):
        raise ValueError("Режим отскока или направление")
    for mode in ("fast", "confirmed"):
        for k in ("rebound_pct", "tp_pct", "sl_pct", "max_hold_seconds"):
            number(p[mode][k], 0.001, 1e6, k)
    p = cfg["polymarket"]
    if not isinstance(p, dict) or set(p) - set(defaults("POLYMARKET")["polymarket"]):
        raise ValueError("Неизвестные настройки Polymarket")
    if not isinstance(p.get("durations"), list) or not p["durations"] or any(
        type(x) is not int or x not in (300, 900, 3600) for x in p["durations"]
    ) or len(p["durations"]) != len(set(p["durations"])):
        raise ValueError("Длительность рынка")
    if not isinstance(p.get("overrides"), dict):
        raise ValueError("Настройки рынков")
    allowed = poly_rule_keys()
    possible = {f"{sym}:{duration}" for sym in symbols for duration in p["durations"]}
    for key, over in p["overrides"].items():
        if key not in possible or not isinstance(over, dict) or set(over) - allowed:
            raise ValueError("Неизвестный параметр рынка или комбинация монета/рынок")
    if cfg["strategy"] == "POLYMARKET":
        for symbol in symbols:
            for duration in p["durations"]:
                validate_poly_rule(resolve_poly_market(cfg, symbol, duration), duration)
    else:
        # The Polymarket defaults also exist in non-Polymarket configurations.
        validate_poly_rule(p, 3600)
    return deepcopy(cfg)
