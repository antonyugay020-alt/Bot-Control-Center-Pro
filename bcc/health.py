"""Read-only connectivity checks. This module never starts a trading worker."""

import argparse
import math
import platform
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import VERSION
from .market import DataUnavailable, PolymarketData, PublicData
from .market_clock import book_age_bounds, clock
from .storage import atomic_json


def check_bybit():
    source = PublicData()
    try:
        candles = source.candles("BTCUSDT", 150)
        book = source.orderbook("BTCUSDT")
        age_low, age_high = book_age_bounds(book["at"])
        if not -0.5 <= age_low <= age_high <= 3:
            raise DataUnavailable("Стакан устарел до завершения проверки источника")
        return {
            "symbol": "BTCUSDT",
            "candles": len(candles),
            "last_candle_at": candles[-1]["t"] / 1000,
            "bid": book["bids"][0][0],
            "ask": book["asks"][0][0],
            "book_at": book["at"],
            "book_age_lower": age_low,
            "book_age_upper": age_high,
            "book_fresh": -0.5 <= age_low <= age_high <= 3,
            "book_transport": book.get("transport"),
            "response_at": book.get("response_at"),
            "engine_at": book.get("engine_at"),
            "clock_reference": clock.evidence,
            "qty_step": book["qty_step"],
            "min_notional": book["min_notional"],
        }
    finally:
        source.close()


class NoLiquidity(DataUnavailable):
    pass


def check_polymarket():
    source = PolymarketData()
    try:
        market = source.discover("BTC", 300)
        terms = source.execution_terms(market)
        checked = {}
        candidate = None
        # A liquid UP book is not proof that DOWN is reachable and valid.
        # Verify both tokens before claiming that Gamma + CLOB are available.
        for outcome in ("UP", "DOWN"):
            try:
                asks, stamp = source.book(market["tokens"][outcome])
                eligible = []
                for row in asks:
                    price, size = float(row["price"]), float(row["size"])
                    if (
                        not math.isfinite(price)
                        or not math.isfinite(size)
                        or size < 0
                        or not 0 <= price <= 1
                    ):
                        raise DataUnavailable("Повреждён стакан " + outcome)
                    if size > 0 and 0 < price < 1:
                        eligible.append(price)
                if not eligible:
                    checked[outcome] = {"status": "NO_LIQUIDITY"}
                    continue
                best = min(eligible)
                checked[outcome] = {"status": "PASS", "best_ask": best}
                if candidate is None:
                    candidate = {"outcome": outcome, "best_ask": best, "book_at": stamp}
            except (DataUnavailable, ValueError, KeyError, TypeError) as exc:
                checked[outcome] = {"status": "UNAVAILABLE", "error": str(exc)}
        # A broken token cannot be hidden by a healthy opposite outcome.
        if any(row["status"] == "UNAVAILABLE" for row in checked.values()):
            raise DataUnavailable("Стаканы текущего рынка недоступны: " + str(checked))
        if candidate is not None:
            return {
                "slug": market["slug"],
                "market_start": market["start"],
                "market_end": market["end"],
                **candidate,
                "checked_outcomes": checked,
                "clock_reference": clock.evidence,
                "terms": terms,
            }
        raise NoLiquidity(
            "Нет исполнимых ASK в UP и DOWN текущего рынка " + market["slug"]
        )
    finally:
        source.close()


def check_signal_source():
    source = PolymarketData()
    try:
        now = time.time()
        start = int(now) // 300 * 300
        usd, pct = source.impulse("BTC", start, now)
        return {
            "symbol": "BTCUSDT",
            "start": start,
            "impulse_usd": usd,
            "impulse_pct": pct,
        }
    finally:
        source.close()


def check_sources():
    def run(item):
        name, callback = item
        start = time.monotonic()
        result = {"source": name}
        try:
            result.update(status="PASS", evidence=callback())
        except NoLiquidity as e:
            result.update(status="NO_LIQUIDITY", error=str(e))
        except Exception as e:
            result.update(status="UNAVAILABLE", error=str(e))
        result["elapsed_seconds"] = round(time.monotonic() - start, 3)
        return result

    callbacks = [
        ("Bybit USDT Futures", check_bybit),
        ("Polymarket Gamma + CLOB", check_polymarket),
        ("Binance spot impulse", check_signal_source),
    ]
    with ThreadPoolExecutor(max_workers=3) as pool:
        checks = list(pool.map(run, callbacks))
    report = {
        "app_version": VERSION,
        "at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "read_only": True,
        "trading_clock": clock.evidence,
        "checks": checks,
        "all_available": all(c["status"] == "PASS" for c in checks),
    }
    if any(
        c["source"] == "Polymarket Gamma + CLOB"
        and "Устаревший стакан" in c.get("error", "")
        for c in checks
    ):
        from .clock_diagnostics import probe_clocks

        report["clock_diagnostics"] = probe_clocks()
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Проверка публичных источников без ордеров"
    )
    parser.add_argument(
        "--output", type=Path, default=Path("verification_reports/public_sources.json")
    )
    args = parser.parse_args()
    report = check_sources()
    atomic_json(args.output, report)
    for check in report["checks"]:
        print(check["source"], check["status"], check.get("error", ""), flush=True)
    print("Report:", args.output.resolve(), flush=True)
    return 0 if report["all_available"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
