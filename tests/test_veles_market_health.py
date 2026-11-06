"""Worker heartbeat must reveal missing market data without claiming a trade."""
import json
from pathlib import Path
from bcc.config import defaults
from bcc.storage import Store
from bcc.market import DataUnavailable
from bcc.runtime import run_loop


class NeverTrades:
    def process(self, *args):
        raise AssertionError('No strategy processing expected after a failed read')


def test_worker_reports_failed_market_tick_in_process_status(tmp_path, monkeypatch):
    from bcc import runtime
    cfg = defaults('VELES')
    cfg['symbols'] = ['BTCUSDT']
    store = Store(tmp_path, 500)
    class FailingScheduler:
        seen = False
        def __init__(self, *args, **kwargs): pass
        def collect(self, *args, **kwargs):
            self.seen = True
            return [('BTCUSDT', None, DataUnavailable('Bybit candles unavailable'))]
        def close(self): pass
    scheduler = FailingScheduler()
    monkeypatch.setattr(runtime, 'MarketScheduler', lambda *a, **kw: scheduler)
    run_loop(tmp_path, cfg, store, object(), NeverTrades(), {},
             lambda: bool(store.read()["diagnostics"].get("BTCUSDT")), lambda *a: 150, None)
    health = json.loads((Path(tmp_path) / 'process.json').read_text(encoding="utf-8"))['market_health']
    assert health['market_errors'] == 1
    assert health['valid_history_ticks'] == 0
    assert health['last_symbol'] == 'BTCUSDT'
    assert 'Bybit candles unavailable' in health['last_error']


def test_missing_book_preserves_both_market_failure_reasons(tmp_path):
    from bcc.runtime import process_tick
    from bcc.scheduler import MarketTick
    cfg = defaults('VELES')
    cfg['symbols'] = ['BTCUSDT']
    store = Store(tmp_path, 500)
    tick = MarketTick('BTCUSDT', [], None, [], {},
                      history_error='Bybit kline timeout',
                      quote_error='Bybit orderbook timeout')
    process_tick(cfg, store, NeverTrades(), tick)
    diagnostics = store.read(include_charts=False)['diagnostics']['BTCUSDT']
    assert [(d['section'], d['reason']) for d in diagnostics] == [
        ('Стакан', 'Bybit orderbook timeout'),
        ('Свечи', 'Bybit kline timeout'),
    ]
    reopened = Store(tmp_path)
    assert reopened.read(include_charts=False)['diagnostics']['BTCUSDT'] == diagnostics


def test_missing_book_and_history_without_exception_is_reported(tmp_path):
    from bcc.runtime import process_tick
    from bcc.scheduler import MarketTick
    cfg = defaults('VELES')
    cfg['symbols'] = ['BTCUSDT']
    store = Store(tmp_path, 500)
    process_tick(cfg, store, NeverTrades(), MarketTick('BTCUSDT', [], None, [], {}))
    reasons = store.read(include_charts=False)['diagnostics']['BTCUSDT']
    assert reasons[0]['section'] == 'Рынок'
    assert 'Нет стакана' in reasons[0]['reason']


def test_healthy_book_does_not_authorize_signal_without_candles(tmp_path, monkeypatch):
    from bcc import runtime
    from bcc.scheduler import MarketTick
    cfg = defaults('VELES')
    cfg['symbols'] = ['BTCUSDT']
    store = Store(tmp_path, 500)
    monkeypatch.setattr(runtime, 'market_now', lambda: 1762000000.0)
    monkeypatch.setattr(runtime, 'book_is_fresh', lambda at: True)
    monkeypatch.setattr(runtime, 'executable_quote', lambda book: (100., 101.))
    class FailOnSignal:
        def process(self, *args):
            raise AssertionError('No signal may be evaluated without valid candles')
    tick = MarketTick('BTCUSDT', [], {'at': 1762000000.0}, [], {}, history_error='kline unavailable')
    runtime.process_tick(cfg, store, FailOnSignal(), tick)
    diagnostics = store.read(include_charts=False)['diagnostics']['BTCUSDT']
    assert any('kline unavailable' in item['reason'] for item in diagnostics)
