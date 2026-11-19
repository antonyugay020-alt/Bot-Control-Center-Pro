"""A single failed market read must not wait for an entire 824-symbol scan."""
import threading
import time
from collections import OrderedDict

import pytest

from bcc.market import DataUnavailable
from bcc.scheduler import MarketScheduler


class ScanSource:
    def __init__(self, always_fail=()):
        self.attempts = {}
        self.lock = threading.Lock()
        self.always_fail = set(always_fail)

    def candles(self, symbol, required):
        time.sleep(.004)
        with self.lock:
            self.attempts[symbol] = self.attempts.get(symbol, 0) + 1
            attempt = self.attempts[symbol]
        if symbol in self.always_fail or (symbol == 'COIN0USDT' and attempt == 1):
            raise DataUnavailable('synthetic temporary history failure')
        return [{'t': int(time.time() * 1000)}]

    def orderbook(self, symbol):
        return dict(at=time.time(), bids=[[99, 10]], asks=[[101, 10]])

    def position_orderbook(self, symbol):
        return self.orderbook(symbol)


def empty_state():
    return {'positions': {}, 'pending': {}, 'funding_archive': {}, 'marks': {}}


def test_failed_history_retries_before_complete_824_symbol_revolution():
    symbols = [f'COIN{i}USDT' for i in range(824)]
    source = ScanSource()
    scheduler = MarketScheduler(source, {'symbols': symbols}, lambda *_: 100,
                                workers=8, period=.01)
    try:
        deadline = time.monotonic() + 15
        recovered = False
        while time.monotonic() < deadline and not recovered:
            for symbol, tick, error in scheduler.collect(empty_state(), timeout=.02):
                if symbol == symbols[0] and tick and tick.bars and not tick.history_error:
                    recovered = True
                    break
        with source.lock:
            discovered = len(source.attempts)
        assert recovered, 'failed symbol was never retried'
        assert discovered < 200, f'retry delayed behind {discovered} scanner symbols'
    finally:
        scheduler.close()


def test_failed_scanner_book_is_retried_without_replaying_the_full_universe():
    symbols = [f'COIN{i}USDT' for i in range(824)]

    class BookFailure(ScanSource):
        def candles(self, symbol, required):
            time.sleep(.004)
            with self.lock:
                self.attempts[symbol] = self.attempts.get(symbol, 0) + 1
            return [{'t': int(time.time() * 1000)}]

        def orderbook(self, symbol):
            if symbol == symbols[0] and self.attempts[symbol] == 1:
                raise DataUnavailable('synthetic temporary book failure')
            return super().orderbook(symbol)

    source = BookFailure()
    scheduler = MarketScheduler(source, {'symbols': symbols}, lambda *_: 100,
                                workers=8, period=.01)
    try:
        deadline = time.monotonic() + 15
        recovered = False
        while time.monotonic() < deadline and not recovered:
            for symbol, tick, error in scheduler.collect(empty_state(), timeout=.02):
                if symbol == symbols[0] and tick and tick.book and not tick.quote_error:
                    recovered = True
                    break
        with source.lock:
            discovered = len(source.attempts)
        assert recovered
        assert discovered < 200, f'book retry delayed behind {discovered} scanner symbols'
    finally:
        scheduler.close()


def test_removed_symbol_is_not_resurrected_by_retry_queue():
    symbols = ['COIN0USDT', 'COIN1USDT']
    source = ScanSource(always_fail={'COIN0USDT'})
    cfg = {'symbols': symbols}
    scheduler = MarketScheduler(source, cfg, lambda *_: 100, workers=1, period=.05)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and 'COIN0USDT' not in scheduler._scan_retries:
            scheduler.collect(empty_state(), timeout=.02)
        assert 'COIN0USDT' in scheduler._scan_retries
        cfg['symbols'] = ['COIN1USDT']
        scheduler.collect(empty_state(), timeout=.02)
        assert 'COIN0USDT' not in scheduler._scan_retries
    finally:
        scheduler.close()


@pytest.mark.parametrize('workers', [1, 2, 8])
def test_permanently_failing_symbols_do_not_starve_new_discovery(workers):
    symbols = [f'COIN{i}USDT' for i in range(824)]
    failed = set(symbols[:8])
    source = ScanSource(always_fail=failed)
    scheduler = MarketScheduler(source, {'symbols': symbols}, lambda *_: 100,
                                workers=workers, period=.01)
    try:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            scheduler.collect(empty_state(), timeout=.02)
            with source.lock:
                retry_seen = any(source.attempts.get(s, 0) > 1 for s in failed)
                fresh_count = len(set(source.attempts) - failed)
            if retry_seen and fresh_count >= 100:
                break
        assert retry_seen, 'failed work did not receive a retry'
        assert fresh_count >= 100, 'retries starved untouched symbols'
    finally:
        scheduler.close()


@pytest.mark.parametrize('workers', [1, 2, 8])
def test_unmarked_position_still_gets_quotes_while_scan_and_retries_progress(workers):
    symbols = [f'COIN{i}USDT' for i in range(824)]
    source = ScanSource(always_fail=set(symbols[:8]))
    scheduler = MarketScheduler(source, {'symbols': symbols}, lambda *_: 100,
                                workers=workers, period=.01)
    state = {'positions': {'held': {'symbol': 'HELDUSDT', 'opened': 1}},
             'pending': {}, 'funding_archive': {}, 'marks': {}}
    try:
        deadline = time.monotonic() + 12
        held_quotes = 0
        while time.monotonic() < deadline:
            for symbol, tick, error in scheduler.collect(state, timeout=.02):
                if symbol == 'HELDUSDT' and tick and tick.book:
                    held_quotes += 1
            with source.lock:
                retry_seen = any(source.attempts.get(s, 0) > 1 for s in symbols[:8])
                fresh_count = len(set(source.attempts) - set(symbols[:8]))
            if held_quotes and retry_seen and fresh_count >= 40:
                break
        assert held_quotes, 'position quote lane was starved'
        assert retry_seen, 'failed history did not retry'
        assert fresh_count >= 40, 'untouched scans were starved'
        assert scheduler.last_pressure['entry_admission_paused']
    finally:
        scheduler.close()


def test_fleet_wide_failure_can_use_idle_workers_for_recovery():
    symbols = [f'COIN{i}USDT' for i in range(16)]
    gate, lock, started = threading.Event(), threading.Lock(), set()

    class RecoveringSource:
        def candles(self, symbol, required):
            with lock:
                started.add(symbol)
            assert gate.wait(10), 'test did not release recovery requests'
            return [{'t': int(time.time() * 1000)}]

        def orderbook(self, symbol):
            return dict(at=time.time(), bids=[[99, 10]], asks=[[101, 10]])

    scheduler = MarketScheduler(RecoveringSource(), {'symbols': symbols},
                                lambda *_: 100, workers=8, period=.05)
    # Model the stable queue after all subjects have failed, without relying
    # on arbitrary wall-clock timing for the outage itself.
    scheduler._scan_retries = OrderedDict((symbol, 0) for symbol in symbols)
    try:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            scheduler.collect(empty_state(), timeout=.02)
            with lock:
                if len(started) >= 4:
                    break
        with lock:
            assert len(started) >= 4, 'recovery left available worker lanes idle'
    finally:
        gate.set()
        scheduler.close()
