"""Account owner consumes real closed-cohort completions, not old ticks."""
from concurrent.futures import Future

import pytest

from bcc import scheduler
from bcc.closed_cohort import ClosedCohort
from bcc.market import SourceAccessDenied

BOUNDARY = 60060000
SYMBOL = 'BTCUSDT'


def bars(last, n=100):
    return [dict(t=last-(n-i-1)*60000, open=100., high=103., low=99.,
                 close=101., volume=1.) for i in range(n)]


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv('BCC_BYBIT_PORTFOLIO_HISTORY_CANDIDATE', '1')
    monkeypatch.setenv('BCC_BYBIT_MAINTAINED_MARK_CANDIDATE', '1')
    now = [BOUNDARY/1000+2.]
    monkeypatch.setattr(scheduler, 'market_now', lambda: now[0])
    class Source:
        def closed_candles(self, *a, **kw):
            pytest.fail('owner performed synchronous history fetch')
        def cached_closed_cohort(self, req):
            return {}
    state = {'positions':{'p':{'symbol':SYMBOL}}, 'pending':{}, 'marks':{}}
    cfg = {'strategy':'VELES', 'symbols':[SYMBOL]}
    s = scheduler.MarketScheduler(Source(), cfg, lambda *a:100, workers=2)
    s._closed_sizes[SYMBOL] = 100
    s._closed_cohort = ClosedCohort(s.source.cached_closed_cohort,
                                   recoverable=(SourceAccessDenied,))
    s._closed_cohort.boundary = BOUNDARY
    s._closed_cohort.next_poll = float('inf')
    try:
        yield s, state, now
    finally:
        s.close()


def test_completed_cohort_between_collect_and_owner_is_admitted(setup):
    s, state, _ = setup
    c = s._closed_cohort
    c.job = Future()
    c.job.set_result({SYMBOL:bars(BOUNDARY-60000)})
    book = {'at':BOUNDARY/1000+1.}
    risk = {'risk_at':BOUNDARY/1000-20.}
    tick = scheduler.MarketTick(SYMBOL, bars(BOUNDARY-120000), book, [], {},
                                margin_data=risk, live_history=True)
    assert s.refresh_closed_handoff(tick, lambda:state)
    assert tick.bars[-1]['t'] == BOUNDARY-60000
    assert tick.book is book and tick.margin_data is risk
    assert not tick.history_error
    assert s._closed_handoffs == 1
    assert not s.refresh_closed_handoff(tick, lambda:pytest.fail('current history was reread'))


@pytest.mark.parametrize('kind', ['denied','risk','intrabar','old_boundary','short','provider_error'])
def test_owner_never_overrides_protection_or_wrong_boundary(setup, kind):
    s, state, now = setup
    c = s._closed_cohort
    c.job = Future()
    current = bars(BOUNDARY-60000, 99 if kind=='short' else 100)
    if kind == 'provider_error':
        c.job.set_exception(SourceAccessDenied('api.bybit.com',403))
    else:
        c.job.set_result({SYMBOL:current})
    if kind == 'intrabar':
        state['positions']['p']['exit_signal'] = {'trigger':'intrabar'}
    if kind == 'old_boundary':
        now[0] += 60.
    tick = scheduler.MarketTick(SYMBOL, [], None, [], {}, history_error='stale',
                                live_history=True, history_denied=kind=='denied',
                                risk_only=kind=='risk')
    assert not s.refresh_closed_handoff(tick, lambda:state)
    assert tick.bars == [] and tick.history_error == 'stale'
