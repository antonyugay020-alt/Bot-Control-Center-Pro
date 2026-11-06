"""Technical indicator calculations and explicit, independently clocked filters.

No unconfirmed candle enters a bar-close filter. Invalid or missing history is
reported as unavailable rather than a false value that can manufacture a cross.
"""

import math
from dataclasses import dataclass

from .config import TIMEFRAMES

CATALOG = {
    "RSI": "RSI",
    "SMA": "Simple Moving Average",
    "EMA": "Exponential Moving Average",
    "SMMA": "Smoothed Moving Average",
    "WMA": "Weighted Moving Average",
    "VWMA": "Volume Weighted Moving Average",
    "HMA": "Hull Moving Average",
    "ALMA": "Arnaud Legoux Moving Average",
    "LSMA": "Least Squares Moving Average",
    "MACD": "MACD — гистограмма",
    "MACD_LINE": "MACD — линия",
    "BB_PERCENT": "Bollinger Bands — положение цены",
    "BB_UPPER": "Bollinger Bands — верхняя",
    "BB_LOWER": "Bollinger Bands — нижняя",
    "ATR": "Average True Range",
    "ADX": "Average Directional Index",
    "CCI": "Commodity Channel Index",
    "CMO": "Chande Momentum Oscillator",
    "MFI": "Money Flow Index",
    "STOCH": "Stochastic %K",
    "STOCH_RSI": "Stochastic RSI %K",
    "ROC": "Rate of Change",
    "PRICE_CHANGE": "Percentage Price Change",
    "PRICE": "Price Filter",
    "VOLUME": "Volume Filter",
    "REL_VOLUME": "Относительный объём",
    "WILLIAMS_R": "Williams %R",
    "AO": "Awesome Oscillator",
    "BOP": "Balance of Power",
    "DONCHIAN_HIGH": "Donchian Channel — верхняя",
    "DONCHIAN_LOW": "Donchian Channel — нижняя",
    "KELTNER_HIGH": "Keltner Channel — верхняя",
    "KELTNER_LOW": "Keltner Channel — нижняя",
    "VORTEX": "Vortex Indicator — VI+ минус VI−",
    "CANDLE": "Candle — изменение тела, %",
    "CANDLES_TREND": "Candles Trend — последовательность свечей",
    "CHAIKIN": "Chaikin Oscillator",
    "HMA_VOLUME": "Hull Moving Average по объёму",
    "SUPERSMOOTHER": "Ehlers SuperSmoother",
    "SUPERTREND": "SuperTrend — направление ±1",
    "PSAR": "Parabolic SAR",
    "ICHIMOKU_TENKAN": "Ichimoku — Tenkan",
    "ICHIMOKU_KIJUN": "Ichimoku — Kijun",
    "ICHIMOKU_A": "Ichimoku — Senkou A",
    "ICHIMOKU_B": "Ichimoku — Senkou B",
    "CONNORS_RSI": "Connors RSI",
    "RVI": "Relative Volatility Index",
    "VOLUME_SPIKE": "Volume Spike — отношение к среднему",
}
EXTRA = {
    "MACD": {"slow": 26, "signal": 9},
    "MACD_LINE": {"slow": 26, "signal": 9},
    "BB_PERCENT": {"deviation": 2.0},
    "BB_UPPER": {"deviation": 2.0},
    "BB_LOWER": {"deviation": 2.0},
    "ALMA": {"offset": 0.85, "sigma": 6.0},
    "KELTNER_HIGH": {"multiplier": 2.0},
    "KELTNER_LOW": {"multiplier": 2.0},
}
EXTRA.update(
    {
        "CHAIKIN": {"slow": 10},
        "SUPERTREND": {"multiplier": 3.0},
        "PSAR": {"acceleration": 0.02, "maximum": 0.2},
        "CONNORS_RSI": {"streak": 2, "rank": 100},
        "ICHIMOKU_A": {"kijun": 26, "displacement": 26},
        "ICHIMOKU_B": {"displacement": 26},
        "AO": {"slow": 34},
    }
)
PERIODLESS = {"PRICE", "VOLUME", "CANDLE", "PSAR", "CANDLES_TREND"}


