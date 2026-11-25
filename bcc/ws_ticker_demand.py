"""Derive ticker transport demand from an existing closed-minute lease.

The caller owns the leader-fenced write transaction and provider denial check.
No price is read or written; the derived lease never exceeds the parent lease.
"""
import json
import math
import uuid

from .ws_symbols import USDT_SYMBOL

KLINE = 'https://api.bybit.com/v5/market/kline'
TICKER = 'https://api.bybit.com/v5/market/tickers'


def renew_tickers(db, *, now, owner, token, boot, identity):
    if not db.execute('SELECT 1 FROM hub_leader WHERE singleton=1 AND owner=? '
            'AND lease_token=? AND expires>? AND '
            '(boot_epoch=0 OR ?=0 OR abs(boot_epoch-?)<=60)',
            (owner,token,now,boot,boot)).fetchone():
        return None
    rows = db.execute('SELECT k,params,active_until,priority_until FROM hub_subscriptions '
                      'WHERE url=? AND active_until>?',(KLINE,now)).fetchall()
    changes = []
    for key,raw,until,priority in rows:
        try:
            p=json.loads(raw)
            if (type(p) is not dict or set(p)!={'category','symbol','interval','limit'}
                    or p['category']!='linear' or p['interval']!='1'
                    or type(p['limit']) is not int or p['limit']!=10
                    or type(p['symbol']) is not str or not USDT_SYMBOL.fullmatch(p['symbol'])
                    or not math.isfinite(until) or not math.isfinite(priority)
                    or identity(KLINE,p)[0]!=key):
                continue
            ticker=dict(category='linear',symbol=p['symbol'])
            k,encoded=identity(TICKER,ticker)
            changes.append((k,TICKER,encoded,until,min(until,priority) if priority>now else 0,
                            uuid.uuid4().hex,now,now,now,now))
        except (ValueError,TypeError,KeyError,OverflowError):
            continue
    if not changes:
        return 0
    # No rearm or token churn on an already active ticker. The existing
    # producer-only WS route, policy fence and source age remain authoritative.
    before=db.total_changes
    db.executemany('''INSERT INTO hub_subscriptions
        (k,url,params,active_until,next_due,priority_until,subscription_token)
        VALUES(?,?,?,?,0,?,?) ON CONFLICT(k) DO UPDATE SET
        active_until=max(hub_subscriptions.active_until,excluded.active_until),
        priority_until=CASE WHEN hub_subscriptions.active_until<=? THEN excluded.priority_until
                           ELSE max(hub_subscriptions.priority_until,excluded.priority_until) END,
        subscription_token=CASE WHEN hub_subscriptions.active_until<=? THEN excluded.subscription_token
                                ELSE hub_subscriptions.subscription_token END,
        next_due=CASE WHEN hub_subscriptions.active_until<=? THEN 0 ELSE hub_subscriptions.next_due END,
        last_error=CASE WHEN hub_subscriptions.active_until<=? THEN '' ELSE hub_subscriptions.last_error END
        WHERE hub_subscriptions.url=excluded.url AND hub_subscriptions.params=excluded.params
          AND (hub_subscriptions.active_until<excluded.active_until
               OR hub_subscriptions.priority_until<excluded.priority_until)''',changes)
    return db.total_changes-before
