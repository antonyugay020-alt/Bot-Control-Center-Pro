"""Real Bybit public WS -> isolated Hub -> client cache acceptance. No orders.

Connects only to public data. Never loads credentials, the user's MY_BOTS or
another project's cache. All process cleanup is identity-checked.
"""
import argparse
import json
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from bcc import process_os as psutil
from bcc.hub_supervisor import _matches, ensure_hub
from bcc.market import PublicData
from bcc.market_clock import book_is_fresh
from bcc.storage import atomic_json

URL = "https://api.bybit.com/v5/market/orderbook"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
           "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT")


def probe(source, symbol):
    started = time.monotonic()
    try:
        # The route is WS-only in this strict client mode. An unverified
        # REST-produced cache row fails the provenance check in PublicData.get.
        params = {"category": "linear", "symbol": symbol, "limit": 50}
        value = source.get(URL, params)
        if value.get("_bcc_ws_book") is not True:
            raise ValueError("No verified Bybit WS provenance")
        book = value["result"]
        if book.get("s") != symbol or not book_is_fresh(float(book["ts"]) / 1000):
            raise ValueError("Wrong symbol or expired exchange timestamp")
        bid = float(book["b"][0][0])
        ask = float(book["a"][0][0])
        if not 0 < bid <= ask:
            raise ValueError("Invalid executable spread")
        return {"symbol": symbol, "status": "PASS",
                "received_in_seconds": round(time.monotonic() - started, 3),
                "age_upper_seconds": round(time.time() - float(book["ts"]) / 1000, 3)}
    except Exception as exc:
        return {"symbol": symbol, "status": "FAIL",
                "error": f"{type(exc).__name__}: {exc}"[:400],
                "received_in_seconds": round(time.monotonic() - started, 3)}


def stop_owned_hub(root):
    record = json.loads((root / "market_hub_process.json").read_text(encoding="utf-8"))
    cache = (root / "market_cache.sqlite").resolve()
    if not _matches(record, cache):
        raise RuntimeError("Cannot prove owned Hub identity: refusing termination")
    process = psutil.Process(record["pid"])
    process.terminate()
    try:
        process.wait(timeout=8)
    except psutil.TimeoutExpired:
        if not _matches(record, cache):
            raise RuntimeError("Hub PID ownership changed: refusing kill")
        process.kill()
        process.wait(timeout=8)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    out = args.output.resolve()
    if out.exists():
        raise RuntimeError("Refusing to reuse any existing Bybit acceptance output")
    out.mkdir(parents=True)
    root = out / "isolated_hub"
    root.mkdir()
    cache = root / "market_cache.sqlite"
    report = {
        "at": datetime.now(timezone.utc).isoformat(),
        "scope": "BYBIT_PUBLIC_WS_ONLY",
        "real_orders": False,
        "isolated_root": str(root),
        "release_ready": False,
        "symbols": list(SYMBOLS),
        "rounds": [],
        "status": "BLOCKED",
    }
    source = None
    hub_started = False
    original = {k: os.environ.get(k) for k in (
        "BCC_PUBLIC_CACHE", "BCC_MARKET_HUB_MODE",
        "BCC_BYBIT_WS_QUOTES", "BCC_BYBIT_WS_BOOKS"
    )}
    try:
        # This root has no MY_BOTS and cannot inherit the user's shared cache.
        ensure_hub(root, bybit_ws=True)
        hub_started = True
        os.environ["BCC_PUBLIC_CACHE"] = str(cache)
        os.environ["BCC_MARKET_HUB_MODE"] = "client"
        os.environ["BCC_BYBIT_WS_QUOTES"] = "1"
        os.environ["BCC_BYBIT_WS_BOOKS"] = "1"
        source = PublicData()
        for iteration in range(3):
            with ThreadPoolExecutor(max_workers=len(SYMBOLS)) as pool:
                rows = list(pool.map(lambda symbol: probe(source, symbol), SYMBOLS))
            report["rounds"].append(rows)
            atomic_json(out / "live_ws_report.json", report)
            if iteration != 2:
                time.sleep(2)
        # Initial subscription may require a handshake; once established,
        # repeated reads must be delivered promptly instead of waiting for
        # the eight-second expired-cache timeout on each symbol.
        report["steady_state_max_seconds"] = max(
            row["received_in_seconds"]
            for batch in report["rounds"][1:] for row in batch
        )
        if (all(row["status"] == "PASS"
                for batch in report["rounds"] for row in batch)
                and report["steady_state_max_seconds"] <= 1.5):
            report["status"] = "PUBLIC_WS_HUB_CLIENT_PASS"
        else:
            report["status"] = "PUBLIC_WS_DELIVERY_INCOMPLETE"
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        (out / "exception.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        if source is not None:
            source.close()
        for key, previous in original.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        if hub_started:
            try:
                stop_owned_hub(root)
                report["isolated_hub_retired"] = True
            except BaseException as exc:
                report["isolated_hub_retired"] = False
                report["cleanup_error"] = str(exc)
                report["status"] = "BLOCKED_HUB_CLEANUP"
        log = root / "market_hub.log"
        if log.exists():
            # Never report private accounts, tokens or raw quotes.
            (out / "hub_public_log_tail.txt").write_text(
                log.read_text(encoding="utf-8", errors="replace")[-12000:],
                encoding="utf-8",
            )
        atomic_json(out / "live_ws_report.json", report)
    print("Bybit public WS acceptance:", report["status"])
    print("Report:", (out / "live_ws_report.json"))
    return 0 if report["status"] == "PUBLIC_WS_HUB_CLIENT_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