def validate_params(name, params):
    allowed = EXTRA.get(name, {})
    if not isinstance(params, dict) or set(params) - set(allowed):
        raise ValueError("Параметры индикатора " + name)
    for k, v in params.items():
        if type(v) not in (int, float) or not math.isfinite(v):
            raise ValueError("Параметр " + k)
        integer = k in ("slow", "signal", "streak", "rank", "kijun", "displacement")
        lo, hi = (0, 1) if k == "offset" else (1, 500) if integer else (0.001, 100)
        if not lo <= v <= hi or integer and type(v) is not int:
            raise ValueError("Диапазон " + k)


def rolling(xs, n, fn):
    return [
        fn(xs[i - n + 1 : i + 1])
        if i >= n - 1 and all(v is not None for v in xs[i - n + 1 : i + 1])
        else None
        for i in range(len(xs))
    ]


def sma(xs, n):
    return rolling(xs, n, lambda w: sum(w) / n)


def ema(xs, n, alpha=None):
    out = []
    seed = []
    value = None
    a = alpha if alpha is not None else 2 / (n + 1)
    for x in xs:
        if x is None:
            value = None
            seed = []
            out.append(None)
            continue
        if value is None:
            seed.append(x)
            if len(seed) == n:
                value = sum(seed) / n
        else:
            value += a * (x - value)
        out.append(value)
    return out


def combine(a, b, fn):
    return [fn(x, y) if x is not None and y is not None else None for x, y in zip(a, b)]


def rsi(xs, n):
    changes = [None] + [b - a for a, b in zip(xs, xs[1:])]
    gains = ema([max(0, x) if x is not None else None for x in changes], n, 1 / n)
    losses = ema([max(0, -x) if x is not None else None for x in changes], n, 1 / n)
    return combine(
        gains,
        losses,
        lambda g, l: (
            50.0 if g == l == 0 else 100.0 if l == 0 else 100 - 100 / (1 + g / l)
        ),
    )


