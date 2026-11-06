"""Bounded public UTC reference. OS/process clocks are never modified.

Bybit nanoseconds and Binance milliseconds provide independent intervals.
All accepted intervals must intersect; decisions use the conservative lower
bound and execution expiry uses the upper bound. Monotonic time ages the
reference, so wall-clock adjustments cannot refresh an old snapshot.
"""

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .clock_diagnostics import probe_source


class ClockUnavailable(ValueError):
    pass


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
                results = list(pool.map(probe_source, ("Bybit", "Binance")))
            wall, anchor = time.time(), time.monotonic()
            intervals = []
            try:
                for result in results:
                    good = []
                    for sample in result["samples"]:
                        if (
                            sample.get("error")
                            or sample.get("cached_response") is not False
                            or sample.get("local_clock_changed_during_request")
                            is not False
                            or sample.get("http_status") != 200
                            or not 0 <= sample.get("round_trip_seconds", math.inf) <= 1
                            or not 0
                            < sample.get("server_precision_seconds", math.inf)
                            <= 0.001
                        ):
                            continue
                        elapsed = anchor - sample["received_monotonic"]
                        if not 0 <= elapsed <= 20:
                            continue
                        if abs(wall - sample["local_received_unix"] - elapsed) > 0.05:
                            continue
                        drift = elapsed * 0.0001
                        good.append(
                            (
                                sample["server_unix"] + elapsed - drift,
                                sample["server_unix"]
                                + sample["round_trip_seconds"]
                                + sample["server_precision_seconds"]
                                + elapsed
                                + drift,
                            )
                        )
                    if len(good) != 2:
                        raise ClockUnavailable(
                            "Нет двух точных некэшированных ответов времени "
                            + result["source"]
                        )
                    intervals.extend(good)
                low = max(x[0] for x in intervals)
                high = min(x[1] for x in intervals)
                if (
                    not all(math.isfinite(x) for x in (low, high))
                    or not 0 <= high - low <= 0.5
                ):
                    raise ClockUnavailable(
                        "Время Bybit и Binance не согласуется или слишком неточно"
                    )
                if max(abs(low - wall), abs(high - wall)) > 300:
                    raise ClockUnavailable(
                        "Расхождение времени API и ПК превышает 5 минут"
                    )
            except (KeyError, TypeError, ClockUnavailable) as exc:
                self.evidence = {
                    "status": "UNAVAILABLE",
                    "error": str(exc),
                    "sources": results,
                }
                if valid:
                    logging.warning("CLOCK refresh deferred: %s", exc)
                    return
                raise ClockUnavailable(str(exc)) from exc
            self._reference = (low, high, wall, anchor)
            self.evidence = {
                "status": "PASS",
                "os_clock_adjusted": False,
                "server_minus_pc_lower_seconds": low - wall,
                "server_minus_pc_upper_seconds": high - wall,
                "sources": results,
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
