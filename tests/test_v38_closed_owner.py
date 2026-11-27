"""Owner receipt authority and current-minute recovery ranking."""
from types import SimpleNamespace

import pytest

from bcc.closed_owner import ClosedOwnerReceipts, MINUTE, pending_ready

B = 1800000000000
NAMES = ['AXTIUSDT', 'GRIFFAINUSDT', 'BYDUSDT', 'KOUSDT']


def bars(boundary=B, count=100):
    return [dict(t=boundary-(count-i)*MINUTE) for i in range(count)]


def tick(symbol=NAMES[0], **changes):
    values = dict(symbol=symbol, bars=bars(), closed_history=True, live_history=True,
                  risk_only=False, history_denied=False, history_error='', book={'at': B/1000+1},
                  closed_decision_at=B/1000+2)
    values.update(changes)
    return SimpleNamespace(**values)


def order(receipts, **changes):
    values = dict(symbols=NAMES, eligible=set(NAMES), ready={s:bars() for s in NAMES},
                  boundary=B, sizes=dict.fromkeys(NAMES,100), histories={},
                  diagnostics={}, admissions={}, receipts=receipts)
    values.update(changes)
    return pending_ready(**values)


def test_first_committed_receipt_is_not_overwritten():
    r = ClosedOwnerReceipts()
    assert r.record(tick(), (B/1000+3, B/1000+3.25), 100)
    assert not r.record(tick(), (B/1000+4, B/1000+4.2), 100)
    assert r.acknowledged(NAMES[0],B)
    assert r.snapshot()['receipts'][str(B)][NAMES[0]] == dict(decision_ms=B+2000,commit_upper_ms=B+3250)
    assert order(r)==NAMES[1:]


@pytest.mark.parametrize('changes', [dict(closed_history=False), dict(live_history=False),
    dict(risk_only=True),dict(history_denied=True),dict(history_error='stale'),dict(book=None),
    dict(bars=bars(count=99)),dict(bars=bars(B-MINUTE)),dict(closed_decision_at=None),
    dict(closed_decision_at=B/1000-1),dict(closed_decision_at=B/1000+60)])
def test_non_authoritative_tick_has_no_receipt(changes):
    r=ClosedOwnerReceipts()
    assert not r.record(tick(**changes),(B/1000+3,B/1000+3.2),100)
    assert not r.snapshot()['receipts']


def test_commit_crossing_boundary_is_measured_but_not_timely():
    r=ClosedOwnerReceipts()
    assert r.record(tick(),(B/1000+60.1,B/1000+60.2),100)
    assert not r.acknowledged(NAMES[0],B)
    assert r.snapshot()['receipts'][str(B)][NAMES[0]]['commit_upper_ms']>=B+MINUTE


def test_bounded_history_and_clock_regression():
    r=ClosedOwnerReceipts()
    for i in range(8):
        b=B+i*MINUTE
        assert r.record(tick(bars=bars(b),closed_decision_at=b/1000+1),(b/1000+2,b/1000+2),100)
    assert len(r.snapshot()['receipts'])==4
    r.advance(B)
    assert not r.snapshot()['receipts']


def test_all_current_symbols_rank_including_scans_before_error():
    r=ClosedOwnerReceipts()
    assert order(r)==NAMES
    assert order(None)==[]  # preserve prior diagnostic-only policy for adapters
    d={NAMES[-1]:[dict(section='Свечи',reason='stale')]}
    assert order(r,diagnostics=d,admissions={NAMES[0]:10})==[NAMES[-1],NAMES[1],NAMES[2],NAMES[0]]
    assert r.record(tick(NAMES[-1]),(B/1000+3,B/1000+3),100)
    assert order(r)==NAMES[:-1]


def test_recovery_filters_denial_short_old_and_ineligible():
    r=ClosedOwnerReceipts()
    ready={s:bars() for s in NAMES}
    ready[NAMES[1]]=bars(count=99)
    ready[NAMES[2]]=bars(B-MINUTE)
    assert order(r,ready=ready,histories={NAMES[0]:tick(history_denied=True)},eligible=set(NAMES[:-1]))==[]


@pytest.mark.parametrize('inventory', [50, 831])
def test_ready_scanner_without_error_gets_bounded_admission(inventory, monkeypatch):
    from bcc import scheduler
    from bcc.closed_cohort import ClosedCohort
    from threading import Event
    monkeypatch.setenv('BCC_BYBIT_PORTFOLIO_HISTORY_CANDIDATE','1')
    monkeypatch.setenv('BCC_BYBIT_MAINTAINED_MARK_CANDIDATE','1')
    monkeypatch.setattr(scheduler,'market_now',lambda:B/1000+5)
    names=[f'C{i}USDT' for i in range(inventory)]
    target=names[-1]
    release=Event()
    calls=[]
    class Source:
        def closed_candles(self,symbol,required,*,priority=False):
            calls.append(symbol)
            assert release.wait(5)
            return bars()
        def cached_closed_cohort(self,requirements):
            return {s:bars() for s in requirements}
        def orderbook(self,symbol):
            return {'at':B/1000+5,'bids':[[100,1]],'asks':[[101,1]]}
    feed=Source()
    s=scheduler.MarketScheduler(feed,{'strategy':'VELES','symbols':names},lambda *a:100,workers=2,period=2)
    s.enable_closed_owner_tracking()
    state=dict(positions={},pending={},funding_archive={},diagnostics={},marks={})
    s._closed_sizes=dict.fromkeys(names,100)
    cohort=ClosedCohort(feed.cached_closed_cohort)
    cohort.boundary=B
    cohort.ready=feed.cached_closed_cohort(s._closed_sizes)
    s._closed_cohort=cohort
    for name in names[:-1]:
        assert s._closed_owner.record(tick(name),(B/1000+3,B/1000+3.2),100)
    try:
        s.collect(state,timeout=.01)
        assert target in s.jobs
        assert names[0] in s.jobs  # ordinary discovery is not starved
        assert len(s.jobs)==s.capacity==2
        assert s.last_pressure['closed_recovery_scan_admissions']==1
        assert not state['diagnostics'] and not s._closed_owner.acknowledged(target,B)
    finally:
        release.set()
        s.close()


