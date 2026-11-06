"""Open positions retain fresh executable books while history is blocked."""
import threading
import time

from bcc.scheduler import MarketScheduler
from bcc.storage import initial_state
from bcc.market_clock import observed_quote
from test_core import book


def test_fresh_position_quotes_do_not_wait_for_indicator_or_funding_io():
    blocked, release = threading.Event(), threading.Event()
    class Source:
        def position_orderbook(self, symbol):
            return book(100)
        def orderbook(self, symbol):
            return book(100)
        def candles(self, symbol, required):
            blocked.set()
            assert release.wait(5)
            return []
        def funding(self, symbol):
            return []
    names = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
    state = initial_state(500)
    state['positions'] = {s: dict(symbol=s, opened=time.time()) for s in names}
    state['marks'] = {s: observed_quote(dict(at=time.time(), bid=100, ask=101)) for s in names}
    scheduler = MarketScheduler(Source(), dict(symbols=names), lambda *_: 30,
                                workers=2, period=.05)
    try:
        got = set()
        end = time.monotonic() + 1.2
        while time.monotonic() < end:
            for symbol, tick, error in scheduler.collect(state, timeout=.02):
                assert error is None
                if tick.book:
                    got.add(symbol)
            if got == set(names) and blocked.is_set():
                break
        assert blocked.is_set(), 'The independent history path must actually be blocked'
        assert got == set(names), 'Fresh position books were held behind slow indicator history'
        assert not release.is_set()
    finally:
        release.set()
        scheduler.close()


def test_protective_paper_exit_commits_while_history_request_is_blocked(tmp_path):
    from bcc.config import defaults
    from bcc.storage import Store
    from bcc.trading import enter, plan
    from bcc.runtime import process_tick
    from bcc.strategies import Veles
    from test_core import attach

    cfg = defaults('VELES')
    cfg['name'] = 'Independent quote plane'
    cfg['symbols'] = ['BTCUSDT']
    settings = cfg['settings']
    settings['orders']['count'] = 1
    settings['profit'].update(mode='simple', pct=.5)
    store = Store(tmp_path, cfg['capital'])
    with store.transaction() as (state, events):
        attach(state)
        assert enter(state, events, 'BTCUSDT:main', 'BTCUSDT', 'LONG', settings,
                     100, 25, plan(settings['orders'], 'LONG', 100), 'main')[0]
        state.pop('_books')
    blocked, release = threading.Event(), threading.Event()
    class Source:
        price = 100
        def position_orderbook(self, symbol):
            return book(self.price)
        def orderbook(self, symbol):
            return book(self.price)
        def candles(self, symbol, required):
            blocked.set()
            assert release.wait(5)
            return []
        def funding(self, symbol):
            return []
    source = Source()
    scheduler = MarketScheduler(source, cfg, lambda *_: 30, workers=2, period=.05)
    try:
        until = time.monotonic() + 1.5
        while not blocked.is_set() and time.monotonic() < until:
            for _, tick, error in scheduler.collect(store.read(), timeout=.02):
                assert error is None
                process_tick(cfg, store, Veles(), tick)
        assert blocked.is_set()
        source.price = 110
        until = time.monotonic() + 1.5
        while store.read()['positions'] and time.monotonic() < until:
            for _, tick, error in scheduler.collect(store.read(), timeout=.02):
                assert error is None
                process_tick(cfg, store, Veles(), tick)
        assert not store.read()['positions'], 'Protection waited for unrelated history IO'
        assert not release.is_set()
        assert [e['action'] for e in reversed(store.history())] == ['OPEN', 'CLOSE']
        store.audit()
    finally:
        release.set()
        scheduler.close()


def test_recovered_history_reaches_quotes_without_holding_new_books():
    ready = threading.Event()
    class Source:
        def position_orderbook(self, symbol):
            return book(100)
        def orderbook(self, symbol):
            return book(100)
        def candles(self, symbol, required):
            assert ready.wait(4)
            return [dict(symbol=symbol, closed=True)]
        def funding(self, symbol):
            return []
    state = initial_state(500)
    state['positions']['p'] = dict(symbol='BTCUSDT', opened=time.time())
    state['marks']['BTCUSDT'] = observed_quote(dict(at=time.time(), bid=100, ask=101))
    scheduler = MarketScheduler(Source(), dict(symbols=['BTCUSDT']), lambda *_: 30, workers=2, period=.03)
    try:
        early = []
        until = time.monotonic() + 1
        while not early and time.monotonic() < until:
            early.extend(scheduler.collect(state, timeout=.02))
        assert early and early[0][1].book and not early[0][1].bars
        ready.set()
        history_tick = None
        until = time.monotonic() + 1.5
        while history_tick is None and time.monotonic() < until:
            for _, tick, error in scheduler.collect(state, timeout=.02):
                assert error is None
                if tick.bars:
                    history_tick = tick
        assert history_tick and history_tick.book
        assert history_tick.bars == [dict(symbol='BTCUSDT', closed=True)]
    finally:
        ready.set()
        scheduler.close()


def test_stale_quote_from_position_reader_is_not_executable(tmp_path):
    import pytest
    from bcc.config import defaults
    from bcc.market import DataUnavailable
    from bcc.market_clock import book_is_fresh
    from bcc.runtime import process_tick
    from bcc.storage import Store
    from bcc.strategies import Veles
    class Source:
        def position_orderbook(self, symbol):
            return {**book(100), 'at': time.time() - 10}
        def candles(self, *args):
            raise AssertionError('Do not start history before a fresh quote')
    state = initial_state(500)
    state['positions']['p'] = dict(symbol='BTCUSDT', opened=time.time())
    scheduler = MarketScheduler(Source(), dict(symbols=['BTCUSDT']), lambda *_: 30, workers=2)
    try:
        got = []
        for _ in range(10):
            got.extend(scheduler.collect(state, timeout=.02))
            if got:
                break
        assert got and got[0][1].book is not None
        assert not book_is_fresh(got[0][1].book['at'])
        assert 'устарел' in got[0][1].quote_error
        cfg = defaults('VELES')
        store = Store(tmp_path, cfg['capital'])
        before = store.read()
        with pytest.raises(DataUnavailable, match='устарел'):
            process_tick(cfg, store, Veles(), got[0][1])
        assert store.read() == before
        assert not store.history()
    finally:
        scheduler.close()
