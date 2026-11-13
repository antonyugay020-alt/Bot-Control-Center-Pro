"""Explicit experimental public-data producer. Does not trade or load keys."""
import argparse
import os
import sys
import traceback
import signal
import sqlite3
import threading
import time
from bcc.market_hub import MarketDataHub


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--cache', required=True)
    p.add_argument('--bybit-ws-quotes', action='store_true',
                   help='Explicit experimental Bybit per-symbol ticker WebSocket transport')
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
    quote_stream = None
    ws_thread = None
    if args.bybit_ws_quotes:
        from bcc.market import PublicData
        from bcc.ws_quotes import BybitQuoteStream
        quote_stream = BybitQuoteStream()
        source = PublicData(quote_stream=quote_stream)
        hub = MarketDataHub(args.cache, source=source, max_workers=args.concurrency)
        ws_thread = threading.Thread(target=quote_stream.run,
                                     name='bybit-public-ws', daemon=True)
        ws_thread.start()
    else:
        hub = MarketDataHub(args.cache, max_workers=args.concurrency)
    print('Market Hub cache initialized', flush=True)
    last_client = time.monotonic()
    ready_logged = False
    try:
        while not stop.is_set():
            try:
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
                    print('Market Hub leader lease acquired', flush=True)
                    ready_logged = True
                hub.poll_nonblocking()
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
        if quote_stream is not None:
            try:
                quote_stream.stop()
            finally:
                if ws_thread is not None:
                    ws_thread.join(timeout=5)
        hub.close()


if __name__ == '__main__':
    main()
