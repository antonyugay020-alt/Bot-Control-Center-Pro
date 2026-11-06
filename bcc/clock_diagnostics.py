"""Read-only time comparison; never adjusts the OS or trading clock."""

import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

URLS = {
    "Bybit": "https://api.bybit.com/v5/market/time",
    "Binance": "https://api.binance.com/api/v3/time",
    "Polymarket": "https://clob.polymarket.com/time",
}


def parse_time(provider, data):
    if provider == "Bybit":
        if data.get("retCode") != 0:
            raise ValueError("Bybit did not return server time")
        stamp, precision = int(data["result"]["timeNano"]) / 1e9, 1e-6
    elif provider == "Binance":
        stamp, precision = int(data["serverTime"]) / 1000, 0.001
    else:
        if isinstance(data, dict):
            data = data.get("time", data.get("timestamp"))
        if isinstance(data, bool) or not str(data).isdigit():
            raise ValueError("Expected Polymarket time in whole Unix seconds")
        stamp, precision = int(data), 1.0
    if not math.isfinite(stamp) or stamp <= 0:
        raise ValueError("Invalid server timestamp")
    return stamp, precision


def time_bounds(stamp, precision, sent, received, elapsed):
    jump = abs((received - sent) - elapsed) > 0.05
    return {
        "server_unix": stamp,
        "server_precision_seconds": precision,
        "local_sent_unix": sent,
        "local_received_unix": received,
        "round_trip_seconds": elapsed,
        "local_clock_changed_during_request": jump,
        "server_minus_pc_lower_seconds": None if jump else stamp - received,
        "server_minus_pc_upper_seconds": None if jump else stamp + precision - sent,
    }


def probe_source(provider, count=2):
    result = {"source": provider, "url": URLS[provider], "samples": []}
    with requests.Session() as session:
        for _ in range(count):
            sample = {}
            try:
                sent = time.time()
                start = time.monotonic()
                response = session.get(
                    URLS[provider],
                    timeout=(3, 5),
                    headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
                )
                received, received_mono = time.time(), time.monotonic()
                elapsed = received_mono - start
                sample["received_monotonic"] = received_mono
                sample.update(
                    http_status=response.status_code,
                    http_date=response.headers.get("Date"),
                    cache_age=response.headers.get("Age"),
                    cache_status=response.headers.get("CF-Cache-Status"),
                )
                response.raise_for_status()
                stamp, precision = parse_time(provider, response.json())
                sample.update(time_bounds(stamp, precision, sent, received, elapsed))
                sample["cached_response"] = (
                    float(sample.get("cache_age") or 0) > 0
                    or (sample.get("cache_status") or "").upper() == "HIT"
                )
            except Exception as exc:
                sample["error"] = str(exc)
            result["samples"].append(sample)
    return result


def probe_clocks():
    with ThreadPoolExecutor(max_workers=3) as pool:
        sources = list(pool.map(probe_source, URLS))
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "clock_adjusted": False,
        "note": "Intervals include network round trip and endpoint precision. Polymarket /time returns whole seconds; cached or clock-jump samples are not reliable clock references.",
        "sources": sources,
    }
