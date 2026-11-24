"""Owned read-only raw WS comparison; never modifies a live BCC or trades.

This is a separate proof of ticker state semantics, NOT the explicit-mark +
orderbook load gate and NOT acceptance of a new financial price source.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time
import uuid

from bcc.market_clock import clock, book_age_bounds
from bcc.ws_ticker_state import TickerState

ENDPOINT = 'wss://stream.bybit.com/v5/public/linear'


def utc():
    return datetime.now(timezone.utc).isoformat()


def sample(tracker, symbols, acked, age_bounds):
    states = tracker.observations()
    rows = []
    for name in symbols:
        state = states[name]
        row = dict(symbol=name, explicit_fresh=False, maintained_fresh=False)
        if state is not None and name in acked:
            try:
                lo, hi = age_bounds(state.confirmed_ms / 1000)
                old_lo, old_hi = age_bounds(state.explicit_ms / 1000)
                row.update(explicit_ms=state.explicit_ms, confirmed_ms=state.confirmed_ms,
                           sequence=state.sequence, chain=state.chain, explicit_age_upper=round(old_hi,6),
                           confirmed_age_upper=round(hi,6),
                           explicit_fresh=-.5 <= old_lo <= old_hi <= 3.,
                           maintained_fresh=-.5 <= lo <= hi <= 3.)
            except (ValueError, TypeError, OverflowError):
                pass
        rows.append(row)
    return dict(explicit_pass=sum(r['explicit_fresh'] for r in rows),
                maintained_pass=sum(r['maintained_fresh'] for r in rows),
                rows=rows)


def rest_comparison(before, after, payload, symbols):
    """Compare only stable WS evidence bracketing REST's response timestamp.

    REST supplies a response timestamp, not a per-symbol sampling timestamp.
    This is a consistency check, not a proof of synchronized atomic prices.
    """
    if type(payload) is not dict or payload.get('retCode') != 0:
        raise ValueError('REST response not successful')
    stamp = payload.get('time')
    if type(stamp) is not int or stamp <= 0:
        raise ValueError('REST timestamp missing')
    result = payload.get('result')
    if type(result) is not dict or result.get('category') != 'linear' or type(result.get('list')) is not list:
        raise ValueError('REST category not confirmed')
    marks = {}
    for row in result['list']:
        if type(row) is not dict or row.get('symbol') not in symbols:
            continue
        name = row['symbol']
        if name in marks:
            raise ValueError('duplicate REST symbol')
        value = row.get('markPrice')
        if type(value) not in (str, int, float):
            raise ValueError('REST mark malformed')
        price = float(value)
        if not math.isfinite(price) or price <= 0:
            raise ValueError('REST mark malformed')
        marks[name] = price
    matches, mismatches, uncomparable = [], [], []
    for name in symbols:
        a,b = before.get(name),after.get(name)
        if (a is None or b is None or name not in marks
                or a.generation != b.generation or a.chain != b.chain
                or a.explicit_ms != b.explicit_ms
                or a.price != b.price or not a.confirmed_ms <= stamp <= b.confirmed_ms):
            uncomparable.append(name)
        elif a.price == marks[name]:
            matches.append(name)
        else:
            mismatches.append(name)
    return dict(response_ms=stamp, matched=matches, mismatched=mismatches,
                uncomparable=uncomparable)


def run(symbols, output, *, rest_check=False):
    import websocket
    import requests
    tracker = TickerState(symbols, age_bounds=book_age_bounds)
    report = dict(scope='RAW_TICKER_STATE_COMPARISON_ONLY', status='NOT_CONFIRMED',
                  real_orders=False, release_ready=False, risk_decoder_changed=False,
                  started_utc=utc(), symbols=list(symbols), rounds=[], rest_checks=[], errors=[])
    stop = threading.Event()
    acked = set()
    ack_lock = threading.Lock()
    worker = sock = generation = None
    try:
        clock.ensure()
        clock.bounds()
        sock = websocket.create_connection(ENDPOINT, timeout=5)
        sock.settimeout(.5)
        generation = tracker.connect()
        pending = {}
        for start in range(0,len(symbols),10):
            names = symbols[start:start+10]
            req = uuid.uuid4().hex
            pending[req] = names
            sock.send(json.dumps(dict(op='subscribe',req_id=req,args=['tickers.'+s for s in names])))

        def receive():
            last_ping = time.monotonic()
            try:
                while not stop.is_set():
                    if time.monotonic()-last_ping >= 20.:
                        sock.send(json.dumps(dict(op='ping')))
                        last_ping = time.monotonic()
                    try:
                        raw = sock.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        raise RuntimeError('WS closed before proof completed')
                    msg = json.loads(raw)
                    if type(msg) is not dict:
                        raise ValueError('invalid WS message')
                    if msg.get('op') == 'subscribe':
                        req = msg.get('req_id')
                        if req not in pending or msg.get('success') is not True:
                            raise RuntimeError('subscription ACK not confirmed')
                        with ack_lock:
                            acked.update(pending.pop(req))
                    elif msg.get('op') in ('ping','pong') or msg.get('ret_msg') == 'pong':
                        continue
                    elif type(msg.get('topic')) is str and msg['topic'].startswith('tickers.'):
                        tracker.ingest(msg,generation)
                    else:
                        raise ValueError('unexpected WS message')
            except Exception as exc:
                if not stop.is_set():
                    report['errors'].append(type(exc).__name__+': '+str(exc))
            finally:
                tracker.disconnect(generation)
                stop.set()

        worker=threading.Thread(target=receive,name='owned-ticker-proof',daemon=True)
        worker.start()
        started=time.monotonic()
        while time.monotonic()-started < 30. and not stop.is_set():
            with ack_lock:
                accepted=acked.copy()
            if len(accepted)==len(symbols) and all(tracker.observations().values()):
                break
            stop.wait(.25)
        with ack_lock:
            report['acknowledged_symbols']=len(acked)
        if len(acked)!=len(symbols):
            raise RuntimeError('incomplete subscribed cohort')
        for rnd in range(1,25):
            if stop.is_set():
                break
            clock.ensure()
            clock.bounds()
            with ack_lock:
                accepted=acked.copy()
            one=sample(tracker,symbols,accepted,book_age_bounds)
            one['round']=rnd
            report['rounds'].append(one)
            if rest_check and rnd in (4,12,20):
                before=tracker.observations()
                response=requests.get('https://api.bybit.com/v5/market/tickers',
                    params={'category':'linear'},timeout=5)
                if response.status_code!=200:
                    raise RuntimeError('REST diagnostic HTTP '+str(response.status_code))
                payload=response.json()
                stop.wait(.75)
                report['rest_checks'].append(rest_comparison(before,tracker.observations(),payload,symbols))
            if rnd < 24:
                stop.wait(2.)
        report['stats']=tracker.stats()
        report['explicit_total']=sum(r['explicit_pass'] for r in report['rounds'])
        report['maintained_total']=sum(r['maintained_pass'] for r in report['rounds'])
        report['maintained_complete_rounds']=sum(r['maintained_pass']==len(symbols) for r in report['rounds'])
        report['rest_mismatch_count']=sum(len(c['mismatched']) for c in report['rest_checks'])
        report['rest_match_count']=sum(len(c['matched']) for c in report['rest_checks'])
        report['rest_consistency'] = ('NOT_REQUESTED' if not rest_check else
            'MISMATCH' if report['rest_mismatch_count'] else
            'CONSISTENT_WHERE_COMPARABLE' if report['rest_match_count'] and len(report['rest_checks'])==3
            else 'NOT_CONFIRMED')
        # Even a complete comparison cannot authorize a trading release.
        if (len(report['rounds'])==24 and not stop.is_set() and not report['errors']
                and report['maintained_complete_rounds']==24 and report['rest_mismatch_count']==0
                and report['rest_consistency'] in ('NOT_REQUESTED','CONSISTENT_WHERE_COMPARABLE')):
            report['status']='MAINTAINED_STATE_OBSERVATION_COMPLETE'
        else:
            report['status']='MAINTAINED_STATE_OBSERVATION_INCOMPLETE'
    except Exception as exc:
        report['errors'].append(type(exc).__name__+': '+str(exc))
    finally:
        stop.set()
        if sock is not None:
            sock.close()
        if worker is not None:
            worker.join(timeout=7)
        if generation is not None:
            tracker.disconnect(generation)
        report['owned_worker_retired']=worker is None or not worker.is_alive()
        if not report['owned_worker_retired']:
            report['status']='NOT_CONFIRMED'
        report['ended_utc']=utc()
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--symbols-file',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--rest-check',action='store_true')
    args=p.parse_args()
    symbols=json.loads(args.symbols_file.read_text(encoding='utf-8'))
    if type(symbols) is not list or len(symbols)!=50:
        p.error('this source proof must use the exact 50-symbol reference cohort')
    if args.output.exists():
        p.error('report path already exists')
    result=run(symbols,args.output,rest_check=args.rest_check)
    print('REPORT_JSON='+str(args.output))
    return 0 if result['status']=='MAINTAINED_STATE_OBSERVATION_COMPLETE' else 1


if __name__=='__main__':
    raise SystemExit(main())
