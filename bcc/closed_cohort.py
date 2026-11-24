"""Read finalized minute evidence for a portfolio, independent of slow GETs."""
import json
from concurrent.futures import ThreadPoolExecutor
from .closed_minutes import key as ws_key, validate, HOST, FIELDS
from .hub_minute_repair import identity, params_for, rest_minute


def plan(previous, required, boundary):
    """Only warm, contiguous, previously verified windows may be extended."""
    if len(previous) < required or not previous:
        return None
    last = previous[-1]['t']
    if type(last) is not int or last % 60000 or not 60000 <= boundary-last <= 240000:
        return None
    return tuple(range(last+60000, boundary, 60000))


def read_tails(db, requests, now, now_ms):
    """One snapshot and bounded SQL batches; no writes or HTTP ownership."""
    keys = {}
    for symbol, starts in requests.items():
        if len(starts)>3:
            raise ValueError('Too many missing closed minutes')
        for start in starts:
            keys[ws_key(symbol,start)] = (symbol,start,True)
            keys[identity(params_for(symbol,start))] = (symbol,start,False)
    rows = {}
    ids = list(keys)
    for offset in range(0,len(ids),200):
        batch=ids[offset:offset+200]
        rows.update(db.execute('SELECT k,value FROM cache WHERE host=? AND until>? AND k IN ('
                               +','.join('?' for _ in batch)+')',(HOST,now,*batch)).fetchall())
    output = {}
    # Prefer genuine WS finality; canonical REST is separately validated.
    for k,(symbol,start,is_ws) in keys.items():
        if k not in rows:
            continue
        try:
            data=json.loads(rows[k])
            row=validate(data,symbol,now_ms) if is_ws else rest_minute(data,symbol,start,now_ms)
            if row is not None and row['t']==start:
                output.setdefault(symbol,{}).setdefault(start,row)
        except (ValueError,TypeError,OverflowError):
            continue
    return output


def extend(previous, required, boundary, tail):
    starts=plan(previous,required,boundary)
    if starts is None or any(t not in tail for t in starts):
        return None
    bars=(list(previous)+[{k:tail[t][k] for k in ('t',*FIELDS)} for t in starts])[-required:]
    if (len(bars)!=required or bars[-1]['t']!=boundary-60000
            or any(b['t']-a['t']!=60000 for a,b in zip(bars,bars[1:]))):
        return None
    return bars


class ClosedCohort:
    """One read-only future, independently bounded, no trading/account writes."""
    def __init__(self, reader, *, recoverable=()):
        self.reader=reader
        self.recoverable=recoverable
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='closed-cohort')
        self.job=None
        self.boundary=None
        self.ready={}
        self.next_poll=0.
        self.delivered=0
        self.reads=0
        self.errors=0

    def poll(self, requirements, boundary, now):
        if boundary!=self.boundary:
            self.ready.clear()
            self.boundary=boundary
            self.next_poll=0.
        for symbol in tuple(self.ready):
            if symbol not in requirements or len(self.ready[symbol])<requirements[symbol]:
                self.ready.pop(symbol,None)
        fresh=set()
        if self.job is not None and self.job.done():
            job,self.job=self.job,None
            try:
                result=job.result()
            except self.recoverable:
                # A denial or unreadable snapshot is not proof of availability.
                self.errors+=1
                self.ready.clear()
            else:
                for symbol,bars in result.items():
                    if (symbol in requirements and len(bars)>=requirements[symbol]
                            and bars[-1]['t']==boundary-60000):
                        if symbol not in self.ready:
                            fresh.add(symbol)
                        self.ready[symbol]=bars
                self.delivered+=len(fresh)
        missing={s:n for s,n in requirements.items() if s not in self.ready}
        if self.job is None and missing and now>=self.next_poll:
            self.job=self.pool.submit(self.reader,missing)
            self.reads+=1
            self.next_poll=now+.5
        return fresh

    def close(self):
        self.pool.shutdown(wait=True,cancel_futures=True)
