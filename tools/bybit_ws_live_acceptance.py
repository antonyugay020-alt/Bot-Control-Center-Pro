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
from bcc.market import PublicData, DataUnavailable
from bcc.market_clock import clock, book_age_bounds
from bcc.storage import atomic_json
from tools.verify_package import verify, assert_same_snapshot

MIN_ROUNDS = 24

URL = "https://api.bybit.com/v5/market/orderbook"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
           "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT")


def _record_age(row, lower_key, upper_key, stamp):
    low, high = book_age_bounds(stamp)
    row[lower_key], row[upper_key] = round(low, 3), round(high, 3)
    if not -.5 <= low <= high <= 3:
        raise ValueError("Exchange observation outside vetted UTC freshness bounds: "
                         + lower_key + f" [{low:.3f}, {high:.3f}]")


def probe(source, symbol, *, require_mark=False, initial=False):
    started = time.monotonic()
    row = {"symbol": symbol, "status": "FAIL"}
    try:
        # Every process consuming raw cache observations needs its own vetted
        # UTC reference. The producer's clock is not inherited across a spawn.
        clock.ensure()
        params = {"category": "linear", "symbol": symbol, "limit": 50}
        value = source.get(URL, params)
        mark = None
        if require_mark:
            deadline = time.monotonic() + (4.0 if initial else 1.0)
            while True:
                try:
                    mark = source._ws_position_mark(symbol)
                    break
                except DataUnavailable:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.1)
            # A book read before waiting for the mark may have expired.
            # Re-read the real WS cache; never restamp the old observation.
            value = source.get(URL, params)
        if value.get("_bcc_ws_book") is not True:
            raise ValueError("No verified Bybit WS provenance")
        book = value["result"]
        if book.get("s") != symbol:
            raise ValueError("Wrong symbol")
        _record_age(row, "book_age_lower_seconds", "age_upper_seconds", float(book["ts"]) / 1000)
        bid, ask = float(book["b"][0][0]), float(book["a"][0][0])
        if not 0 < bid <= ask:
            raise ValueError("Invalid executable spread")
        if mark is not None:
            _record_age(row, "mark_age_lower_seconds", "mark_age_seconds", mark["at"])
            row["mark_price_verified"] = True
        row["status"] = "PASS"
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"[:400]
    row["received_in_seconds"] = round(time.monotonic() - started, 3)
    return row


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


