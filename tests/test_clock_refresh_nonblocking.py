"""A periodic public clock probe cannot stall a live WS maintenance lane."""
import threading
from types import SimpleNamespace

import pytest

from bcc import market_clock as mc


@pytest.fixture
def slow_clock(monkeypatch):
    timer={'wall':1031.,'mono':131.}
    monkeypatch.setattr(mc,'time',SimpleNamespace(
        time=lambda:timer['wall'],monotonic=lambda:timer['mono']))
    reference=mc.MarketClock()
    reference._reference=(1000.,1000.01,1000.,100.)
    reference._attempted=100.
    monkeypatch.setattr(mc,'clock',reference)
    entered,released=threading.Event(),threading.Event()
    state={'calls':0,'fail':False}
    lock=threading.Lock()
    def probe(provider):
        with lock:state['calls']+=1
        entered.set()
        assert released.wait(timeout=3)
        sample={'http_status':200,'cached_response':False,
                'local_clock_changed_during_request':False,
                'server_unix':timer['wall'], 'server_precision_seconds':.001,
                'round_trip_seconds':.01,'local_received_unix':timer['wall'],
                'received_monotonic':timer['mono']}
        return {'source':provider,'samples':[] if state['fail'] else [sample,sample]}
    monkeypatch.setattr(mc,'probe_source',probe)
    yield reference,timer,entered,released,state
    released.set()
    if reference._refresh_thread is not None:
        reference._refresh_thread.join(timeout=3)
        assert not reference._refresh_thread.is_alive()


def test_periodic_clock_network_io_does_not_block_prices_and_has_one_refresh(slow_clock):
    reference,timer,entered,released,state=slow_clock
    returned=threading.Event()
    caller=threading.Thread(target=lambda:(reference.ensure(),returned.set()))
    caller.start()
    try:
        assert entered.wait(timeout=1)
        assert returned.wait(timeout=1), 'The WS caller waited for network clock refresh'
        first=reference._refresh_thread
        for _ in range(100):
            reference.ensure()
            assert reference._refresh_thread is first
        low,high=reference.bounds()
        assert low<=timer['wall']<=high
        assert mc.book_is_fresh(timer['wall'])
        assert state['calls']<=2
    finally:
        released.set();caller.join(timeout=3)
    first.join(timeout=3)
    assert reference.evidence['status']=='PASS'
    assert reference._reference[3]==131.


@pytest.mark.parametrize('fault',['expiry','wall_jump'])
def test_pending_clock_probe_cannot_extend_authority_or_hide_a_pc_jump(slow_clock,fault):
    reference,timer,entered,released,state=slow_clock
    reference.ensure();assert entered.wait(timeout=1)
    original=reference._reference
    if fault=='expiry':timer.update(wall=1061.,mono=161.)
    else:timer['wall']+=1
    with pytest.raises(mc.ClockUnavailable):reference.bounds()
    assert not mc.book_is_fresh(timer['wall'])
    assert reference._reference==original


def test_failed_background_refresh_preserves_only_original_reference_and_cooldown(slow_clock):
    reference,timer,entered,released,state=slow_clock
    state['fail']=True;original=reference._reference
    reference.ensure();assert entered.wait(timeout=1)
    released.set();reference._refresh_thread.join(timeout=3)
    assert not reference._refresh_thread.is_alive()
    assert reference._reference==original and reference.evidence['status']=='UNAVAILABLE'
    attempts=state['calls'];reference.ensure();assert state['calls']==attempts
    timer.update(wall=1061.,mono=161.)
    with pytest.raises(mc.ClockUnavailable):reference.ensure()
    assert not mc.book_is_fresh(timer['wall'])


def test_cold_start_still_waits_for_an_independent_verified_quorum(slow_clock):
    reference,timer,entered,released,state=slow_clock
    reference._reference=None;reference._attempted=None
    returned=threading.Event()
    def start():reference.ensure();returned.set()
    caller=threading.Thread(target=start);caller.start()
    try:
        assert entered.wait(timeout=1)
        assert not returned.is_set()
        with pytest.raises(mc.ClockUnavailable):reference.bounds()
        assert reference._refresh_thread is None
    finally:
        released.set();caller.join(timeout=3)
    assert returned.is_set() and reference.evidence['status']=='PASS'
    assert reference.bounds()[0]>=timer['wall']