def test_closed_history_provenance_survives_scheduler_handoff(monkeypatch):
    from bcc import scheduler
    monkeypatch.setenv('BCC_BYBIT_PORTFOLIO_HISTORY_CANDIDATE','1')
    monkeypatch.setattr(scheduler,'market_now',lambda:B/1000+5)
    s=scheduler.MarketScheduler(SimpleNamespace(),{'strategy':'VELES','symbols':NAMES},lambda *a:100,workers=2)
    s.enable_closed_owner_tracking()
    s._closed_sizes={NAMES[0]:100}
    s._closed_cohort=SimpleNamespace(boundary=B,ready={NAMES[0]:bars()},close=lambda:None)
    old=scheduler.MarketTick(NAMES[0],bars(B-MINUTE),None,[],{},history_error='stale',live_history=True)
    try:
        s._save_history_result(NAMES[0],old,{NAMES[0]}, {})
        saved=s._history_results[NAMES[0]]
        assert saved.closed_history and saved.bars[-1]['t']==B-MINUTE
        assert not s._closed_owner.acknowledged(NAMES[0],B)
    finally:
        s.close()


@pytest.mark.parametrize('fail_transaction', [False, True])
def test_runtime_sets_receipt_provenance_only_after_committed_strategy(tmp_path, monkeypatch, fail_transaction):
    import time
    from bcc.config import defaults, range_section
    from bcc.runtime import process_tick
    from bcc.scheduler import MarketTick, MarketScheduler
    from bcc.storage import Store
    from bcc.market_clock import clock
    from test_core import book
    cfg=defaults('VELES')
    cfg['symbols']=['BTCUSDT']
    cfg['sections']=[range_section(30,40)]
    store=Store(tmp_path,cfg['capital'])
    now=time.time()
    boundary=int(now//60)*MINUTE
    # Keep the synthetic decision far from a real minute rollover.
    decision=boundary/1000+5
    monkeypatch.setattr('bcc.runtime.market_now',lambda:decision)
    monkeypatch.setattr(clock,'bounds',lambda:(decision+1,decision+1.1))
    monkeypatch.setattr('bcc.runtime.book_is_fresh',lambda stamp:True)
    monkeypatch.setenv('BCC_BYBIT_PORTFOLIO_HISTORY_CANDIDATE','1')
    candle=MarketTick('BTCUSDT',bars(boundary),book(),[],{},live_history=True,closed_history=True)
    class Engine:
        def process(self,*args):
            assert candle.closed_decision_at is None
            if fail_transaction:
                raise RuntimeError('rollback fixture')
    s=MarketScheduler(SimpleNamespace(),cfg,lambda *a:100,workers=1)
    s.enable_closed_owner_tracking()
    s._closed_sizes={'BTCUSDT':100}
    try:
        if fail_transaction:
            with pytest.raises(RuntimeError,match='rollback fixture'):
                process_tick(cfg,store,Engine(),candle)
            assert candle.closed_decision_at is None
            assert not s.record_closed_owner(candle)
        else:
            process_tick(cfg,store,Engine(),candle)
            assert candle.closed_decision_at==decision
            assert s.record_closed_owner(candle)
            assert s._closed_owner.acknowledged('BTCUSDT',boundary)
        store.audit()
        assert not store.history()
    finally:
        s.close()


def test_owner_snapshot_requires_verified_clock(monkeypatch):
    from bcc.scheduler import MarketScheduler
    from bcc.market_clock import clock
    monkeypatch.setenv('BCC_BYBIT_PORTFOLIO_HISTORY_CANDIDATE','1')
    s=MarketScheduler(SimpleNamespace(),{'strategy':'VELES','symbols':NAMES},lambda *a:100,workers=1)
    s.enable_closed_owner_tracking()
    try:
        def unavailable():
            raise ValueError('unverified clock')
        monkeypatch.setattr(clock,'bounds',unavailable)
        assert s.closed_owner_snapshot()['clock_valid'] is False
        monkeypatch.setattr(clock,'bounds',lambda:(B/1000+5,B/1000+5.2))
        proof=s.closed_owner_snapshot()
        assert proof['clock_valid'] is True and proof['boundary']==B
        assert proof['clock_low_ms']==B+5000 and proof['clock_high_ms']==B+5200
    finally:
        s.close()
