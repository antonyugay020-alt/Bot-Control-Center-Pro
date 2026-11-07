"""Integrated failure recovery: PAPER conservation + hub + diagnostic evidence."""
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

from bcc.config import defaults
from bcc.market import DataUnavailable
from bcc.runtime import run_loop
from bcc.storage import Repository, Store
from bcc.support import export_current_report
from bcc.worker import process_polymarket


def test_poly_transient_external_failure_rolls_back_cash_positions_and_keeps_worker_alive(tmp_path):
    cfg = defaults('POLYMARKET')
    store = Store(tmp_path, cfg['capital'])
    calls = []

    def process(cfg, state, events, source, now, should_stop):
        calls.append(True)
        state['cash'] -= 27
        state['done']['wrong-partial-tick'] = True
        events.append({'action': 'OPEN'})
        raise DataUnavailable('Temporary public Gamma failure')

    run_loop(tmp_path, cfg, store, object(), None, {},
             lambda: bool(calls), lambda *_: 150, process)
    result = store.read(include_charts=False)
    assert len(calls) == 1
    assert result['cash'] == cfg['capital']
    assert 'wrong-partial-tick' not in result['done']
    assert not store.history()
    status = json.loads((tmp_path / 'process.json').read_text(encoding='utf-8'))
    assert status['market_health']['market_errors'] >= 1
    assert status['market_health']['last_symbol'] == 'POLYMARKET'
    assert 'Gamma' in status['market_health']['last_error']


def test_poly_unexpected_programming_exception_remains_fatal(tmp_path):
    cfg = defaults('POLYMARKET')
    store = Store(tmp_path, cfg['capital'])
    def broken(*_):
        raise AssertionError('programming logic violation')
    with pytest.raises(AssertionError, match='programming logic'):
        run_loop(tmp_path, cfg, store, object(), None, {}, lambda: False,
                 lambda *_: 150, broken)
    assert store.read(include_charts=False)['cash'] == cfg['capital']


def test_poly_transient_locked_sqlite_rolls_back_without_worker_exit(tmp_path):
    cfg = defaults('POLYMARKET')
    store = Store(tmp_path, cfg['capital'])
    calls = []
    def locked(cfg, state, events, source, now, stop):
        calls.append(1)
        state['cash'] -= 20
        raise sqlite3.OperationalError('database is locked')
    run_loop(tmp_path, cfg, store, object(), None, {},
             lambda: bool(calls), lambda *_: 150, locked)
    assert store.read(include_charts=False)['cash'] == cfg['capital']
    status = json.loads((tmp_path / 'process.json').read_text(encoding='utf-8'))
    assert status['market_health']['market_errors'] == 1


def test_poly_non_busy_sqlite_error_is_not_silently_retried(tmp_path):
    cfg = defaults('POLYMARKET')
    store = Store(tmp_path, cfg['capital'])
    def corrupt(*_):
        raise sqlite3.OperationalError('no such table: transactions')
    with pytest.raises(sqlite3.OperationalError, match='no such table'):
        run_loop(tmp_path, cfg, store, object(), None, {},
                 lambda: False, lambda *_: 150, corrupt)


def test_poly_per_market_data_failure_visible_in_diagnostics_and_worker_health(tmp_path):
    cfg = defaults('POLYMARKET')
    cfg['symbols'] = ['BTC']
    state = Store(tmp_path, cfg['capital']).read(include_charts=False)
    class Offline:
        def discover(self, *a):
            raise DataUnavailable('Gamma endpoint temporarily unavailable')
    count = process_polymarket(cfg, state, [], Offline(), 2000000000)
    assert count >= 1
    failures = state['diagnostics']['POLYMARKET']
    assert failures and all(x.get('category') == 'market_error' for x in failures)
    assert 'Gamma' in failures[0]['reason']
    assert state['cash'] == cfg['capital']


def test_report_keeps_worker_traceback_and_paper_on_corrupt_process_json(tmp_path):
    root = tmp_path / 'test_root'
    repo = Repository(root)
    folder = repo.create({**defaults('POLYMARKET'), 'name': 'Failed poly'})
    Store(folder, defaults('POLYMARKET')['capital'])
    (folder / 'process.json').write_text('[]', encoding='utf-8')
    (folder / 'engine.log').write_text('Traceback: unknown transport error\napi_key=should-not-leak\n', encoding='utf-8')
    out = tmp_path / 'evidence.zip'
    report = export_current_report(root, out)
    assert report['bot_count'] == 1
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        engine_log = next(name for name in names if name.endswith('/engine.log'))
        paper = next(name for name in names if name.endswith('/paper.sqlite'))
        text = z.read(engine_log).decode('utf-8')
        assert 'Traceback' in text
        assert 'should-not-leak' not in text
        assert z.read(paper)[:16] == b'SQLite format 3\x00'
        summary = json.loads(z.read('current_bots_report/report_summary.json'))
        assert any('process_error' in x for x in summary['errors'])


def test_hub_service_continues_after_transient_sqlite_lock(monkeypatch, tmp_path):
    from tools import market_hub_service
    attempts = []
    class Registry:
        def claim_leader(self, *_args, **_kw):
            return True
        def has_active_subscriptions(self):
            return False
    class Hub:
        registry = Registry()
        owner = 'unit-test-leader'
        def __init__(self, *_args, **_kw): pass
        def acquire_leadership(self, **kwargs):
            return self.registry.claim_leader(self.owner, **kwargs)
        def poll_nonblocking(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise sqlite3.OperationalError('database is locked')
        def close(self): pass
    monkeypatch.setattr(market_hub_service, 'MarketDataHub', Hub)
    monkeypatch.setattr(sys, 'argv', ['hub', '--cache', str(tmp_path / 'cache.sqlite'),
                                      '--poll', '.02', '--idle-exit', '.06'])
    market_hub_service.main()
    assert len(attempts) >= 2

@pytest.mark.parametrize('stage', ['claim', 'liveness'])
def test_hub_service_survives_sqlite_contention_at_start_or_liveness(monkeypatch, tmp_path, stage):
    from tools import market_hub_service
    attempts = {'claim': 0, 'live': 0, 'fetch': 0}
    class Registry:
        def claim_leader(self, *_args, **_kw):
            attempts['claim'] += 1
            if stage == 'claim' and attempts['claim'] == 1:
                raise sqlite3.OperationalError('database is locked')
            return True
        def has_active_subscriptions(self):
            attempts['live'] += 1
            if stage == 'liveness' and attempts['live'] == 1:
                raise sqlite3.OperationalError('database is locked')
            return False
    class Hub:
        registry = Registry()
        owner = 'test-owner'
        def __init__(self, *_args, **_kw): pass
        def acquire_leadership(self, **kwargs):
            return self.registry.claim_leader(self.owner, **kwargs)
        def poll_nonblocking(self):
            attempts['fetch'] += 1
        def close(self): pass
    monkeypatch.setattr(market_hub_service, 'MarketDataHub', Hub)
    monkeypatch.setattr(sys, 'argv', ['hub', '--cache', str(tmp_path / 'cache.sqlite'),
                                      '--poll', '.02', '--idle-exit', '.08'])
    market_hub_service.main()
    assert attempts['claim'] >= (2 if stage == 'claim' else 1)
    assert attempts['live'] >= (2 if stage == 'liveness' else 1)
    assert attempts['fetch'] >= 1
