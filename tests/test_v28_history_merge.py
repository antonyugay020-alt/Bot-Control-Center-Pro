"""Late history jobs cannot replace already verified current cohort evidence."""
from types import SimpleNamespace

import pytest

from bcc import scheduler

BOUNDARY = 60060000
SYMBOL = 'BTCUSDT'


def bars(last):
    return [dict(t=last-(99-i)*60000, open=100., high=103., low=99.,
                 close=101., volume=1.) for i in range(100)]


@pytest.mark.parametrize('kind', ['current', 'denied', 'old_boundary'])
def test_late_history_result_reconciles_current_evidence_without_replacing_other_planes(monkeypatch, kind):
    monkeypatch.setenv('BCC_BYBIT_PORTFOLIO_HISTORY_CANDIDATE', '1')
    monkeypatch.setattr(scheduler, 'market_now', lambda: BOUNDARY/1000+2.)
    s = scheduler.MarketScheduler(SimpleNamespace(), {'strategy':'VELES','symbols':[SYMBOL]},
                                  lambda *a:100, workers=2)
    current = bars(BOUNDARY-60000)
    prior = bars(BOUNDARY-120000)
    s._closed_cohort = SimpleNamespace(boundary=BOUNDARY-(60000 if kind=='old_boundary' else 0),
                                      ready={SYMBOL:current}, close=lambda:None)
    s._closed_sizes = {SYMBOL:100}
    s._closed_handoffs = 0
    rates, funding, margin = [], {}, {'risk_at':BOUNDARY/1000-5.}
    tick = scheduler.MarketTick(SYMBOL, prior, {'at':BOUNDARY/1000+1.}, rates, funding,
                                history_error='stale', margin_data=margin, live_history=True,
                                history_denied=kind=='denied')
    try:
        s._save_history_result(SYMBOL, tick, {SYMBOL}, {})
        saved = s._history_results[SYMBOL]
        assert saved.book is None and saved.rates is rates and saved.funding_marks is funding
        assert saved.margin_data is margin and saved.live_history
        if kind=='current':
            assert saved.bars is current and not saved.history_error and s._closed_handoffs==1
        else:
            assert saved.bars is prior and saved.history_error=='stale' and s._closed_handoffs==0
            assert saved.history_denied==(kind=='denied')
    finally:
        s.close()
