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
