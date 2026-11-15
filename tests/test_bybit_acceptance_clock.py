"""Raw WS QA must use the same vetted trading UTC as the producer/worker."""
from types import SimpleNamespace

import pytest

from bcc import market_clock
from tools import bybit_ws_live_acceptance as qa


@pytest.fixture
def feed(monkeypatch):
    state = SimpleNamespace(now=10000., local_offset=-.85, verified=False, reads=0,
                            delay=0., old_mark=False, old_book=False)
    monkeypatch.setattr(qa.time, 'time', lambda: state.now + state.local_offset)
    def ensure():
        state.verified = True
    monkeypatch.setattr(qa, 'clock', SimpleNamespace(ensure=ensure))
    monkeypatch.setattr(market_clock, 'utc_bounds', lambda: (
        (state.now, state.now + .02) if state.verified
        else (state.now + state.local_offset,) * 2))

    class Source:
        def get(self, *args, **kwargs):
            state.reads += 1
            stamp = 10000. if state.old_book else state.now
            return {'_bcc_ws_book': True, 'result': {
                's': 'ADAUSDT', 'ts': int(stamp * 1000),
                'b': [['.4','100']], 'a': [['.41','100']]}}
        def _ws_position_mark(self, symbol):
            state.now += state.delay
            return {'at': 10000. if state.old_mark else state.now, 'bid': .405, 'ask': .405}
    return state, Source()


@pytest.mark.parametrize('offset', [-.85, -120., 120.])
def test_skewed_pc_clock_cannot_reject_fresh_independent_ws_data(feed, offset):
    state, source = feed
    state.local_offset = offset
    result = qa.probe(source, 'ADAUSDT', require_mark=True)
    assert result['status'] == 'PASS', result
    assert state.verified
    assert result['age_upper_seconds'] == .02
    assert result['mark_age_seconds'] == .02


def test_waiting_for_mark_refreshes_book_from_actual_cache(feed):
    state, source = feed
    state.delay = 4.
    result = qa.probe(source, 'ADAUSDT', require_mark=True)
    assert result['status'] == 'PASS', result
    assert state.reads == 2


@pytest.mark.parametrize('expired', ['old_mark', 'old_book'])
def test_expired_original_exchange_observations_still_fail(feed, expired):
    state, source = feed
    state.delay = 4.
    setattr(state, expired, True)
    result = qa.probe(source, 'ADAUSDT', require_mark=True)
    assert result['status'] == 'FAIL'
    assert 'vetted UTC' in result['error']
    assert max(result.get('mark_age_seconds',0),result.get('age_upper_seconds',0)) > 3


def test_missing_vetted_clock_blocks_probe_without_reading_data(feed, monkeypatch):
    state, source = feed
    def unavailable():
        raise market_clock.ClockUnavailable('No clock quorum')
    monkeypatch.setattr(qa, 'clock', SimpleNamespace(ensure=unavailable))
    result = qa.probe(source,'ADAUSDT',require_mark=True)
    assert result['status']=='FAIL' and state.reads == 0
