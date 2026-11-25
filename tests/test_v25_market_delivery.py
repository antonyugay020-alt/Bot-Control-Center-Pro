"""The mark controller progresses independently of a stalled Hub scheduler."""
import threading
import time
from bcc.rest_mark_fallback import RestMarkPump


def test_independent_control_obeys_ready_and_continues_without_hub_polls():
    ready=threading.Event()
    fetched=threading.Event()
    calls=[]
    def fetch():
        calls.append(time.monotonic())
        if len(calls)>=2:
            fetched.set()
        return True
    pump=RestMarkPump(fetch,period=.65)
    try:
        pump.start(ready.is_set)
        time.sleep(.15)
        assert not calls
        ready.set()
        assert fetched.wait(3)
        ready.clear()
        assert calls[1]-calls[0]>=.65
        count=len(calls)
        time.sleep(.8)
        assert len(calls)==count
        pump.check()
        assert pump.stats()['independent_control']
    finally:
        pump.close()


def test_independent_control_surfaces_unexpected_failure():
    pump=RestMarkPump(lambda: True)
    def broken():
        raise AssertionError('control contract')
    try:
        pump.start(broken)
        pump._runner.join(2)
        assert not pump._runner.is_alive()
        import pytest
        with pytest.raises(RuntimeError) as exc:
            pump.check()
        assert isinstance(exc.value.__cause__,AssertionError)
    finally:
        pump.close()
