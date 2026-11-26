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
from bcc.data_cache import CacheBusy, ProviderDenied
from bcc.market import PublicData, DataUnavailable, SourceAccessDenied
from bcc.market_clock import clock, book_age_bounds
from bcc.storage import atomic_json
from tools.verify_package import verify, assert_same_snapshot

MIN_ROUNDS = 24
STEADY_READ_BUDGET = 1.5

URL = "https://api.bybit.com/v5/market/orderbook"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
           "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "AUSDT", "4USDT")


def _record_age(row, lower_key, upper_key, stamp):
    low, high = book_age_bounds(stamp)
    row[lower_key], row[upper_key] = round(low, 3), round(high, 3)
    if not -.5 <= low <= high <= 3:
        raise ValueError("Exchange observation outside vetted UTC freshness bounds: "
                         + lower_key + f" [{low:.3f}, {high:.3f}]")


def _sample_pair(source, symbol, row):
    """Sample both independent channels in one SQLite read transaction.

    A successful scalar mark read cannot remain the authority after a later
    blocking depth read. Use the same bulk-cache boundary as the worker's
    portfolio observer, then validate both original exchange timestamps.
    """
    source._assert_provider_allowed('api.bybit.com')
    value, ticker = source.shared.read_fresh_many([
        (URL, {'category':'linear','symbol':symbol,'limit':50}),
        ('https://api.bybit.com/v5/market/tickers',
         {'category':'linear','symbol':symbol}),
    ], 'api.bybit.com')
    # Only provenance/timing metadata, never prices, book sizes or credentials.
    # When a bounded read fails the next report can distinguish a missing
    # book from a quote-only replacement of a once-verified mark.
    row['snapshot_flags'] = {
        'book_cached': value is not None,
        'ticker_cached': ticker is not None,
        'mark_provenance': ticker.get('_bcc_ws_mark') is True if isinstance(ticker, dict) else False,
        'quote_provenance': ticker.get('_bcc_ws_quote') is True if isinstance(ticker, dict) else False,
        'generation_present': bool(ticker.get('_bcc_ws_connection_fence')) if isinstance(ticker, dict) else False,
    }
    if value is None or ticker is None:
        raise DataUnavailable('Independent WS book/mark cache snapshot incomplete')
    _record_book(row, symbol, value)
    if ticker.get('_bcc_ws_mark') is not True:
        raise ValueError('No verified Bybit WS mark provenance')
    _record_age(row, 'mark_age_lower_seconds', 'mark_age_seconds',
                float(ticker['_bcc_ws_mark_at']) / 1000)
    source._decode_ws_position_mark(symbol, ticker)
    row['mark_price_verified'] = True


def _record_book(row, symbol, value):
    if value.get('_bcc_ws_book') is not True:
        raise ValueError('No verified Bybit WS provenance')
    book = value['result']
    if book.get('s') != symbol:
        raise ValueError('Wrong symbol')
    _record_age(row, 'book_age_lower_seconds', 'age_upper_seconds', float(book['ts']) / 1000)
    bid, ask = float(book['b'][0][0]), float(book['a'][0][0])
    if not 0 < bid <= ask:
        raise ValueError('Invalid executable spread')


