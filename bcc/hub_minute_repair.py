"""Canonical closed-minute REST repair. Never a mark-price source."""
import hashlib
import json
import uuid

from .closed_minutes import key as ws_key, validate

URL = 'https://api.bybit.com/v5/market/kline'
HOST = 'api.bybit.com'


def params_for(symbol, start):
    ws_key(symbol, start)  # validate symbol and exact minute boundary
    return dict(category='linear', symbol=symbol, interval='1', limit=1,
                end=start+59999)


def identity(params):
    raw = json.dumps([URL, params], sort_keys=True, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def rest_minute(data, symbol, start, now_ms):
    """Only the requested completed minute from the actual exchange response."""
    try:
        if (type(data) is not dict or type(data.get('retCode')) is not int
                or data['retCode'] != 0 or any(k.startswith('_bcc_ws_') for k in data)
                or type(data.get('time')) is not int
                or data['time'] < start+60000):
            return None
        result = data['result']
        if result['symbol'] != symbol or result['category'] != 'linear':
            return None
        rows = result['list']
        if type(rows) is not list or len(rows) != 1 or int(rows[0][0]) != start:
            return None
        row = rows[0]
        if len(row) < 6:
            return None
        return validate(dict(symbol=symbol, t=start, confirmed_ms=data['time'],
            **dict(zip(('open','high','low','close','volume'), map(float,row[1:6])))),
            symbol, now_ms)
    except (KeyError, TypeError, ValueError, IndexError, OverflowError, AttributeError):
        return None


def cached_repair(db, symbol, start, now, now_ms):
    row = db.execute('SELECT value FROM cache WHERE k=? AND host=? AND until>?',
                     (identity(params_for(symbol,start)),HOST,now)).fetchone()
    if row is None:
        return None
    try:
        return rest_minute(json.loads(row[0]),symbol,start,now_ms)
    except (ValueError, TypeError):
        return None


def schedule(db, jobs, start, now, now_ms, *, owner, token, boot):
    """Caller owns a short write transaction. No network or cache publication.

    Only active kline-tail demand under the current producer lease can generate
    a repair. Never overwrite an in-flight generation or extend an existing TTL.
    """
    if len(jobs)>64 or not 0 <= now_ms-(start+60000) < 60000:
        raise ValueError('Invalid repair batch')
    if not db.execute('SELECT 1 FROM hub_leader WHERE singleton=1 AND owner=? '
            'AND lease_token=? AND expires>? AND '
            '(boot_epoch=0 OR ?=0 OR abs(boot_epoch-?)<=60)',
            (owner,token,now,boot,boot)).fetchone():
        return None
    scheduled = []
    for symbol in jobs:
        parent = dict(category='linear', symbol=symbol, interval='1', limit=10)
        parent_key = identity(parent)
        active = db.execute('SELECT priority_until,active_until FROM hub_subscriptions '
            'WHERE k=? AND url=? AND params=? AND active_until>?',
            (parent_key,URL,json.dumps(parent,sort_keys=True,separators=(',',':')),now)).fetchone()
        if active is None:
            continue
        final = db.execute('SELECT value FROM cache WHERE k=? AND host=? AND until>?',
                           (ws_key(symbol,start),HOST,now)).fetchone()
        if final:
            try:
                row=validate(json.loads(final[0]),symbol,now_ms)
                if row['t']==start:
                    continue
            except (ValueError, TypeError):
                pass
        if cached_repair(db,symbol,start,now,now_ms) is not None:
            continue
        params=params_for(symbol,start)
        encoded=json.dumps(params,sort_keys=True,separators=(',',':'))
        until=min(now+30,active[1])
        priority=min(active[0],until) if active[0]>now else 0
        k=identity(params)
        # Existing demand (including an active HTTP lease) wins. A newly
        # activated client can independently rearm an expired subscription.
        changed=db.execute('INSERT OR IGNORE INTO hub_subscriptions '
            '(k,url,params,active_until,next_due,priority_until,subscription_token) '
            'VALUES(?,?,?,?,0,?,?)',(k,URL,encoded,until,priority,uuid.uuid4().hex)).rowcount
        if changed:
            scheduled.append(symbol)
    return scheduled