def series(name, bars, n, params=None):
    if name not in CATALOG:
        raise ValueError(name)
    params = {**EXTRA.get(name, {}), **(params or {})}
    c = [b["close"] for b in bars]
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    o = [b["open"] for b in bars]
    v = [b["volume"] for b in bars]
    if name == "PRICE":
        return c
    if name == "VOLUME":
        return v
    if name == "HMA_VOLUME":
        return series("HMA", [dict(b, close=b["volume"]) for b in bars], n)
    if name == "RSI":
        return rsi(c, n)
    if name == "SMA":
        return sma(c, n)
    if name == "EMA":
        return ema(c, n)
    if name == "SMMA":
        return ema(c, n, 1 / n)
    if name == "WMA":
        return rolling(
            c,
            n,
            lambda w: sum((i + 1) * x for i, x in enumerate(w)) / (n * (n + 1) / 2),
        )
    if name == "VWMA":
        return combine(
            sma([x * y for x, y in zip(c, v)], n),
            sma(v, n),
            lambda x, y: x / y if y else None,
        )
    if name == "HMA":
        half = series("WMA", bars, max(1, n // 2))
        full = series("WMA", bars, n)
        raw = combine(half, full, lambda x, y: 2 * x - y)
        m = max(1, int(math.sqrt(n)))
        return rolling(
            raw,
            m,
            lambda w: sum((i + 1) * x for i, x in enumerate(w)) / (m * (m + 1) / 2),
        )
    if name == "ALMA":
        m = params["offset"] * (n - 1)
        s = n / params["sigma"]
        weights = [math.exp(-((i - m) ** 2) / (2 * s * s)) for i in range(n)]
        return rolling(
            c, n, lambda w: sum(x * y for x, y in zip(w, weights)) / sum(weights)
        )
    if name == "LSMA":
        mean = (n - 1) / 2
        den = sum((i - mean) ** 2 for i in range(n))
        return rolling(
            c,
            n,
            lambda w: (
                sum(w) / n
                + sum((i - mean) * (x - sum(w) / n) for i, x in enumerate(w))
                / den
                * mean
            ),
        )
    if name == "SUPERSMOOTHER":
        a = math.exp(-math.sqrt(2) * math.pi / n)
        b = 2 * a * math.cos(math.sqrt(2) * math.pi / n)
        c2 = b
        c3 = -a * a
        c1 = 1 - c2 - c3
        out = []
        for i, x in enumerate(c):
            out.append(
                x if i < 2 else c1 * (x + c[i - 1]) / 2 + c2 * out[-1] + c3 * out[-2]
            )
        return out
    if name == "CHAIKIN":
        acc = 0
        ad = []
        for hi, lo, close, vol in zip(h, l, c, v):
            acc += ((2 * close - hi - lo) / (hi - lo) if hi != lo else 0) * vol
            ad.append(acc)
        return combine(ema(ad, n), ema(ad, params["slow"]), lambda a, b: a - b)
    if name == "CONNORS_RSI":
        streak = [0]
        for a, b in zip(c, c[1:]):
            prior = streak[-1]
            streak.append(
                prior + 1
                if b > a and prior > 0
                else 1
                if b > a
                else prior - 1
                if b < a and prior < 0
                else -1
                if b < a
                else 0
            )
        rr = rsi(streak, params["streak"])
        price_rsi = rsi(c, n)
        roc = [None] + [100 * (b / a - 1) for a, b in zip(c, c[1:])]
        rank = params["rank"]
        percent = []
        for i, x in enumerate(roc):
            hist = roc[max(1, i - rank) : i]
            percent.append(
                100 * sum(y < x for y in hist) / rank if len(hist) == rank else None
            )
        return [
            None if any(x is None for x in row) else sum(row) / 3
            for row in zip(rr, price_rsi, percent)
        ]
    if name.startswith("ICHIMOKU"):

        def mid(period):
            return combine(
                rolling(h, period, max),
                rolling(l, period, min),
                lambda a, b: (a + b) / 2,
            )

        if name in ("ICHIMOKU_TENKAN", "ICHIMOKU_KIJUN"):
            return mid(n)
        xs = (
            mid(n)
            if name == "ICHIMOKU_B"
            else combine(mid(n), mid(params["kijun"]), lambda a, b: (a + b) / 2)
        )
        shift = params["displacement"]
        return [None] * min(shift, len(xs)) + xs[:-shift]
    if name.startswith("MACD"):
        line = combine(ema(c, n), ema(c, params["slow"]), lambda x, y: x - y)
        return (
            line
            if name == "MACD_LINE"
            else combine(line, ema(line, params["signal"]), lambda x, y: x - y)
        )
    if name.startswith("BB_"):
        mean = sma(c, n)
        sd = rolling(
            c, n, lambda w: math.sqrt(sum((x - sum(w) / n) ** 2 for x in w) / n)
        )
        upper = combine(mean, sd, lambda x, y: x + params["deviation"] * y)
        lower = combine(mean, sd, lambda x, y: x - params["deviation"] * y)
        if name == "BB_UPPER":
            return upper
        if name == "BB_LOWER":
            return lower
        return [
            None if a is None or b is None else 0.5 if a == b else (price - b) / (a - b)
            for price, a, b in zip(c, upper, lower)
        ]
    tr = (
        [h[0] - l[0]]
        + [
            max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
            for i in range(1, len(c))
        ]
        if c
        else []
    )
    if name == "ATR":
        return ema(tr, n, 1 / n)
    if name == "RVI":
        std = rolling(
            c, n, lambda w: math.sqrt(sum((x - sum(w) / n) ** 2 for x in w) / n)
        )
        up = [
            None if x is None else x if i > 0 and c[i] > c[i - 1] else 0
            for i, x in enumerate(std)
        ]
        down = [
            None if x is None else x if i > 0 and c[i] < c[i - 1] else 0
            for i, x in enumerate(std)
        ]
        return combine(
            ema(up, n), ema(down, n), lambda a, b: 100 * a / (a + b) if a + b else 50
        )
    if name == "SUPERTREND":
        atr = ema(tr, n, 1 / n)
        upper = lower = None
        trend = 1
        out = []
        for i, x in enumerate(c):
            if atr[i] is None:
                out.append(None)
                continue
            u = (h[i] + l[i]) / 2 + params["multiplier"] * atr[i]
            d = (h[i] + l[i]) / 2 - params["multiplier"] * atr[i]
            if upper is not None:
                u = u if u < upper or c[i - 1] > upper else upper
                d = d if d > lower or c[i - 1] < lower else lower
                if trend == -1 and x > upper:
                    trend = 1
                elif trend == 1 and x < lower:
                    trend = -1
            upper, lower = u, d
            out.append(float(trend))
        return out
    if name == "PSAR":
        if len(c) < 2:
            return [None] * len(c)
        rising = c[1] >= c[0]
        sar = l[0] if rising else h[0]
        extreme = h[0] if rising else l[0]
        af = params["acceleration"]
        out = [None]
        for i in range(1, len(c)):
            sar += af * (extreme - sar)
            if rising:
                sar = min(sar, l[i - 1], l[max(0, i - 2)])
                if l[i] < sar:
                    rising = False
                    sar = extreme
                    extreme = l[i]
                    af = params["acceleration"]
                elif h[i] > extreme:
                    extreme = h[i]
                    af = min(params["maximum"], af + params["acceleration"])
            else:
                sar = max(sar, h[i - 1], h[max(0, i - 2)])
                if h[i] > sar:
                    rising = True
                    sar = extreme
                    extreme = h[i]
                    af = params["acceleration"]
                elif l[i] < extreme:
                    extreme = l[i]
                    af = min(params["maximum"], af + params["acceleration"])
            out.append(sar)
        return out
    if name == "ADX":
        plus = [0] + [
            max(h[i] - h[i - 1], 0) if h[i] - h[i - 1] > l[i - 1] - l[i] else 0
            for i in range(1, len(c))
        ]
        minus = [0] + [
            max(l[i - 1] - l[i], 0) if l[i - 1] - l[i] > h[i] - h[i - 1] else 0
            for i in range(1, len(c))
        ]
        p = ema(plus, n, 1 / n)
        m = ema(minus, n, 1 / n)
        dx = combine(p, m, lambda a, b: 100 * abs(a - b) / (a + b) if a + b else 0)
        return ema(dx, n, 1 / n)
    if name == "CCI":
        tp = [(a + b + d) / 3 for a, b, d in zip(h, l, c)]
        return rolling(
            tp,
            n,
            lambda w: (
                (w[-1] - sum(w) / n) / (0.015 * sum(abs(x - sum(w) / n) for x in w) / n)
                if max(w) != min(w)
                else 0
            ),
        )
    if name == "CMO":
        delta = [None] + [b - a for a, b in zip(c, c[1:])]
        return rolling(
            delta, n, lambda w: 100 * sum(w) / sum(abs(x) for x in w) if any(w) else 0
        )
    if name == "MFI":
        tp = [(a + b + d) / 3 for a, b, d in zip(h, l, c)]
        pos = [None] + [
            tp[i] * v[i] if tp[i] > tp[i - 1] else 0 for i in range(1, len(c))
        ]
        neg = [None] + [
            tp[i] * v[i] if tp[i] < tp[i - 1] else 0 for i in range(1, len(c))
        ]
        return combine(
            sma(pos, n),
            sma(neg, n),
            lambda a, b: (
                50 if a == b == 0 else 100 if b == 0 else 100 - 100 / (1 + a / b)
            ),
        )
    if name in ("STOCH", "WILLIAMS_R"):
        hi = rolling(h, n, max)
        lo = rolling(l, n, min)
        return [
            None
            if a is None
            else (50 if name == "STOCH" else -50)
            if a == b
            else 100 * (price - b) / (a - b) - (100 if name == "WILLIAMS_R" else 0)
            for price, a, b in zip(c, hi, lo)
        ]
    if name == "STOCH_RSI":
        r = rsi(c, n)
        hi = rolling(r, n, max)
        lo = rolling(r, n, min)
        raw = [
            None if a is None else 50 if a == b else 100 * (x - b) / (a - b)
            for x, a, b in zip(r, hi, lo)
        ]
        return sma(raw, 3)
    if name in ("ROC", "PRICE_CHANGE"):
        return [None if i < n else 100 * (c[i] / c[i - n] - 1) for i in range(len(c))]
    if name == "REL_VOLUME":
        previous = [None] + sma(v, n)[:-1]
        return combine(v, previous, lambda x, y: x / y if y else None)
    if name == "VOLUME_SPIKE":
        return combine(v, sma(v, n), lambda x, y: x / y if y else None)
    if name == "AO":
        mid = [(a + b) / 2 for a, b in zip(h, l)]
        return combine(sma(mid, n), sma(mid, params["slow"]), lambda a, b: a - b)
    if name == "BOP":
        return [(d - a) / (b - e) if b != e else 0 for a, b, e, d in zip(o, h, l, c)]
    if name.startswith("DONCHIAN"):
        return rolling(
            h if name.endswith("HIGH") else l, n, max if name.endswith("HIGH") else min
        )
    if name.startswith("KELTNER"):
        return combine(
            ema(c, n),
            ema(tr, n, 1 / n),
            lambda a, b: (
                a + params["multiplier"] * b * (1 if name.endswith("HIGH") else -1)
            ),
        )
    if name == "VORTEX":
        p = [None] + [abs(h[i] - l[i - 1]) for i in range(1, len(c))]
        m = [None] + [abs(l[i] - h[i - 1]) for i in range(1, len(c))]
        diff = combine(sma(p, n), sma(m, n), lambda a, b: a - b)
        return combine(diff, sma(tr, n), lambda a, b: a / b if b else 0)
    if name == "CANDLE":
        return [100 * (b / a - 1) for a, b in zip(o, c)]
    if name == "CANDLES_TREND":
        count = 0
        out = []
        for a, b in zip(o, c):
            if b > a:
                count = count + 1 if count > 0 else 1
            elif b < a:
                count = count - 1 if count < 0 else -1
            out.append(float(count))
        return out
    raise ValueError("Не реализован индикатор " + name)


def aggregate(bars, minutes, now_ms, trigger="bar_close"):
    if minutes not in TIMEFRAMES:
        raise ValueError("Таймфрейм")
    groups = {}
    duration = minutes * 60000
    last = -1
    for b in bars:
        t = b["t"]
        if type(t) is not int or t < 0 or t % 60000 or t <= last:
            raise ValueError("Нарушен порядок свечей")
        last = t
        vals = [b[k] for k in ("open", "high", "low", "close", "volume")]
        if (
            any(type(x) not in (int, float) or not math.isfinite(x) for x in vals)
            or min(vals[:4]) <= 0
            or vals[4] < 0
            or b["high"] < max(b["open"], b["close"], b["low"])
            or b["low"] > min(b["open"], b["close"])
        ):
            raise ValueError("Повреждена свеча")
        if t > now_ms:
            continue
        if trigger == "bar_close" and t + 60000 > now_ms:
            continue
        groups.setdefault(t // duration * duration, []).append(b)
    out = []
    for start, rows in sorted(groups.items()):
        expected = (
            minutes if start + duration <= now_ms else (now_ms - start) // 60000 + 1
        )
        if len(rows) != expected or any(
            b["t"] != start + i * 60000 for i, b in enumerate(rows)
        ):
            continue
        closed = start + duration <= now_ms
        if trigger == "bar_close" and not closed:
            continue
        out.append(
            dict(
                t=start,
                open=rows[0]["open"],
                high=max(b["high"] for b in rows),
                low=min(b["low"] for b in rows),
                close=rows[-1]["close"],
                volume=sum(b["volume"] for b in rows),
                closed=closed,
            )
        )
    return out


@dataclass
class Evaluation:
    available: bool
    matched: bool
    observations: list
    clocks: dict


def evaluate(spec, bars, now_ms, clocks, *, fresh_only=True):
    results = []
    observations = []
    next_clocks = dict(clocks)
    availability = []
    advanced = False
    for idx, child in enumerate(spec.get("groups", [])):
        key = "group:" + str(idx)
        res = evaluate(child, bars, now_ms, clocks.get(key, {}), fresh_only=False)
        results.append(res.matched)
        observations.extend(res.observations)
        next_clocks[key] = res.clocks
        availability.append(res.available)
        advanced |= res.clocks != clocks.get(key, {}) or any(
            c["trigger"] == "intrabar" for c in walk_conditions(child)
        )
    for idx, c in enumerate(spec["conditions"]):
        frames = aggregate(bars, c["timeframe"], now_ms, c["trigger"])
        n = c["period"]
        key = str(idx)
        prior = clocks.get(key, {})
        status = {
            "comparison": c["comparison"],
            "indicator": c["indicator"],
            "timeframe": c["timeframe"],
            "trigger": c["trigger"],
            "value": None,
            "matched": False,
            "available": False,
        }
        if not frames:
            availability.append(False)
            results.append(False)
            observations.append(status)
            continue
        if any(
            b["t"] - a["t"] != c["timeframe"] * 60000
            for a, b in zip(frames, frames[1:])
        ):
            availability.append(False)
            results.append(False)
            observations.append(status)
            continue
        xs = series(c["indicator"], frames, n, c.get("params"))
        shift = c.get("shift", 0)
        if shift:
            xs = [None] * min(shift, len(xs)) + xs[:-shift]
        if c["reference"] == "number":
            rhs = [c["value"]] * len(xs)
        elif c["reference"] == "price":
            rhs = [b["close"] for b in frames]
        else:
            right_frames = aggregate(
                bars, c.get("reference_timeframe", c["timeframe"]), now_ms, c["trigger"]
            )
            right = series(
                c.get("reference_indicator", c["indicator"]),
                right_frames,
                c["reference_period"],
                c.get("reference_params"),
            )
            rshift = c.get("reference_shift", 0)
            if rshift:
                right = [None] * min(rshift, len(right)) + right[:-rshift]
            # Align by candle completion time; a larger timeframe must never leak
            # its future close into an earlier left-hand candle.
            rhs = []
            j = -1
            rtf = c.get("reference_timeframe", c["timeframe"]) * 60000
            for frame in frames:
                cutoff = min(now_ms, frame["t"] + c["timeframe"] * 60000)
                while (
                    j + 1 < len(right_frames)
                    and min(now_ms, right_frames[j + 1]["t"] + rtf) <= cutoff
                ):
                    j += 1
                rhs.append(right[j] if j >= 0 else None)
        a, b = xs[-1], rhs[-1]
        timestamp = frames[-1]["t"]
        is_new = timestamp > prior.get("t", -1) or c["trigger"] == "intrabar"
        if a is None or b is None or not math.isfinite(a) or not math.isfinite(b):
            availability.append(False)
            results.append(False)
            observations.append(status)
            continue
        op = c["comparison"]
        cross = op.startswith("cross_")
        if cross:
            if c["trigger"] == "intrabar" and prior and "delta" in prior:
                old_delta = prior["delta"]
            elif (
                len(xs) >= 2
                and xs[-2] is not None
                and rhs[-2] is not None
                and frames[-1]["t"] - frames[-2]["t"] == c["timeframe"] * 60000
            ):
                old_delta = xs[-2] - rhs[-2]
            else:
                availability.append(False)
                results.append(False)
                observations.append(status)
                continue
            delta = a - b
            hit = (
                (old_delta < 0 <= delta)
                if op == "cross_up"
                else (old_delta > 0 >= delta)
            )
            hit = hit and is_new
        else:
            hit = {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
        status.update(value=a, reference=b, matched=hit, available=True)
        availability.append(True)
        observations.append(status)
        results.append(hit)
        advanced |= is_new
        if is_new:
            next_clocks[key] = {"t": timestamp, "delta": a - b}
    matched = all(results) if spec["operator"] == "AND" else any(results)
    available = bool(results) and (
        all(availability) if spec["operator"] == "AND" else any(availability)
    )
    return Evaluation(
        available,
        available and matched and (advanced or not fresh_only),
        observations,
        next_clocks,
    )


def walk_conditions(spec):
    yield from spec["conditions"]
    for child in spec.get("groups", []):
        yield from walk_conditions(child)
