"""A ready portfolio cohort must reach the owner via the existing quote lane."""
from concurrent.futures import Future
from threading import Event
from types import SimpleNamespace
import time

import pytest

from bcc import scheduler
from bcc.closed_cohort import ClosedCohort

BOUNDARY = 1800000000000
BAD = ('AXTIUSDT', 'GRIFFAINUSDT', 'BYDUSDT')
NAMES = [f'HELD{i}USDT' for i in range(43)] + list(BAD) + [f'SCAN{i}USDT' for i in range(4)]


def bars(last, size=100):
    return [dict(t=last-(size-1-i)*60000, open=100., high=103., low=99.,
                 close=101., volume=1.) for i in range(size)]


class Feed:
    def __init__(self):
        self.release = Event()
        self.called = []

    def position_orderbook(self, symbol):
        self.called.append(symbol)
        return {'at': BOUNDARY/1000+5., 'bids': [[100., 10]], 'asks': [[101., 10]]}

    orderbook = position_orderbook

    def closed_candles(self, symbol, required, *, priority=False):
        assert self.release.wait(10)
        return bars(BOUNDARY-120000, required)

    def cached_closed_cohort(self, requirements):
        return {s: bars(BOUNDARY-60000, n) for s,n in requirements.items()}

    def position_candles(self, symbol, required):
        return self.closed_candles(symbol, required, priority=True)

    def funding(self, symbol):
        return []

    def risk_maintenance_enabled(self):
        return False


@pytest.fixture
def fleet(monkeypatch):
    monkeypatch.setenv('BCC_BYBIT_PORTFOLIO_HISTORY_CANDIDATE', '1')
    monkeypatch.setenv('BCC_BYBIT_MAINTAINED_MARK_CANDIDATE', '1')
    monkeypatch.setattr(scheduler, 'market_now', lambda: BOUNDARY/1000+5.)
    monkeypatch.setattr(scheduler, 'book_is_fresh', lambda at: at == BOUNDARY/1000+5.)
    monkeypatch.setattr(scheduler, 'quote_is_recent', lambda *args, **kwargs: True)
    feed = Feed()
    s = scheduler.MarketScheduler(feed, {'strategy':'VELES','symbols':NAMES},
                                  lambda *args:100, workers=8, period=2)
    state = dict(positions={name:dict(symbol=name,opened=BOUNDARY/1000-600.) for name in NAMES[:46]},
                 pending={}, funding_archive={}, marks={}, margin_data={},
                 diagnostics={name:[dict(section='Свечи',reason='Устаревшие свечи')] for name in BAD})
    s._closed_sizes = dict.fromkeys(NAMES, 100)
    s._quoted_once.update(NAMES[:46])
    for name in NAMES:
        s._history_results[name] = scheduler.MarketTick(
            name, bars(BOUNDARY-120000), None, [], {},
            history_error='Устаревшие свечи' if name in BAD else '', live_history=True)
    # A completed genuine cohort future in the scheduler; no exchange traffic.
    cohort = ClosedCohort(feed.cached_closed_cohort)
    cohort.boundary = BOUNDARY
    cohort.job = Future()
    cohort.job.set_result(feed.cached_closed_cohort(s._closed_sizes))
    s._closed_cohort = cohort
    try:
        yield s, state, feed
    finally:
        feed.release.set()
        s.close()


def test_ready_cohort_stages_history_without_acknowledging_owner(fleet):
    s,state,feed = fleet
    h = s._history_results[BAD[0]]
    rates,marks,margin = [{'at':1}], {1:100.}, {'risk_at':1.}
    h.rates, h.funding_marks, h.margin_data = rates,marks,margin
    s._poll_closed_cohort(state,set(NAMES),set(NAMES[:46]))
    assert len(s._closed_cohort.ready) == 50
    assert s._closed_staged == 50 and s._closed_handoffs == 0
    assert h.bars[-1]['t'] == BOUNDARY-60000 and not h.history_error
    assert h.book is None and h.rates is rates and h.funding_marks is marks and h.margin_data is margin
    assert set(state['diagnostics']) == set(BAD)
    assert s._closed_recovery_order(state,NAMES[:46],set(NAMES)) == list(BAD)


