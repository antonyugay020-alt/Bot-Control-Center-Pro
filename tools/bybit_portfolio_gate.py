"""Isolated public Bybit WS mark/book + risk-tier + minute-candle acceptance.

No account discovery, no orders, no adopted processes or existing cache. A
supplied symbol file is a JSON array of public USDT perpetual identifiers.
This is a live-data transport test, NOT proof of actual held PAPER positions.
"""
import argparse
import json
import math
import os
import re
import threading
import time
import traceback
from pathlib import Path

DEFAULT_SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT',
                   'DOGEUSDT', 'ADAUSDT', 'LINKUSDT', 'AVAXUSDT',
                   'AUSDT', '4USDT')
TICKER_URL = 'https://api.bybit.com/v5/market/tickers'
BOOK_URL = 'https://api.bybit.com/v5/market/orderbook'
KLINE_URL = 'https://api.bybit.com/v5/market/kline'
ISOLATION_KEYS = ('BCC_DATA_ROOT', 'BCC_PUBLIC_CACHE', 'BCC_MARKET_HUB_MODE',
                  'BCC_BYBIT_WS_QUOTES', 'BCC_BYBIT_WS_BOOKS', 'BCC_BYBIT_WS_KLINES')


def validate_symbols(value):
    if not isinstance(value, list) or not value:
        raise ValueError('Symbols must be a nonempty JSON list')
    if any(type(s) is not str or re.fullmatch(r'[A-Z0-9]{1,64}USDT', s) is None
           for s in value):
        raise ValueError('Invalid public Bybit USDT symbol')
    if len(set(value)) != len(value):
        raise ValueError('Duplicate symbols forbidden; no silent deduplication')
    return tuple(value)


def inspect_tiers(snapshot, now):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('tiers'), list) or not snapshot['tiers']:
        raise ValueError('Missing independently sourced risk tiers')
    stamp = snapshot.get('risk_at')
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or not -0.5 <= now-stamp <= 300:
        raise ValueError('Risk tiers have no fresh original exchange timestamp')
    return round(now-stamp, 3)


def inspect_candles(bars, now):
    if not isinstance(bars, list) or len(bars) < 100:
        raise ValueError('Incomplete real 1m history')
    stamps = [row.get('t') for row in bars]
    if any(type(t) is not int or t < 0 or t % 60000 for t in stamps):
        raise ValueError('Invalid exchange candle timestamp')
    if any(b-a != 60000 for a,b in zip(stamps,stamps[1:])):
        raise ValueError('Gapped or repeated minute candles')
    age = now-stamps[-1]/1000
    if not 0 <= age < 120:
        raise ValueError('Stale real candle history')
    return round(age, 3)


def assess_cohort(symbols, marks, quotes, is_recent):
    """One coherent nonblocking cache read for the full exposure cohort."""
    return {
        symbol: {'mark': is_recent(marks.get(symbol), maximum=3),
                 'executable_quote': is_recent(quotes.get(symbol), maximum=3)}
        for symbol in symbols
    }


def status_from_evidence(rows, cohort, all_symbols, *, cleanup, source_ok,
                         rounds=None, expected_rounds=0, read_budget=1.5):
    if not cleanup or not source_ok or set(rows) != set(all_symbols) or set(cohort) != set(all_symbols):
        return 'NOT_CONFIRMED'
    if not all(row.get('risk_status') == 'PASS' and row.get('candles_status') == 'PASS'
               and row.get('book_status') == 'PASS' for row in rows.values()):
        return 'NOT_CONFIRMED'
    if not all(row.get('mark') is True and row.get('executable_quote') is True
               for row in cohort.values()):
        return 'NOT_CONFIRMED'
    if expected_rounds:
        if not isinstance(rounds, list) or len(rounds) != expected_rounds:
            return 'NOT_CONFIRMED'
        if any(row.get('full_cohort') is not True
               or type(row.get('read_seconds')) not in (int, float)
               or not 0 <= row['read_seconds'] <= read_budget for row in rounds):
            return 'NOT_CONFIRMED'
    return 'PUBLIC_PORTFOLIO_DATA_PASS'


