"""End-to-end Hub priority propagation into the shared HTTP admission boundary.

Offline PAPER-only checks: synthetic PublicData, no provider HTTP or orders.
The queue's urgent flag must reach the producer thread-local context in
synchronous, bounded parallel, and nonblocking modes. Otherwise the shared
Bybit cache treats open-position books as background scans and exhausts the
reserved capacity before a position can be marked.
"""
import threading
import time

import pytest

from bcc.market import PublicData
from bcc.market_hub import MarketDataHub


URL = "https://api.bybit.com/v5/market/orderbook"
URGENT = {"category": "linear", "symbol": "BTCUSDT"}
REGULAR = {"category": "linear", "symbol": "ETHUSDT"}


class RecordingPublicData(PublicData):
    def __init__(self):
        super().__init__()
        self.records = []
        self.records_lock = threading.Lock()
        self.fail = False

    def get(self, url, params=None, ttl=0):
        record = (
            params["symbol"],
            bool(getattr(self._thread, "position_mark", False)),
            self._position_critical_request(url, params),
        )
        with self.records_lock:
            self.records.append(record)
        if self.fail:
            raise RuntimeError("synthetic upstream failure")
        return {"ok": True}


@pytest.mark.parametrize("mode", ["synchronous", "parallel", "nonblocking"])
def test_hub_preserves_urgent_position_mark_in_all_producer_modes(
    tmp_path, monkeypatch, mode,
):
    monkeypatch.delenv("BCC_PUBLIC_CACHE", raising=False)
    monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)
    source = RecordingPublicData()
    hub = MarketDataHub(
        tmp_path / "hub.sqlite", source,
        max_workers=1 if mode == "synchronous" else 2,
    )
    try:
        urgent_key = hub.registry.subscribe(URL, URGENT, priority=True)
        regular_key = hub.registry.subscribe(URL, REGULAR)
        if mode == "nonblocking":
            completed = []
            deadline = time.monotonic() + 5
            while len(completed) < 2 and time.monotonic() < deadline:
                completed.extend(hub.poll_nonblocking())
                time.sleep(.01)
        else:
            completed = hub.run_once(limit=2)

        assert {key for key, ok in completed if ok} == {
            urgent_key, regular_key,
        }
        assert sorted(source.records) == [
            ("BTCUSDT", True, True),
            ("ETHUSDT", False, False),
        ]
        assert not getattr(source._thread, "position_mark", False)
    finally:
        hub.close()


def test_hub_priority_context_restores_previous_value_even_on_failure(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("BCC_PUBLIC_CACHE", raising=False)
    source = RecordingPublicData()
    hub = MarketDataHub(tmp_path / "hub.sqlite", source)
    try:
        source.fail = True
        source._thread.position_mark = True
        with pytest.raises(RuntimeError, match="synthetic upstream"):
            hub._fetch_public(URL, REGULAR, urgent=False)
        assert source._thread.position_mark is True

        source._thread.position_mark = False
        with pytest.raises(RuntimeError, match="synthetic upstream"):
            hub._fetch_public(URL, URGENT, urgent=True)
        assert source._thread.position_mark is False
        assert source.records == [
            ("ETHUSDT", False, False),
            ("BTCUSDT", True, True),
        ]
    finally:
        hub.close()
