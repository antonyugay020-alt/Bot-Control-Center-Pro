import time
from bcc.scheduler import MarketScheduler
from bcc.strategy_dispatch import StrategyDispatcher


def test_market_scheduler_uses_bounded_dispatcher_for_real_fetch():
    class Source:
        def candles(self, symbol, required):
            return [{"symbol": symbol}]
        def orderbook(self, symbol):
            return {"at": time.time(), "bids": [[100, 1]], "asks": [[101, 1]]}
        def funding(self, symbol):
            return []
    scheduler = MarketScheduler(Source(), {"symbols": ["BTCUSDT", "ETHUSDT"]}, lambda *a: 20, workers=2, period=0)
    state = {"positions": {}, "pending": {}, "funding_archive": {}}
    try:
        assert isinstance(scheduler.pool, StrategyDispatcher)
        got = []
        for _ in range(20):
            got += scheduler.collect(state, timeout=.1)
            if len(got) == 2:
                break
        assert {symbol for symbol, tick, error in got if error is None and tick.bars} == {"BTCUSDT", "ETHUSDT"}
        assert not scheduler.pool._pending
    finally:
        scheduler.close()


def test_scheduler_services_pending_symbol_not_in_config_or_open_positions():
    class Source:
        def candles(self, symbol, required):
            return [{"symbol": symbol}]
        def orderbook(self, symbol):
            return {"at": time.time(), "bids": [[100, 1]], "asks": [[101, 1]]}
        def funding(self, symbol):
            return []

    scheduler = MarketScheduler(Source(), {"symbols": ["BTCUSDT"]}, lambda *a: 20, workers=1, period=0)
    state = {"positions": {}, "pending": {"exit-1": {"symbol": "ETHUSDT", "opened": 0}}, "funding_archive": {}}
    try:
        got = []
        for _ in range(30):
            got.extend(scheduler.collect(state, timeout=.05))
            if any(symbol == "ETHUSDT" for symbol, _, _ in got):
                break
        assert any(symbol == "ETHUSDT" and tick is not None and tick.book is not None and error is None for symbol, tick, error in got)
    finally:
        scheduler.close()


def test_zero_period_does_not_resubmit_fast_symbol_during_slow_sibling():
    """Regression: a completed end-of-collect tick is part of this round."""
    import threading

    slow_started = threading.Event()
    release_slow = threading.Event()
    fast_calls = []

    class Source:
        def candles(self, symbol, required):
            return [{"symbol": symbol}]

        def orderbook(self, symbol):
            if symbol == 'ETHUSDT':
                slow_started.set()
                assert release_slow.wait(timeout=5)
            else:
                fast_calls.append(symbol)
            return {"at": time.time(), "bids": [[100, 1]], "asks": [[101, 1]]}

        def funding(self, symbol):
            return []

    scheduler = MarketScheduler(
        Source(), {"symbols": ["BTCUSDT", "ETHUSDT"]},
        lambda *a: 20, workers=2, period=0,
    )
    state = {"positions": {}, "pending": {}, "funding_archive": {}}
    try:
        first = scheduler.collect(state, timeout=.2)
        assert slow_started.wait(timeout=2)
        assert [symbol for symbol, _, _ in first] == ['BTCUSDT']
        assert len(fast_calls) == 1
        for _ in range(5):
            scheduler.collect(state, timeout=.01)
        assert len(fast_calls) == 1, 'Fast request was fetched again before sibling completion'
    finally:
        release_slow.set()
        scheduler.close()


def test_history_lane_respects_explicit_risk_transport_capability():
    """No fallback may override an explicit opt-out; native and legacy stay enabled."""
    class Source:
        def __init__(self, enabled):
            self.enabled = enabled
            self.calls = []

        def candles(self, symbol, required):
            return [{"symbol": symbol}]

        def funding(self, symbol):
            return []

        def refresh_position_risk(self, symbol):
            self.calls.append(symbol)
            return {"symbol": symbol, "risk_at": 1.}

        def risk_maintenance_enabled(self):
            return self.enabled

    for enabled, independent, expected in ((False, False, 0),
                                            (True, False, 1),
                                            (True, True, 0)):
        source = Source(enabled)
        sched = MarketScheduler(source, {"symbols": ["BTCUSDT"]},
                                lambda *a: 20, workers=1)
        try:
            sched._risk_refresh_independent = independent
            tick = sched._fetch("BTCUSDT", 20, [0], urgent=True, history_only=True)
            assert tick.bars and not tick.history_error
            assert len(source.calls) == expected
            assert (tick.margin_data is not None) == bool(expected)
        finally:
            sched.close()

    # Third-party adapters that do not define the capability retain the
    # original explicit risk-reader behavior.
    source = Source(True)
    source.risk_maintenance_enabled = lambda: True
    class Legacy:
        candles = source.candles
        funding = source.funding
        refresh_position_risk = source.refresh_position_risk
    sched = MarketScheduler(Legacy(), {"symbols": ["BTCUSDT"]}, lambda *a: 20, workers=1)
    try:
        tick = sched._fetch("BTCUSDT",20,[0],urgent=True,history_only=True)
        assert source.calls == ["BTCUSDT"] and tick.margin_data["symbol"] == "BTCUSDT"
    finally:
        sched.close()
