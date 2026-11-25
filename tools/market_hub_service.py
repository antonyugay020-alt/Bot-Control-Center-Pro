"""Explicit experimental public-data producer. Does not trade or load keys."""
import argparse
import json
import os
import sys
import traceback
import signal
import sqlite3
import threading
import time
from bcc.market_hub import MarketDataHub
from bcc.market_clock import ClockUnavailable, clock, market_now
from bcc.ws_dispatch import MarketEventQueue


def create_market_event_queue():
    """Producer must use adaptive active-demand capacity, not a fixed 16k cap."""
    return MarketEventQueue(urgent_burst=8)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--cache', required=True)
    p.add_argument('--bybit-ws-quotes', action='store_true',
                   help='Explicit experimental Bybit ticker WebSocket transport')
    p.add_argument('--bybit-ws-books', action='store_true',
                   help='Explicit experimental Bybit orderbook.50 WebSocket transport')
    p.add_argument('--bybit-ws-klines', action='store_true',
                   help='Explicit experimental Bybit current-minute kline WebSocket transport')
    p.add_argument('--poly-ws-books', action='store_true',
                   help='Explicit experimental public Polymarket CLOB market WebSocket')
    p.add_argument('--binance-ws-price', action='store_true',
                   help='Explicit experimental Binance spot trade WebSocket for PAPER')
    p.add_argument('--ready-file', default=None)
    p.add_argument('--ready-token', default=None)
    p.add_argument('--poll', type=float, default=.1)
    p.add_argument('--concurrency', type=int, default=6)
    p.add_argument('--idle-exit', type=float, default=90.0,
                   help='Exit after this many seconds without active clients; 0 disables')
    args = p.parse_args()
    if bool(args.ready_file) != bool(args.ready_token):
        p.error('ready-file and ready-token must be given together')
    if args.ready_token and (len(args.ready_token) != 32 or
            any(c not in '0123456789abcdef' for c in args.ready_token)):
        p.error('invalid ready-token')
    if not 0 <= args.idle_exit <= 86400:
        p.error('--idle-exit must be 0..86400 seconds')
    if not .02 <= args.poll <= 5:
        p.error('--poll must be 0.02..5 seconds')
    if args.concurrency < 1:
        p.error('--concurrency must be a positive integer')
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (OSError, ValueError):
            pass
    # Startup telemetry belongs to this public-data worker's dedicated
    # market_hub.log. No credentials or market payloads are logged.
    print(f'Market Hub starting: pid={os.getpid()} '
          f'python={sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}',
          flush=True)
    quote_stream = book_stream = kline_stream = poly_stream = binance_stream = None
    ws_threads = []
    ws_jobs = []
    ws_updates = create_market_event_queue()

    def on_update(kind, symbol):
        ws_updates.notify(kind, symbol)

    def publish_events():
        last_warning = 0.0
        while not stop.is_set():
            # Coalesce very small bursts rather than taking a WAL writer
            # transaction for each one to three live market notifications.
            # A saturated queue drains immediately; the low-load wait is
            # bounded to 20ms, far below the original source age fences.
            mark_balanced = os.environ.get("BCC_BYBIT_MARK_DELIVERY_CANDIDATE")=="1"
            if mark_balanced:
                # Reduce shared WAL writer contention under the measured
                # depth/ticker flood. Saturated batches retain 40ms cadence;
                # ordinary batches coalesce latest-source notifications for
                # at most 100ms. Every age is still checked at commit/read.
                delay = .040 if ws_updates.stats()["pending"]>64 else .100
                if stop.wait(delay):
                    break
            elif os.environ.get("BCC_BYBIT_CENTRAL_REPAIR_CANDIDATE")=="1":
                if stop.wait(.040):
                    break
            elif ws_updates.stats()["pending"] < 64:
                if stop.wait(.020):
                    break
            batch = (ws_updates.take_mark_balanced_batch(timeout=.1) if mark_balanced
                     else ws_updates.take_batch(timeout=.1))
            if os.environ.get("BCC_BYBIT_CENTRAL_REPAIR_CANDIDATE")=="1":
                batch = list(dict.fromkeys(batch))
            if not batch:
                continue
            try:
                # No fresh WS evidence is published while the producer's
                # independent UTC quorum is absent. Never use wall fallback.
                clock.bounds()
                hub.publish_ws_events(batch)
            except ClockUnavailable:
                continue
            except (sqlite3.Error, OSError, ValueError) as exc:
                print("WebSocket event publication unavailable: " +
                      type(exc).__name__, flush=True)
            stats = ws_updates.stats()
            if stats["dropped_notifications"] and time.monotonic() - last_warning > 30:
                print("WebSocket event queue saturated; snapshots fail closed",
                      flush=True)
                last_warning = time.monotonic()

    publisher = None
    if (args.bybit_ws_quotes or args.bybit_ws_books or
            args.bybit_ws_klines or args.poly_ws_books or
            args.binance_ws_price):
        from websocket import WebSocketApp
        from bcc.market import PublicData
        if args.bybit_ws_quotes:
            # Explicit diagnostic opt-in only; no default transport change.
            if os.environ.get('BCC_BYBIT_DUAL_WS_CANDIDATE') == '1':
                from bcc.ws_dual_quotes import DualBybitQuoteStream
                quote_stream = DualBybitQuoteStream(wall=market_now, on_update=on_update,
                    maintained=os.environ.get('BCC_BYBIT_MAINTAINED_MARK_CANDIDATE') == '1',
                    maintained_recovery=os.environ.get('BCC_BYBIT_MARK_DELIVERY_CANDIDATE') == '1')
            else:
                from bcc.ws_quotes import BybitQuoteStream
                quote_stream = BybitQuoteStream(wall=market_now, on_update=on_update)
        if args.bybit_ws_books:
            from bcc.ws_orderbooks import BybitOrderbookWS
            book_stream = BybitOrderbookWS(wall=market_now, on_update=on_update)
        if args.bybit_ws_klines:
            from bcc.ws_klines import BybitKlineWS
            kline_stream = BybitKlineWS(wall=market_now, on_update=on_update,
                retain_confirmed=os.environ.get('BCC_BYBIT_CLOSED_HISTORY_CANDIDATE') == '1')
        if args.poly_ws_books:
            from bcc.ws_polymarket import PolymarketMarketWS
            poly_stream = PolymarketMarketWS(wall=market_now, on_update=on_update)
        if args.binance_ws_price:
            from bcc.ws_binance import BinanceTradeWS
            binance_stream = BinanceTradeWS(wall=market_now, on_update=on_update)
        source = PublicData(quote_stream=quote_stream, orderbook_stream=book_stream,
                            kline_stream=kline_stream, polymarket_stream=poly_stream,
                            binance_stream=binance_stream)
        hub = MarketDataHub(args.cache, source=source, max_workers=args.concurrency)
        if os.environ.get('BCC_BYBIT_DUAL_WS_CANDIDATE') == '1':
            hub.source.shared.bybit_ws_source_deadlines = True
        if os.environ.get('BCC_BYBIT_SPLIT_HISTORY_CANDIDATE') == '1':
            hub.enable_split_history()
            print('Dedicated REST history/metadata executors enabled', flush=True)
        if os.environ.get('BCC_BYBIT_CLOSED_HISTORY_CANDIDATE') == '1':
            if kline_stream is None:
                raise RuntimeError('Central finalized history requires Bybit kline stream')
            hub.source.shared.central_closed_history = True
            print('Central finalized minute cache enabled', flush=True)
        if os.environ.get("BCC_BYBIT_CENTRAL_REPAIR_CANDIDATE")=="1":
            if not hub.source.shared.central_closed_history or hub._history_pool is None:
                raise RuntimeError("Central repair requires closed history and split REST lanes")
            hub.source.shared.central_repair = True
            print("Canonical closed-minute repair enabled",flush=True)
        hub.set_ws_dispatcher(ws_updates)
        for label, stream in (('tickers', quote_stream), ('depth50', book_stream),
                              ('kline1', kline_stream), ('polybook', poly_stream),
                              ('binance_trade', binance_stream)):
            if stream is None:
                continue
            thread = threading.Thread(
                target=lambda selected=stream: selected.run(
                    websocket_app_factory=WebSocketApp),
                name='bybit-ws-' + label, daemon=True)
            ws_jobs.append(thread)
    else:
        hub = MarketDataHub(args.cache, max_workers=args.concurrency)
    print('Market Hub cache initialized', flush=True)
    last_client = time.monotonic()
    last_ws_health = 0.0
    ready_logged = False
    clock_was_ready = None
    clock_outages = 0
    clock_recoveries = 0
    try:
        while not stop.is_set():
            try:
                market_ready = not ws_jobs or clock.poll_refresh()
                if ws_jobs and clock_was_ready != market_ready:
                    if not market_ready:
                        clock_outages += int(clock_was_ready is True)
                        print("Market Hub clock unavailable; market admission paused", flush=True)
                    else:
                        clock_recoveries += int(clock_was_ready is False and ready_logged)
                        print("Market Hub verified clock available; market admission enabled", flush=True)
                    clock_was_ready = market_ready
                if any(not thread.is_alive() for thread in ws_threads):
                    raise RuntimeError("Bybit public WebSocket producer exited")
                if publisher is not None and not publisher.is_alive():
                    raise RuntimeError("Public WebSocket cache publisher exited")
                if not ready_logged:
                    if not market_ready:
                        stop.wait(args.poll)
                        continue
                    # Startup lease and steady-state queue use the same
                    # bounded SQLite contention policy. A brief writer lock
                    # must not kill the service before it can acknowledge
                    # readiness; no readiness receipt is emitted without a lease.
                    if not hub.acquire_leadership(lifetime=120):
                        stop.wait(args.poll)
                        continue
                    if args.ready_file:
                        from pathlib import Path
                        from bcc.storage import atomic_json
                        # Bind the readiness challenge to this exact Hub lease
                        # incarnation. A PID alone can be reused after a crash;
                        # the supervisor must persist and later revoke only
                        # this owner's generation, never a successor's lease.
                        atomic_json(Path(args.ready_file), {
                            'pid': os.getpid(), 'token': args.ready_token,
                            'leader_owner': hub.owner,
                        })
                    # Never start sockets on a standby Hub: only the
                    # authoritative leased producer may attach to Bybit.
                    if ws_jobs:
                        publisher = threading.Thread(
                            target=publish_events, name='ws-cache-publisher',
                            daemon=True)
                        publisher.start()
                    for thread in ws_jobs:
                        thread.start()
                        ws_threads.append(thread)
                    print('Market Hub leader lease acquired', flush=True)
                    ready_logged = True
                try:
                    hub.poll_nonblocking(market_ready=market_ready)
                except ClockUnavailable:
                    # The clock may expire after the admission check and
                    # before an individual scheduler operation. This is a
                    # recoverable external-data outage, not loss of leadership.
                    market_ready = False
                    if clock_was_ready:
                        clock_outages += 1
                        print("Market Hub clock unavailable during scheduling; market admission paused", flush=True)
                    clock_was_ready = False
                    hub.poll_nonblocking(market_ready=False)
                if ws_threads and time.monotonic() - last_ws_health >= 10:
                    # Only aggregate public transport liveness and queue
                    # pressure; no token ids, API keys or market payloads.
                    states = {}
                    for label, stream in (
                        ("bybit_quotes", quote_stream),
                        ("bybit_depth", book_stream),
                        ("bybit_klines", kline_stream),
                        ("clob_books", poly_stream),
                        ("binance_trade", binance_stream),
                    ):
                        if stream is None:
                            continue
                        health = getattr(stream, 'transport_health', None)
                        if callable(health):
                            try:
                                states[label] = health()
                            except ClockUnavailable:
                                states[label] = {"clock": "unavailable"}
                            continue
                        with stream._lock:
                            states[label] = {
                                "connection": "connected" if stream._connected else "reconnecting",
                            }
                            # Connected sockets alone prove nothing about
                            # active symbol delivery. Log counts, not prices,
                            # symbols or account data, so a stalled subscription
                            # is observable without disclosing user holdings.
                            if label == "bybit_quotes":
                                states[label]["subscribed"] = len(stream._subscriptions)
                                states[label]["received_symbols"] = len(stream._quotes)
                            elif label == "bybit_depth":
                                states[label]["subscribed"] = len(stream._symbols)
                                states[label]["received_symbols"] = len(stream._books)
                            pressure = getattr(stream, "refresh_pressure", None)
                            if callable(pressure):
                                states[label]["refresh"] = pressure()
                    queue_stats = ws_updates.stats()
                    cached_marks = None
                    if (market_ready and quote_stream is not None
                            and os.environ.get("BCC_BYBIT_MARK_DELIVERY_CANDIDATE")=="1"):
                        from bcc.data_cache import CacheBusy, ProviderDenied
                        try:
                            names = tuple(hub._ws_quote_symbols)
                            rows = hub.source.shared.read_fresh_many([
                                ("https://api.bybit.com/v5/market/tickers",
                                 {"category":"linear","symbol":s}) for s in names
                            ], "api.bybit.com")
                            from bcc.market import DataUnavailable
                            fresh = 0
                            for symbol, row in zip(names, rows):
                                if not isinstance(row,dict):
                                    continue
                                try:
                                    PublicData._decode_ws_position_mark(symbol,row)
                                except DataUnavailable:
                                    continue
                                fresh += 1
                            cached_marks = {"active":len(names),"fresh":fresh}
                        except (CacheBusy,ProviderDenied,ClockUnavailable):
                            cached_marks = {"unavailable":True}
                    print("Market Hub WS health: " +
                          json.dumps({
                              "streams": states,
                              "queue": queue_stats,
                              "publication": hub.ws_publication_stats(),
                              "rest_lanes": hub.rest_lane_health(),
                              "mark_delivery": {
                                  "enabled": os.environ.get("BCC_BYBIT_MARK_DELIVERY_CANDIDATE")=="1",
                                  "balanced_batch": os.environ.get("BCC_BYBIT_MARK_DELIVERY_CANDIDATE")=="1",
                                  "cache": cached_marks,
                              },
                              "clock": {
                                  "ready": market_ready,
                                  "outages": clock_outages,
                                  "recoveries": clock_recoveries,
                              },
                              "closed_history": {
                                  "enabled": hub.source.shared.central_closed_history,
                                  "published": hub.source.shared.stats["finalized_minutes_published"],
                                  "repair_enabled": hub.source.shared.central_repair,
                                  "repair_scheduled": hub._repair_scheduled,
                                  "repair_completed": hub._repair_completed,
                              },
                          }, sort_keys=True), flush=True)
                    last_ws_health = time.monotonic()
                if ws_threads and not hub._leader_held:
                    raise RuntimeError("Bybit WebSocket producer lost Hub leader lease")
                # A failed liveness probe is NOT permission to retire a
                # leader with potentially active PAPER subscribers.
                if args.idle_exit:
                    if hub.registry.has_active_subscriptions():
                        last_client = time.monotonic()
                    elif time.monotonic() - last_client >= args.idle_exit:
                        break
            except sqlite3.OperationalError as exc:
                if not any(x in str(exc).lower() for x in ('locked', 'busy')):
                    raise
                print('Market Hub transient SQLite contention: ' + type(exc).__name__,
                      flush=True)
                stop.wait(min(1.0, max(args.poll, .02)))
            stop.wait(args.poll)
    except BaseException:
        traceback.print_exc()
        raise
    finally:
        for stream in (quote_stream, book_stream, kline_stream, poly_stream,
                       binance_stream):
            if stream is not None:
                stream.stop()
        for thread in ws_threads:
            thread.join(timeout=5)
        if publisher is not None:
            publisher.join(timeout=5)
        hub.close()


if __name__ == '__main__':
    main()
