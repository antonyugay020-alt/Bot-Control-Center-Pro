"""System admission invariants: priority must not defeat independent progress."""
from threading import Event
from time import monotonic, sleep
from bcc.fleet import Fleet


def _wait_for(predicate, timeout=3):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return True
        sleep(.005)
    return False


def test_urgent_followup_for_active_owner_does_not_starve_other_owner():
    entered, release, independent = Event(), Event(), Event()
    fleet = Fleet(workers=2, max_pending=3)
    try:
        fleet.register('running', lambda: (entered.set(), release.wait(3)))
        fleet.register('other', lambda: independent.set())
        assert fleet.request('running')
        assert entered.wait(2)
        assert fleet.request('running', urgent=True)
        assert fleet.request('other')
        assert independent.wait(2), 'idle worker must run independent ordinary owner'
    finally:
        release.set()
        fleet.close()


def test_urgent_runnable_owner_precedes_ordinary_after_slot_frees():
    entered, release = Event(), Event()
    seen = []
    fleet = Fleet(workers=1, max_pending=3)
    try:
        fleet.register('running', lambda: (entered.set(), release.wait(3)))
        fleet.register('normal', lambda: seen.append('normal'))
        fleet.register('urgent', lambda: seen.append('urgent'))
        assert fleet.request('running')
        assert entered.wait(2)
        assert fleet.request('normal')
        assert fleet.request('urgent', urgent=True)
        release.set()
        assert _wait_for(lambda: len(seen)==2)
        assert seen == ['urgent', 'normal']
    finally:
        release.set()
        fleet.close()
