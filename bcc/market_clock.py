"""Bounded public UTC reference. OS/process clocks are never modified.

Bybit/Binance are primary independent references; OKX/Bitget are bounded
fallback public time references, never replacement market-data transports.
A unique quorum of at least two precise uncached sources is required. Decisions
use the conservative lower bound and expiry uses the upper bound. Monotonic
time ages the reference, so wall-clock adjustments cannot refresh old evidence.
"""

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations

from .clock_diagnostics import probe_source


class ClockUnavailable(ValueError):
    pass


# Independent public clock references. A single provider (even with two
# samples) never authorizes a PAPER decision. Probe the second pair only if the
# primary pair cannot establish a bounded consensus. This avoids making every
# Polymarket/Bybit worker depend on *both* original clock hosts being reachable.
PRIMARY_CLOCKS = ("Bybit", "Binance")
FALLBACK_CLOCKS = ("OKX", "Bitget")


def _provider_interval(provider, result, wall, anchor):
    """Validate two uncached sub-millisecond samples from one provider.

    This function returns a bounded interval, never a timestamp guess. The
    second sample does not count as a second independent provider.
    """
    if not isinstance(result, dict) or result.get("source") != provider:
        raise ClockUnavailable("Ответ другого источника времени: " + provider)
    samples = result.get("samples")
    if not isinstance(samples, list) or len(samples) != 2:
        raise ClockUnavailable("Нет двух ответов времени: " + provider)
    intervals = []
    for sample in samples:
        if not isinstance(sample, dict):
            raise ClockUnavailable("Повреждён ответ времени: " + provider)
        fields = (
            "round_trip_seconds", "server_precision_seconds", "server_unix",
            "received_monotonic", "local_received_unix",
        )
        if (
            sample.get("error")
            or sample.get("cached_response") is not False
            or sample.get("local_clock_changed_during_request") is not False
            or sample.get("http_status") != 200
            or any(type(sample.get(key)) not in (int, float)
                   or not math.isfinite(sample[key]) for key in fields)
            # A provider's independently measured network RTT is not
            # equivalent to the uncertainty of the final two-provider UTC.
            # Permit bounded slow connections here; _clock_consensus keeps
            # the <=0.5 second executable trading-time requirement.
            or not 0 <= sample["round_trip_seconds"] <= 1.5
            or not 0 < sample["server_precision_seconds"] <= 0.001
        ):
            raise ClockUnavailable("Нет двух точных некэшированных ответов времени " + provider)
        elapsed = anchor - sample["received_monotonic"]
        if not 0 <= elapsed <= 20 or abs(
            wall - sample["local_received_unix"] - elapsed
        ) > 0.05:
            raise ClockUnavailable("Устаревшая/несогласованная метка времени: " + provider)
        drift = elapsed * 0.0001
        intervals.append((
            sample["server_unix"] + elapsed - drift,
            sample["server_unix"] + sample["round_trip_seconds"]
            + sample["server_precision_seconds"] + elapsed + drift,
        ))
    low = max(interval[0] for interval in intervals)
    high = min(interval[1] for interval in intervals)
    # A single source provides an uncertainty envelope, never permission
    # to trade. Do not reject two internally consistent uncached samples
    # merely because their shared network envelope is wider than 0.5s.
    # The independent-provider intersection below MUST still be <=0.5s.
    if not 0 <= high - low <= 1.5:
        raise ClockUnavailable("Два ответа источника не согласуются: " + provider)
    return low, high


def _clock_consensus(intervals, wall):
    """Find a unique maximal independent-provider consensus, or fail closed.

    Never silently choose between two disjoint groups of apparently healthy
    clocks. A lone surviving source is not enough to execute PAPER orders.
    """
    providers = sorted(intervals)
    for size in range(len(providers), 1, -1):
        groups = []
        for group in combinations(providers, size):
            low = max(intervals[name][0] for name in group)
            high = min(intervals[name][1] for name in group)
            if (
                0 <= high - low <= 0.5
                and max(abs(low - wall), abs(high - wall)) <= 300
            ):
                groups.append((low, high, group))
        if len(groups) == 1:
            return groups[0]
        if groups:
            raise ClockUnavailable("Неоднозначный кворум независимых источников времени")
    raise ClockUnavailable("Нет двух независимых согласованных источников времени")


def _validated_clock_sources(results, wall, anchor):
    valid, errors = {}, {}
    for provider, result in results:
        try:
            valid[provider] = _provider_interval(provider, result, wall, anchor)
        except ClockUnavailable as exc:
            errors[provider] = str(exc)
    return valid, errors