def probe(source, symbol, *, require_mark=False, initial=False, parallel=False):
    started = time.monotonic()
    parallel = bool(parallel and require_mark)
    row = {"symbol": symbol, "status": "FAIL", "stage_seconds": {}, "parallel_pair": parallel}
    # Preserve wall-clock duration across both independent reads and their
    # coherent recheck. In parallel mode scalar stage durations overlap.
    def measure(name, operation):
        before = time.monotonic()
        try:
            return operation()
        finally:
            durations = row["stage_seconds"]
            durations[name] = round(durations.get(name, 0) + time.monotonic() - before, 4)

    def wait_mark(deadline):
        while True:
            try:
                return measure('mark_get', lambda: source._ws_position_mark(symbol))
            except SourceAccessDenied:
                raise
            except DataUnavailable as exc:
                row['last_mark_failure'] = str(exc)[:160]
                if time.monotonic() >= deadline or source.stop_event.is_set():
                    raise
                time.sleep(min(.1, max(0., deadline - time.monotonic())))

    stage = 'initial_book_get'
    try:
        measure('clock_ensure', clock.ensure)
        params = {"category": "linear", "symbol": symbol, "limit": 50}
        def get_book():
            return measure('book_get', lambda: source.get(URL, params))
        if parallel:
            from tools.ws_pair_delivery import parallel_reads
            # The initial handshake keeps the original combined 8+4 second
            # allowance. Steady delivery retains the same 1.5 s total budget.
            deadline = started + (12.0 if initial else STEADY_READ_BUDGET)
            depth, collateral = measure('parallel_pair_wait', lambda: parallel_reads(
                get_book, lambda: wait_mark(deadline)))
            value = depth.result()
            stage = 'independent_mark_get'
            collateral.result()
            if time.monotonic() > deadline:
                raise DataUnavailable('Independent WS delivery exceeded total budget')
        else:
            value = get_book()
            stage = 'independent_mark_get'
            if require_mark:
                deadline = (time.monotonic() + 4.0 if initial
                            else started + STEADY_READ_BUDGET)
                wait_mark(deadline)
        if require_mark:
            attempts = 0
            stage = 'coherent_cache_pair'
            while True:
                candidate = {}
                attempts += 1
                try:
                    measure('coherent_pair_read', lambda: _sample_pair(source, symbol, candidate))
                    row.update(candidate)
                    row.pop('last_mark_failure', None)
                    break
                except ProviderDenied as exc:
                    raise SourceAccessDenied(exc.host, exc.http_status) from exc
                except SourceAccessDenied:
                    raise
                except (DataUnavailable, CacheBusy, ValueError):
                    if time.monotonic() >= deadline or source.stop_event.is_set():
                        row.update(candidate)
                        raise
                    time.sleep(min(.05, max(0., deadline-time.monotonic())))
                finally:
                    row['snapshot_attempts'] = attempts
        else:
            _record_book(row, symbol, value)
        if parallel and time.monotonic() > deadline:
            raise DataUnavailable('Coherent WS delivery exceeded total budget')
        row["status"] = "PASS"
    except Exception as exc:
        row["failed_stage"] = stage
        row["error"] = f"{type(exc).__name__}: {exc}"[:400]
    elapsed = time.monotonic() - started
    row["received_in_seconds"] = round(elapsed, 3)
    if parallel and not initial and elapsed > STEADY_READ_BUDGET:
        row['status'] = 'FAIL'
        row.setdefault('failed_stage', 'total_delivery_budget')
        row.setdefault('error', 'Independent WS delivery exceeded total budget')
    durations = row['stage_seconds']
    accounted = sum(v for k, v in durations.items()
                    if not parallel or k not in ('book_get','mark_get'))
    row['unattributed_seconds'] = round(max(0., elapsed - accounted), 4)
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


def validate_report(report, manifest_sha256, *, require_central=False):
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
    if require_central or report.get('central_demand_requested') is True:
        from tools.ws_pair_delivery import central_evidence_problems
        errors.extend(central_evidence_problems(report, symbols=SYMBOLS))
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
                    if value < 0 or (index and value > STEADY_READ_BUDGET):
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
    parser.add_argument('--central-demand', action='store_true',
                        help='Require central Hub demand transport, with parallel independent WS reads')
    args = parser.parse_args(argv)
    if args.central_demand and not args.require_mark:
        parser.error('--central-demand requires --require-mark')
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
        "central_demand_requested": args.central_demand,
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
        "BCC_BYBIT_WS_QUOTES", "BCC_BYBIT_WS_BOOKS",
        "BCC_PUBLIC_DEMAND_HUB_CANDIDATE", "BCC_BYBIT_REST_MARK_FALLBACK"
    )}
    try:
        # This root has no MY_BOTS and cannot inherit the user's shared cache.
        clock.ensure()
        report["trading_clock"] = clock.evidence
        if args.central_demand:
            os.environ['BCC_PUBLIC_DEMAND_HUB_CANDIDATE'] = '1'
            # This remains a strict WS-only gate; official REST fallback is
            # independently tested during the long PAPER acceptance.
            os.environ.pop('BCC_BYBIT_REST_MARK_FALLBACK', None)
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
                                         initial=iteration == 0,
                                         parallel=args.central_demand),
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
                and report["steady_state_max_seconds"] <= STEADY_READ_BUDGET):
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
        if args.central_demand:
            from tools.ws_pair_delivery import collect_central_evidence
            try:
                report['central_transport'] = collect_central_evidence(root)
            except (OSError, ValueError) as exc:
                report['central_transport_error'] = str(exc)
        try:
            report["source_integrity_final"] = assert_same_snapshot(
                integrity, verify(Path(__file__).resolve().parents[1]))
        except Exception as exc:
            report["status"] = "BLOCKED_SOURCE_CHANGED"
            report["source_error"] = str(exc)
        atomic_json(out / "live_ws_report.json", report)
    if report["status"] == "PUBLIC_WS_HUB_CLIENT_PASS" and args.require_mark:
        problems = validate_report(report, integrity["manifest_sha256"], require_central=args.central_demand)
        if problems:
            report["status"] = "BLOCKED_INCOMPLETE_EVIDENCE"
            report["evidence_errors"] = problems
            atomic_json(out / "live_ws_report.json", report)
    print("Bybit public WS acceptance:", report["status"])
    print("Report:", (out / "live_ws_report.json"))
    return 0 if report["status"] == "PUBLIC_WS_HUB_CLIENT_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