def run(output, symbols, budget_seconds=180, interval_seconds=0.25, rounds=8):
    from bcc.hub_supervisor import ensure_hub
    from bcc.market import PublicData, SourceAccessDenied
    from bcc.market_clock import clock, market_now, quote_is_recent, book_is_fresh
    from bcc.market_hub import HubRegistry
    from bcc.indicators import aggregate
    from bcc.storage import atomic_json
    from tools.bybit_ws_live_acceptance import stop_owned_hub
    from tools.verify_package import verify, assert_same_snapshot

    symbols = validate_symbols(list(symbols))
    if (not 15 <= budget_seconds <= 3600 or not 0 <= interval_seconds <= 5
            or type(rounds) is not int or not 3 <= rounds <= 120):
        raise ValueError('Invalid bounded public network budget or cohort rounds')
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Refusing to reuse an existing acceptance output')
    seal = verify(Path(__file__).resolve().parents[1])
    output.mkdir(parents=True)
    root = output/'isolated_hub'
    root.mkdir()
    cache_path = root/'market_cache.sqlite'
    report = dict(status='NOT_CONFIRMED', real_orders=False, release_ready=False,
                  simulated_held_priority=True,
                  scope='ISOLATED_PUBLIC_MARK_BOOK_RISK_1M_2M', symbols=list(symbols),
                  source_integrity=seal, rows={}, coherent_portfolio={},
                  cohort_rounds=[], requested_rounds=rounds, isolated_root=str(root))
    saved = {key:os.environ.get(key) for key in ISOLATION_KEYS}
    hub_started = False
    source = None
    stop = threading.Event()
    renewal = None
    errors = []
    started = time.monotonic()
    try:
        for key in ISOLATION_KEYS:
            os.environ.pop(key, None)
        clock.ensure()
        report['trading_clock'] = clock.evidence
        ensure_hub(root, bybit_ws=True)
        hub_started = True
        os.environ['BCC_PUBLIC_CACHE'] = str(cache_path)
        os.environ['BCC_MARKET_HUB_MODE'] = 'client'
        os.environ['BCC_BYBIT_WS_QUOTES'] = '1'
        os.environ['BCC_BYBIT_WS_BOOKS'] = '1'
        source = PublicData()
        source.set_position_symbols(symbols)
        registry = HubRegistry(cache_path)

        def renew():
            # The real worker renews position subscriptions continuously.
            # Simulate that demand only in THIS newly owned cache and Hub.
            while not stop.is_set():
                for symbol in symbols:
                    if stop.is_set():
                        break
                    try:
                        registry.subscribe(TICKER_URL, {'category':'linear','symbol':symbol},
                                           lifetime=60, priority=True)
                        registry.subscribe(BOOK_URL, {'category':'linear','symbol':symbol,'limit':50},
                                           lifetime=60, priority=True)
                    except Exception as exc:
                        errors.append(type(exc).__name__ + ': ' + str(exc)[:160])
                        stop.set()
                        break
                stop.wait(20)

        renewal = threading.Thread(target=renew, daemon=True, name='owned-portfolio-subscriptions')
        renewal.start()
        # First Bybit message requires a real subscription handshake. Never
        # claim that this start-up allowance is a steady-state latency SLA.
        stop.wait(3)
        for symbol in symbols:
            if stop.is_set() or time.monotonic()-started > budget_seconds:
                report['budget_exhausted'] = True
                break
            row = dict(book_status='NOT_CONFIRMED',risk_status='NOT_CONFIRMED',
                       candles_status='NOT_CONFIRMED')
            report['rows'][symbol] = row
            try:
                book = source.position_orderbook(symbol)
                if not isinstance(book, dict) or not book_is_fresh(book.get('at')):
                    raise ValueError('No live executable position book')
                row['book_status'] = 'PASS'
            except Exception as exc:
                row['book_error'] = type(exc).__name__ + ': ' + str(exc)[:160]
                if isinstance(exc, SourceAccessDenied):
                    report['provider_denied'] = True
                    break
            try:
                tiers = source.refresh_position_risk(symbol)
                row['risk_age_seconds'] = inspect_tiers(tiers, market_now())
                row['risk_status'] = 'PASS'
            except Exception as exc:
                row['risk_error'] = type(exc).__name__ + ': ' + str(exc)[:160]
                if isinstance(exc, SourceAccessDenied):
                    report['provider_denied'] = True
                    break
            try:
                bars = source.position_candles(symbol, 100)
                row['candle_age_seconds'] = inspect_candles(bars, market_now())
                # 2m indicator input MUST derive from actual continuous 1m
                # history, never from generated bars or synthetic backfill.
                two = aggregate(bars, 2, int(market_now()*1000), 'bar_close')
                if not two:
                    raise ValueError('No completed genuine 2m aggregation')
                row['candles_status'] = 'PASS'
                row['two_minute_closed_bars'] = len(two)
            except Exception as exc:
                row['candles_error'] = type(exc).__name__ + ': ' + str(exc)[:160]
                if isinstance(exc, SourceAccessDenied):
                    report['provider_denied'] = True
                    break
            atomic_json(output/'portfolio_report.json', report)
            if stop.wait(interval_seconds):
                break
        if source is not None:
            for index in range(rounds):
                if stop.is_set() or time.monotonic()-started > budget_seconds:
                    report['budget_exhausted'] = True
                    break
                clock.ensure()
                before = time.monotonic()
                marks, quotes = source.cached_position_observations()
                cohort = assess_cohort(symbols, marks, quotes, quote_is_recent)
                read_time = time.monotonic()-before
                # By definition a cohort is only fully available if ALL
                # independently timestamped marks and depth quotes are fresh
                # simultaneously. Per-symbol serial successes are not enough.
                report['coherent_portfolio'] = cohort
                mark_count = sum(value['mark'] for value in cohort.values())
                quote_count = sum(value['executable_quote'] for value in cohort.values())
                report['coherent_mark_count'] = mark_count
                report['coherent_quote_count'] = quote_count
                report['cohort_rounds'].append({
                    'round': index+1, 'read_seconds': round(read_time, 6),
                    'mark_count': mark_count, 'quote_count': quote_count,
                    'full_cohort': (mark_count == len(symbols) and
                                    quote_count == len(symbols) and read_time <= 1.5)})
                atomic_json(output/'portfolio_report.json', report)
                if index+1 < rounds:
                    stop.wait(2)
    except BaseException as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)[:300]
        (output/'exception.txt').write_text(traceback.format_exc(),encoding='utf-8')
    finally:
        stop.set()
        if renewal is not None:
            renewal.join(timeout=10)
        report['subscription_renewal_errors'] = errors[:20]
        report['renewal_retired'] = renewal is None or not renewal.is_alive()
        if source is not None:
            source.close()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if hub_started:
            try:
                stop_owned_hub(root)
                report['isolated_hub_retired'] = True
            except BaseException as exc:
                report['isolated_hub_retired'] = False
                report['cleanup_error'] = str(exc)[:240]
        try:
            report['source_integrity_final'] = assert_same_snapshot(
                seal, verify(Path(__file__).resolve().parents[1]))
        except Exception as exc:
            report['source_error'] = str(exc)[:200]
        report['elapsed_seconds'] = round(time.monotonic()-started, 3)
        if report['elapsed_seconds'] > budget_seconds:
            report['budget_exhausted'] = True
        report['status'] = status_from_evidence(
            report['rows'], report['coherent_portfolio'], symbols,
            cleanup=report.get('isolated_hub_retired') is True and report['renewal_retired']
                and not report.get('subscription_renewal_errors'),
            source_ok='source_integrity_final' in report and not report.get('provider_denied')
                and not report.get('error') and not report.get('budget_exhausted'),
            rounds=report['cohort_rounds'], expected_rounds=rounds)
        atomic_json(output/'portfolio_report.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--symbols-file', type=Path,
                        help='Optional JSON array of public symbols (no keys or account files)')
    parser.add_argument('--budget-seconds', type=int, default=180)
    parser.add_argument('--interval-seconds', type=float, default=.25)
    parser.add_argument('--rounds', type=int, default=8)
    args = parser.parse_args(argv)
    symbols = (json.loads(args.symbols_file.read_text(encoding='utf-8'))
               if args.symbols_file else list(DEFAULT_SYMBOLS))
    report = run(args.output, symbols, args.budget_seconds, args.interval_seconds, args.rounds)
    print(json.dumps({'status':report['status'], 'symbols':len(report['symbols']),
                      'mark':report.get('coherent_mark_count'),
                      'book':report.get('coherent_quote_count')},ensure_ascii=False))
    return 0 if report['status']=='PUBLIC_PORTFOLIO_DATA_PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