class MarketClock:
    lifetime = 60
    refresh_after = 30

    def __init__(self):
        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._reference = None
        self._attempted = None
        self.evidence = None

    def _bounds(self):
        if self._reference is None:
            raise ClockUnavailable("Торговое время ещё не сверено с публичными API")
        low, high, wall, mono = self._reference
        elapsed = time.monotonic() - mono
        if not 0 <= elapsed <= self.lifetime:
            raise ClockUnavailable("Истёк срок сверки торгового времени")
        if abs(time.time() - wall - elapsed) > 0.05:
            raise ClockUnavailable("Часы ПК изменились: нужна повторная сверка времени")
        drift = elapsed * 0.0001
        return low + elapsed - drift, high + elapsed + drift

    def bounds(self):
        with self._lock:
            return self._bounds()

    def ensure(self):
        # A slow public probe must not block execution age checks/heartbeats.
        # Readers retain the previous immutable reference until it expires.
        with self._sync_lock:
            mono = time.monotonic()
            try:
                valid = self._bounds()
            except ClockUnavailable:
                valid = None
            if valid and mono - self._reference[3] < self.refresh_after:
                return
            if self._attempted is not None and mono - self._attempted < 10:
                if valid:
                    return
                raise ClockUnavailable(
                    "Сверка времени API недоступна; повтор через 10 секунд"
                )
            self._attempted = mono
            with ThreadPoolExecutor(max_workers=2) as pool:
                primary = list(zip(PRIMARY_CLOCKS, pool.map(probe_source, PRIMARY_CLOCKS)))
            results = primary
            wall, anchor = time.time(), time.monotonic()
            candidates, errors = _validated_clock_sources(results, wall, anchor)
            try:
                low, high, selected = _clock_consensus(candidates, wall)
            except ClockUnavailable:
                # Fallback is a fresh independent *time* reference, not a
                # replacement for blocked exchange market data or an HTTP
                # proxy. No stale orderbook is ever made executable by this.
                with ThreadPoolExecutor(max_workers=2) as pool:
                    fallback = list(zip(FALLBACK_CLOCKS, pool.map(probe_source, FALLBACK_CLOCKS)))
                results = primary + fallback
                wall, anchor = time.time(), time.monotonic()
                candidates, errors = _validated_clock_sources(results, wall, anchor)
                try:
                    low, high, selected = _clock_consensus(candidates, wall)
                except ClockUnavailable as exc:
                    self.evidence = {
                        "status": "UNAVAILABLE",
                        "error": str(exc),
                        "provider_errors": errors,
                        "sources": [result for _, result in results],
                    }
                    if valid:
                        logging.warning("CLOCK refresh deferred: %s", exc)
                        return
                    raise ClockUnavailable(str(exc)) from exc
            self._reference = (low, high, wall, anchor)
            self.evidence = {
                "status": "PASS",
                "os_clock_adjusted": False,
                "selected_sources": list(selected),
                "fallback_used": len(results) > len(PRIMARY_CLOCKS),
                "provider_errors": errors,
                "server_minus_pc_lower_seconds": low - wall,
                "server_minus_pc_upper_seconds": high - wall,
                "sources": [result for _, result in results],
            }
            logging.info(
                "CLOCK public UTC minus PC [%+.3f, %+.3f] seconds",
                low - wall,
                high - wall,
            )


clock = MarketClock()


def utc_bounds():
    # Pure strategy tests/replay transports do not initialize a public clock.
    # Production public transports always call ensure() before timed data.
    if clock._reference is None and clock._attempted is None:
        now = time.time()
        return now, now
    return clock.bounds()


def market_now(fallback=None):
    if fallback is not None and clock._reference is None and clock._attempted is None:
        return fallback
    return utc_bounds()[0]


def latest_now():
    return utc_bounds()[1]


def book_age_bounds(stamp):
    if type(stamp) not in (int, float) or not math.isfinite(stamp):
        raise ValueError("Некорректная метка времени стакана")
    low, high = utc_bounds()
    return low - stamp, high - stamp


def book_is_fresh(stamp):
    try:
        low, high = book_age_bounds(stamp)
        return low >= -0.5 and high <= 3
    except (ClockUnavailable, ValueError, TypeError):
        return False


def observed_quote(quote):
    """Attach receipt evidence for readers that do not own the worker's UTC clock."""
    low, high = utc_bounds()
    return {
        **quote,
        "seen_at": time.time(),
        "seen_mono": time.monotonic(),
        "source_age_low": low - quote["at"],
        "source_age_high": high - quote["at"],
    }


def quote_is_recent(quote, maximum=15, now=None):
    if not quote:
        return False
    now = time.time() if now is None else now
    try:
        if "seen_mono" in quote:
            elapsed = time.monotonic() - quote["seen_mono"]
            wall_elapsed = now - quote["seen_at"]
            values = [
                elapsed,
                wall_elapsed,
                quote["source_age_low"],
                quote["source_age_high"],
            ]
            return (
                all(math.isfinite(v) for v in values)
                and quote["source_age_low"] <= quote["source_age_high"]
                and elapsed >= 0
                and abs(wall_elapsed - elapsed) <= 0.05
                and quote["source_age_low"] + elapsed >= -0.5
                and quote["source_age_high"] + elapsed <= maximum
            )
        # Old persisted marks have no receipt evidence. Future-dated marks cannot
        # be treated as fresh indefinitely after a clock change.
        age = now - quote["at"]
        return math.isfinite(age) and -0.5 <= age <= maximum
    except (KeyError, TypeError, ValueError):
        return False
