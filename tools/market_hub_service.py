"""Explicit experimental public-data producer. Does not trade or load keys."""
import argparse
import os
import sys
import traceback
import signal
import sqlite3
import threading
import time
import queue
from bcc.market_hub import MarketDataHub


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--cache', required=True)
    p.add_argument('--bybit-ws-quotes', action='store_true',
                   help='Explicit experimental Bybit ticker WebSocket transport')
    p.add_argument('--bybit-ws-books', action='store_true',
                   help='Explicit experimental Bybit orderbook.50 WebSocket transport')
    p.add_argument('--bybit-ws-klines', action='store_true',
                   help='Explicit experimental Bybit current-minute kline WebSocket transport')
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
    quote_stream = book_stream = kline_stream = None
    ws_threads = []
    ws_jobs = []
    ws_updates = queue.Queue(maxsize=16384)
    ws_pending = set()
    ws_pending_lock = threading.Lock()

    def on_update(kind, symbol):
        item = (kind, symbol)
        with ws_pending_lock:
            if item in ws_pending:
                return
            try:
                ws_updates.put_nowait(item)
            except queue.Full:
                # Streaming backpressure must not stall the receiver.
                # Scheduled hub snapshots remain an independent recovery path.
                return
            ws_pending.add(item)

    def publish_events():
        while not stop.is_set():
            try:
                item = ws_updates.get(timeout=.1)
            except queue.Empty:
                continue
            with ws_pending_lock:
                ws_pending.discard(item)
            try:
                hub.publish_ws_event(*item)
            except (sqlite3.Error, OSError, ValueError) as exc:
                print("WebSocket event publication unavailable: " +
                      type(exc).__name__, flush=True)

    publisher = None
    if args.bybit_ws_quotes or args.bybit_ws_books or args.bybit_ws_klines:
        from websocket import WebSocketApp
        from bcc.market import PublicData
        if args.bybit_ws_quotes:
            from bcc.ws_quotes import BybitQuoteStream
            quote_stream = BybitQuoteStream(on_update=on_update)
        if args.bybit_ws_books:
            from bcc.ws_orderbooks import BybitOrderbookWS
            book_stream = BybitOrderbookWS(on_update=on_update)
        if args.bybit_ws_klines:
            from bcc.ws_klines import BybitKlineWS
            kline_stream = BybitKlineWS(on_update=on_update)
        source = PublicData(quote_stream=quote_stream, orderbook_stream=book_stream,
                            kline_stream=kline_stream)
        hub = MarketDataHub(args.cache, source=source, max_workers=args.concurrency)
        for label, stream in (('tickers', quote_stream), ('depth50', book_stream),
                              ('kline1', kline_stream)):
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
    ready_logged = False
    try:
        while not stop.is_set():
            try:
                if any(not thread.is_alive() for thread in ws_threads):
                    raise RuntimeError("Bybit public WebSocket producer exited")
                if not ready_logged:
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
                hub.poll_nonblocking()
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
        for stream in (quote_stream, book_stream, kline_stream):
            if stream is not None:
                stream.stop()
        for thread in ws_threads:
            thread.join(timeout=5)
        if publisher is not None:
            publisher.join(timeout=5)
        hub.close()


if __name__ == '__main__':
    main()
