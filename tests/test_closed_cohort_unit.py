import json, sqlite3, threading, time
from concurrent.futures import ThreadPoolExecutor
import pytest
from bcc.closed_cohort import plan, read_tails, extend, ClosedCohort
from bcc.closed_minutes import key, HOST
from bcc.hub_minute_repair import params_for, identity

B=60060000
S=B-60000
NOW=B+2000

def prev(last=S-60000, n=100):
    return [dict(t=last-(n-1-i)*60000,open=100.,high=103.,low=99.,close=100.,volume=1.) for i in range(n)]

def ws(symbol,t=S,price=102.):
    return dict(symbol=symbol,t=t,confirmed_ms=t+60000,open=100.,high=103.,low=99.,close=price,volume=1.)

def rest(symbol,t=S,price=101.):
    return dict(retCode=0,time=t+60001,result=dict(symbol=symbol,category='linear',list=[[str(t),'100','103','99',str(price),'2','200']]))

def store(db,k,row,expires=10.):
    db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?)',(k,json.dumps(row),expires,HOST));db.commit()

@pytest.fixture
def db(tmp_path):
    d=sqlite3.connect(tmp_path/'test.sqlite');d.execute('PRAGMA journal_mode=WAL');d.execute('CREATE TABLE cache(k TEXT PRIMARY KEY,value TEXT,until REAL,host TEXT)');yield d;d.close()

def test_portfolio_500_single_snapshot_bounded_queries(db):
    symbols=[f'COIN{i}USDT' for i in range(500)]
    for s in symbols:store(db,identity(params_for(s,S)),rest(s))
    calls=[];db.set_trace_callback(calls.append);db.execute('BEGIN');out=read_tails(db,{s:(S,) for s in symbols},1.,NOW);db.rollback()
    assert len(out)==500 and len([c for c in calls if c.startswith('SELECT')])==5
    assert all(extend(prev(),100,B,tail)[-1]['close']==101. for tail in out.values())

@pytest.mark.parametrize('mode',['wrong_symbol','wrong_minute','unclosed','expired','nan','future','ws_unclosed'])
def test_reject_bad_evidence(db,mode):
    s='BTCUSDT';d=rest(s);k=identity(params_for(s,S));expires=10.
    if mode=='wrong_symbol':d['result']['symbol']='ETHUSDT'
    elif mode=='wrong_minute':d['result']['list'][0][0]=str(S-60000)
    elif mode=='unclosed':d['time']=S+59999
    elif mode=='expired':expires=0.
    elif mode=='nan':d['result']['list'][0][4]='nan'
    elif mode=='future':d['time']=NOW+6000
    else:k=key(s,S);d=ws(s);d['confirmed_ms']=S+59998
    store(db,k,d,expires)
    assert read_tails(db,{s:(S,)},1.,NOW)=={}

def test_ws_preferred_and_repair_for_other_symbols(db):
    s='BTCUSDT';store(db,key(s,S),ws(s));store(db,identity(params_for(s,S)),rest(s));store(db,identity(params_for('ETHUSDT',S)),rest('ETHUSDT'))
    rows=read_tails(db,{s:(S,),'ETHUSDT':(S,)},1.,NOW)
    assert rows[s][S]['close']==102. and rows['ETHUSDT'][S]['close']==101.

def test_gap_never_fabricated(db):
    s='BTCUSDT';store(db,key(s,S),ws(s));p=prev(S-120000)
    rows=read_tails(db,{s:plan(p,100,B)},1.,NOW)
    assert extend(p,100,B,rows[s]) is None
    store(db,identity(params_for(s,S-60000)),rest(s,S-60000))
    assert extend(p,100,B,read_tails(db,{s:plan(p,100,B)},1.,NOW)[s])[-1]['t']==S

@pytest.mark.parametrize('last,n',[(S-240000,100),(S+60000,100),(S-60000,99)])
def test_no_cold_or_invalid_window(last,n):
    assert plan(prev(last,n),100,B) is None

def test_existing_gap_rejected():
    p=prev();p[80]['t']+=60000
    assert extend(p,100,B,{S:ws('BTCUSDT')}) is None

def test_singleflight_no_stale_handoff():
    entered=threading.Event();release=threading.Event();calls=[]
    def reader(req):
        calls.append(req);entered.set();release.wait(2);return {'BTCUSDT':prev(S)}
    c=ClosedCohort(reader)
    try:
        assert c.poll({'BTCUSDT':100},B,1.)==set();assert entered.wait(1.)
        for i in range(100):assert c.poll({'BTCUSDT':100},B,1.)==set()
        assert len(calls)==1
        release.set();c.job.result(2)
        assert c.poll({'BTCUSDT':100},B+60000,2.)==set();assert not c.ready
    finally:release.set();c.close()

def test_completion_independent_of_blocked_history_pool():
    event=threading.Event();history=ThreadPoolExecutor(max_workers=1);blocked=history.submit(event.wait,2.)
    c=ClosedCohort(lambda req:{s:prev(S) for s in req})
    try:
        req={f'COIN{i}USDT':100 for i in range(50)};c.poll(req,B,1.);c.job.result(1.)
        assert c.poll(req,B,1.1)==set(req);assert not blocked.done();assert len(c.ready)==50
        assert c.poll(req,B,2.)==set() and c.reads==1
    finally:event.set();history.shutdown();c.close()

def test_provider_failure_discards_ready():
    def reader(req):raise PermissionError('provider denied')
    c=ClosedCohort(reader,recoverable=(PermissionError,))
    try:
        c.poll({'BTCUSDT':100},B,1.)
        with pytest.raises(PermissionError):c.job.result(1.)
        c.ready['ETHUSDT']=prev(S);c.poll({'BTCUSDT':100,'ETHUSDT':100},B,1.1)
        assert not c.ready and c.errors==1
    finally:c.close()