def validate_report(report, manifest_sha256):
    """Require every symbol/mark in sustained, source-bound transport evidence."""
    import math
    errors = []
    if not isinstance(report, dict):
        return ["Bybit WS report is not an object"]
    if (report.get("status") != "PUBLIC_WS_HUB_CLIENT_PASS"
            or report.get("require_mark") is not True
            or report.get("real_orders") is not False
            or report.get("isolated_hub_retired") is not True):
        errors.append("Bybit WS status, mark verification or owned cleanup incomplete")
    for key in ("source_integrity", "source_integrity_final"):
        record = report.get(key)
        if (not isinstance(record, dict) or record.get("status") != "PASS"
                or record.get("manifest_sha256") != manifest_sha256):
            errors.append("Bybit WS source snapshot mismatch: " + key)
    rounds = report.get("rounds")
    requested = report.get("requested_rounds")
    if (type(requested) is not int or not MIN_ROUNDS <= requested <= 120
            or not isinstance(rounds, list) or len(rounds) != requested):
        return errors + ["Bybit WS sustained round inventory incomplete"]
    steady = []
    for index, batch in enumerate(rounds):
        if (not isinstance(batch, list) or len(batch) != len(SYMBOLS)
                or any(not isinstance(row, dict) for row in batch)
                or sorted(str(row.get("symbol")) for row in batch) != sorted(SYMBOLS)):
            errors.append("Bybit WS symbol inventory incomplete")
            continue
        for row in batch:
            if row.get("status") != "PASS" or row.get("mark_price_verified") is not True:
                errors.append("Bybit WS symbol or independent mark failed")
            for key in ("mark_age_lower_seconds", "mark_age_seconds", "book_age_lower_seconds", "age_upper_seconds", "received_in_seconds"):
                value = row.get(key)
                if type(value) not in (int, float) or not math.isfinite(value):
                    errors.append("Bybit WS timing missing or invalid")
                elif key != "received_in_seconds" and not -.5 <= value <= 3:
                    errors.append("Bybit WS exchange observation expired")
                elif key == "received_in_seconds":
                    if value < 0 or (index and value > 1.5):
                        errors.append("Bybit WS delivery latency exceeded")
                    if index:
                        steady.append(value)
    if not steady or report.get("steady_state_max_seconds") != max(steady):
        errors.append("Bybit WS steady-state latency evidence inconsistent")
    return list(dict.fromkeys(errors))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-mark", action="store_true",
                        help="Also require independently timestamped live Bybit WS mark-price")
    parser.add_argument("--rounds", type=int, default=MIN_ROUNDS,
                        help="Sustained public-data sampling rounds (minimum 24)")
    args = parser.parse_args(argv)
    if not MIN_ROUNDS <= args.rounds <= 120:
        parser.error("rounds must be between 24 and 120")
    out = args.output.resolve()
    if out.exists():
        raise RuntimeError("Refusing to reuse any existing Bybit acceptance output")
    out.mkdir(parents=True)
    root = out / "isolated_hub"
    root.mkdir()
    cache = root / "market_cache.sqlite"
    integrity = verify(Path(__file__).resolve().parents[1])
    report = {
        "at": datetime.now(timezone.utc).isoformat(),
        "scope": "BYBIT_PUBLIC_WS_BOOK_AND_MARK" if args.require_mark else "BYBIT_PUBLIC_WS_ONLY",
        "require_mark": args.require_mark,
        "real_orders": False,
        "source_integrity": integrity,
        "requested_rounds": args.rounds,
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
        clock.ensure()
        report["trading_clock"] = clock.evidence
        ensure_hub(root, bybit_ws=True)
        hub_started = True
        os.environ["BCC_PUBLIC_CACHE"] = str(cache)
        os.environ["BCC_MARKET_HUB_MODE"] = "client"
        os.environ["BCC_BYBIT_WS_QUOTES"] = "1"
        os.environ["BCC_BYBIT_WS_BOOKS"] = "1"
        source = PublicData()
        for iteration in range(args.rounds):
            clock.ensure()
            report["trading_clock"] = clock.evidence
            with ThreadPoolExecutor(max_workers=len(SYMBOLS)) as pool:
                rows = list(pool.map(
                    lambda symbol: probe(source, symbol,
                                         require_mark=args.require_mark,
                                         initial=iteration == 0),
                    SYMBOLS,
                ))
            report["rounds"].append(rows)
            atomic_json(out / "live_ws_report.json", report)
            if iteration != args.rounds - 1:
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
        try:
            report["source_integrity_final"] = assert_same_snapshot(
                integrity, verify(Path(__file__).resolve().parents[1]))
        except Exception as exc:
            report["status"] = "BLOCKED_SOURCE_CHANGED"
            report["source_error"] = str(exc)
        atomic_json(out / "live_ws_report.json", report)
    if report["status"] == "PUBLIC_WS_HUB_CLIENT_PASS" and args.require_mark:
        problems = validate_report(report, integrity["manifest_sha256"])
        if problems:
            report["status"] = "BLOCKED_INCOMPLETE_EVIDENCE"
            report["evidence_errors"] = problems
            atomic_json(out / "live_ws_report.json", report)
    print("Bybit public WS acceptance:", report["status"])
    print("Report:", (out / "live_ws_report.json"))
    return 0 if report["status"] == "PUBLIC_WS_HUB_CLIENT_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
