import threading
import pytest
from bcc.rest_mark_fallback import build_snapshot, index_snapshot, RestMarkPump

NOW=10000.
STAMP=10000000

def fresh(t):return -.5<=NOW-t<=3

def payload(rows=None, **kw):
    return dict(retCode=0,time=STAMP,result=dict(category='linear',list=rows if rows is not None else [dict(symbol='BTCUSDT',markPrice='100',bid1Price='97',ask1Price='103')]),**kw)

def test_snapshot_uses_only_exchange_mark_and_timestamp():
    value=build_snapshot(payload(),fresh)
    row=index_snapshot(value,fresh)['BTCUSDT']
    assert row==dict(bid=100.,ask=100.,at=NOW,mark_basis='rest_snapshot')
    assert index_snapshot(value,lambda t:NOW+3.001-t<=3)=={}

@pytest.mark.parametrize('case',['wrong_category','bad_code','bool_code','stale','future','bool_time','ws','empty','wrong_time'])
def test_invalid_envelope(case):
    p=payload()
    if case=='wrong_category':p['result']['category']='spot'
    elif case=='bad_code':p['retCode']=10006
    elif case=='bool_code':p['retCode']=False
    elif case=='stale':p['time']-=3001
    elif case=='future':p['time']+=501
    elif case=='bool_time':p['time']=True
    elif case=='ws':p['_bcc_ws_mark']=True
    elif case=='empty':p['result']['list']=[]
    else:p['time']=str(STAMP)
    assert build_snapshot(p,fresh) is None

@pytest.mark.parametrize('price',[True,0,-1,'NaN','Inf','',None])
def test_invalid_mark_never_uses_bid_ask(price):
    assert build_snapshot(payload([dict(symbol='BTCUSDT',markPrice=price,bid1Price='100',ask1Price='101')]),fresh) is None


def test_duplicate_and_wrong_symbol_excluded_without_harming_others():
    rows=[dict(symbol='BTCUSDT',markPrice='100'),dict(symbol='BTCUSDT',markPrice='101'),dict(symbol='ETHUSDT',markPrice='20'),dict(symbol='bad',markPrice='99')]
    assert set(index_snapshot(build_snapshot(payload(rows),fresh),fresh))=={'ETHUSDT'}


def test_500_symbols_one_snapshot():
    rows=[dict(symbol=f'C{i}USDT',markPrice=str(i+1)) for i in range(500)]
    assert len(index_snapshot(build_snapshot(payload(rows),fresh),fresh))==500


def test_pump_no_overlap_no_work_without_admission_and_bounded_cadence():
    now=[1.];entered=threading.Event();release=threading.Event();calls=[]
    def fetch():
        calls.append(1);entered.set();assert release.wait(3);return True
    p=RestMarkPump(fetch,monotonic=lambda:now[0])
    try:
        p.poll(False);assert not calls and p.job is None
        p.poll(True);assert entered.wait(1)
        now[0]+=10
        for _ in range(100):p.poll(True)
        assert len(calls)==1
        release.set();p.job.result(2);p.poll(False)
        assert p.completed==1 and p.job is None
        p.poll(True);p.job.result(2);p.poll(True)
        assert p.completed==2 and len(calls)==2 and p.job is None
    finally:release.set();p.close()


def test_pump_recoverable_failure_does_not_create_price():
    now=[0.]
    def fetch():raise ValueError('no price')
    p=RestMarkPump(fetch,recoverable=(ValueError,),monotonic=lambda:now[0])
    try:
        p.poll(True)
        with pytest.raises(ValueError):p.job.result(2)
        p.poll(True)
        assert p.errors==1 and p.completed==0 and p.job is None
    finally:p.close()
