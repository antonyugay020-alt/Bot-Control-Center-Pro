"""Optional *closed-candle* technical gate for Polymarket PAPER entries.

The original impulse/ASK/timing entry strategy is untouched unless a bot
explicitly configures indicator_filter. Evaluate only trusted completed Binance
1-minute OHLCV aggregated by the same indicator engine as VELES. Missing or
ambiguous history always blocks entry; it must never act as a pass.
"""

from .indicators import evaluate, walk_conditions

SUPPORTED = frozenset({"RSI", "EMA", "VOLUME", "REL_VOLUME"})
MAX_BARS = 900  # Binance public klines per call capped at 1000


def history_needed(spec):
    """Conservative bounded lookback for 1m input with up to 30m indicators."""
    needed = 150
    for item in walk_conditions(spec):
        # Crosses need 2 completed values and EMA needs a burn-in. Keep the
        # estimate conservative, but bound network reads and reject huge rules.
        left = (3 * item["period"] + 4 + item.get("shift", 0)) * item["timeframe"]
        right = 0
        if item["reference"] == "indicator":
            right = (3 * item["reference_period"] + 4 +
                     item.get("reference_shift", 0)) * item.get(
                         "reference_timeframe", item["timeframe"])
        needed = max(needed, left, right)
    return needed + 30


def validate_filter(spec):
    if spec is None:
        return
    from .config import validate_group

    validate_group(spec)
    for item in walk_conditions(spec):
        if item["indicator"] not in SUPPORTED or (
            item["reference"] == "indicator"
            and item.get("reference_indicator", item["indicator"]) not in SUPPORTED
        ):
            raise ValueError("Polymarket: доступны только RSI, EMA и объём")
        if item["trigger"] != "bar_close":
            raise ValueError("Polymarket: фильтры работают только на закрытых свечах")
    if history_needed(spec) > MAX_BARS:
        raise ValueError("Polymarket: для фильтра требуется слишком длинная история свечей")


def check_filter(spec, source, symbol, now):
    """Return (matched, snapshot). No network request if filter is disabled."""
    if spec is None:
        return True, None
    needed = history_needed(spec)
    bars = source.indicator_bars(symbol, needed, now)
    # Verify timestamp continuity here too, independently of the data adapter.
    # A stale-but-contiguous history must never activate a PAPER signal.
    if (not isinstance(bars, list) or len(bars) < needed or
        type(bars[-1].get("t")) is not int or
        bars[-1]["t"] != (int(now // 60) - 1) * 60000 or
        any(type(a.get("t")) is not int or type(b.get("t")) is not int
            or b["t"] - a["t"] != 60000 for a, b in zip(bars, bars[1:]))):
        raise ValueError("Polymarket: история индикаторов неполная или устарела")
    # The shared indicator engine also checks OHLCV validity and exact
    # completion of each requested timeframe; no intra-candle projection.
    result = evaluate(spec, bars, int(now * 1000), {}, fresh_only=False)
    return bool(result.available and result.matched), {
        "available": bool(result.available),
        "matched": bool(result.matched),
        "observations": result.observations,
        "last_candle_start_ms": bars[-1]["t"] if bars else None,
    }
