"""Audit must not confuse concurrent PAPER commits with damaged history."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

from bcc.storage import Store


def _cash_event(store, number):
    with store.transaction(include_charts=False) as (state, events):
        state["cash"] -= 0.1
        events.append({
            "id": f"event-{number}", "at": time.time(),
            "symbol": "BTC", "section": "test", "action": "FEE",
            "cash_delta": -0.1, "net_pnl": 0,
        })


def test_audit_ignores_stale_external_read_and_uses_single_snapshot(tmp_path, monkeypatch):
    store = Store(tmp_path, 500)
    original_read = store.read
    def simulate_bad_two_connection_read(*args, **kwargs):
        old_state = original_read(*args, **kwargs)
        _cash_event(Store(tmp_path), 1)
        return old_state
    # Any external stale read must not affect audit: it must own one snapshot.
    monkeypatch.setattr(store, "read", simulate_bad_two_connection_read)
    store.audit()
    assert Store(tmp_path).read()["cash"] == 500


def test_read_and_audit_remain_consistent_with_concurrent_writer(tmp_path):
    writer_store = Store(tmp_path, 500)
    reader_store = Store(tmp_path)
    started = Event()
    def writer():
        started.set()
        for i in range(40):
            _cash_event(writer_store, i)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        assert started.wait(timeout=3)
        for _ in range(75):
            state = reader_store.read(include_charts=False)
            assert isinstance(state["cash"], (int,float))
            reader_store.audit()
        future.result(timeout=20)
    reader_store.audit()
    assert len(reader_store.history()) == 40
    assert abs(reader_store.read()["cash"] - 496) < 1e-7