def test_46_exposures_recovered_errors_receive_real_quote_tasks(fleet):
    s,state,feed = fleet
    recovered = set()
    ordinary = set()
    deadline = time.monotonic()+3.
    # The owner fixture acknowledges only a delivered tick with a new book
    # and the exact closed minute. Scheduler must not edit diagnostics itself.
    while time.monotonic() < deadline and (recovered != set(BAD) or not ordinary):
        for name,tick,error in s.collect(state,timeout=.02):
            assert error is None
            if tick and tick.book and tick.bars and not tick.history_error:
                assert tick.bars[-1]['t'] == BOUNDARY-60000
                if name in BAD:
                    recovered.add(name)
                    state['diagnostics'].pop(name,None)
                else:
                    ordinary.add(name)
    assert recovered == set(BAD) and ordinary
    assert not feed.release.is_set()
    assert s._closed_recovery_sequence >= 3
    assert s.quote_capacity == 4 and len(s.quote_jobs) <= 4
    assert not state['diagnostics']


def test_recovery_quote_retry_can_run_while_same_history_job_is_blocked(fleet):
    s,state,feed = fleet
    symbol = BAD[0]
    s._poll_closed_cohort(state,set(NAMES),set(NAMES[:46]))
    history = Future()
    s.jobs[symbol] = history
    s._history_only.add(symbol)
    s.retry_tick(scheduler.MarketTick(symbol,bars(BOUNDARY-120000),{'at':0.},[],{},
                                      history_error='Устаревшие свечи',live_history=True))
    delivered = None
    deadline = time.monotonic()+2.
    try:
        while time.monotonic()<deadline and delivered is None:
            for name,tick,error in s.collect(state,timeout=.02):
                assert error is None
                if name==symbol and tick.refresh_attempted:
                    delivered=tick
        assert delivered is not None and delivered.book['at']==BOUNDARY/1000+5.
        assert delivered.bars[-1]['t']==BOUNDARY-60000 and not delivered.history_error
        assert not history.done()
    finally:
        history.cancel()
        s.jobs.pop(symbol,None)
        s._history_only.discard(symbol)


@pytest.mark.parametrize('damage', ['denied','old_boundary','old_tail','short','intrabar'])
def test_recovery_priority_and_staging_do_not_bypass_history_authority(fleet, damage):
    s,state,feed = fleet
    symbol=BAD[0]
    s._poll_closed_cohort(state,set(NAMES),set(NAMES[:46]))
    h=s._history_results[symbol]
    h.bars=bars(BOUNDARY-120000)
    h.history_error='Устаревшие свечи'
    eligible=set(NAMES)
    if damage=='denied':
        h.history_denied=True
    elif damage=='old_boundary':
        s._closed_cohort.boundary-=60000
    elif damage=='old_tail':
        s._closed_cohort.ready[symbol]=bars(BOUNDARY-120000)
    elif damage=='short':
        s._closed_cohort.ready[symbol]=bars(BOUNDARY-60000,99)
    else:
        eligible.remove(symbol)
    assert symbol not in s._closed_recovery_order(state,NAMES[:46],eligible)
    assert not s._apply_closed_cohort([(symbol,h,None)],eligible,state,staged=True)
    assert h.history_error and h.bars[-1]['t']==BOUNDARY-120000
    assert symbol in state['diagnostics']


def test_recovery_order_rotates_until_owner_acknowledges(fleet):
    s,state,feed=fleet
    s._poll_closed_cohort(state,set(NAMES),set(NAMES[:46]))
    assert s._closed_recovery_order(state,NAMES[:46],set(NAMES))==list(BAD)
    s._note_recovery_admission(BAD[0])
    assert s._closed_recovery_order(state,NAMES[:46],set(NAMES))==[BAD[1],BAD[2],BAD[0]]
    state['diagnostics'].pop(BAD[1])
    assert s._closed_recovery_order(state,NAMES[:46],set(NAMES))==[BAD[2],BAD[0]]


def test_single_quote_slot_retains_regular_admission_under_recovery_load(fleet):
    s,state,feed=fleet
    s.quote_capacity=1
    for name in NAMES[:46]:
        state['diagnostics'][name]=[dict(section='Свечи',reason='stale')]
    # Keep all failures pending: ordinary rotation must nevertheless advance.
    deadline=time.monotonic()+2.
    while time.monotonic()<deadline and len(feed.called)<8:
        s.collect(state,timeout=.01)
    assert len(feed.called)>=8
    assert s._closed_recovery_sequence>=2
    assert s._closed_recovery_sequence<len(feed.called)
    assert s.quote_cursor>0


def test_first_recovery_uses_existing_capacity_before_long_round_robin(fleet):
    s,state,feed=fleet
    completed=s.collect(state,timeout=.02)
    admitted=set(s.quote_jobs) | set(feed.called)
    admitted.update(name for name,tick,error in completed if tick is not None and tick.book)
    assert BAD[0] in admitted
    assert admitted-set(BAD)
    assert len(admitted)<=s.quote_capacity
