"""Published PAPER ownership across engines, fleet risk, observers and recovery.

Only offline synthetic market input; actual strategies, SQLite and Qt models.
No user accounts, real exchange requests, trading keys or orders.
"""
import shutil
import time

import pytest

from bcc.config import condition, defaults, group
from bcc.lifecycle import Lifecycle
from bcc.monitoring import snapshot_all
from bcc.runtime import process_tick
from bcc.scheduler import MarketTick
from bcc.shared_risk import exposure, portfolio_snapshot, risk_transaction
from bcc.storage import AccountingError, Repository, Store, atomic_json
from bcc.strategies import Grid, PumpDump, Veles
from bcc.supervisor import Supervisor
from bcc.worker import process_polymarket
from test_core import bars, book
from test_strategies import PolySource

STRATEGIES = ('VELES', 'GRID', 'PUMP_DUMP', 'POLYMARKET')


def account(repo, strategy, name):
    cfg = defaults(strategy)
    cfg.update(name=name, symbols=['BTC'] if strategy == 'POLYMARKET' else ['BTCUSDT'])
    cfg['settings'].update(leverage=1, allocation_pct=5)
    cfg['settings']['orders']['count'] = 1
    if strategy == 'VELES':
        cfg['sections'] = cfg['sections'][:1]
        cfg['sections'][0]['entry'] = group(condition('PRICE', 100, '>', 1))
        cfg['sections'][0]['exit'] = group(condition('PRICE', 200, '>', 1))
    if strategy == 'GRID':
        cfg['grid']['levels'] = 1
    if strategy == 'POLYMARKET':
        cfg['polymarket']['durations'] = [300]
    folder = Supervisor(repo).create_bot(cfg)
    Store(folder, cfg['capital'])
    return folder, cfg


def advance(folder, cfg):
    """Actual worker decision/commit paths with deterministic market inputs."""
    store = Store(folder)
    if cfg['strategy'] == 'POLYMARKET':
        source = PolySource()
        with risk_transaction(store) as (state, events):
            process_polymarket(cfg, state, events, source, source.now)
        return
    engine = {'VELES': Veles, 'GRID': Grid, 'PUMP_DUMP': PumpDump}[cfg['strategy']]()
    prices = (120, 119, 118, 114) if cfg['strategy'] == 'PUMP_DUMP' else (101,)
    now = time.time()
    history = bars([100] * 27 + [110, 115, 120] if cfg['strategy'] == 'PUMP_DUMP'
                   else [99, 101], int(now) // 60 * 60000 - 30 * 60000)
    if cfg['strategy'] == 'PUMP_DUMP':
        for row in history[-3:]:
            row['volume'] = 1000
    for price in prices:
        depth = book(price)
        depth['margin_required'] = True
        depth['margin_data'] = dict(
            symbol='BTCUSDT', quote=dict(at=depth['at'], bid=price, ask=price),
            risk_at=depth['at'], tiers=[dict(id=1, limit=1e12, initial_rate=.01,
                                           maintenance_rate=.005, deduction=0, max_leverage=100)])
        process_tick(cfg, store, engine, MarketTick('BTCUSDT', history, depth, [], {}))
        if store.read()['positions']:
            break


@pytest.mark.parametrize('strategy', STRATEGIES)
def test_real_engines_open_audited_accounts_for_recovery_matrix(tmp_path, strategy):
    repo = Repository(tmp_path)
    folder, cfg = account(repo, strategy, 'Real engine')
    advance(folder, cfg)
    assert Store(folder).read()['positions'], Store(folder).read()['diagnostics']
    assert any(e['action'] == 'OPEN' for e in Store(folder).history())
    Store(folder).audit()


@pytest.mark.parametrize('supplier', STRATEGIES)
@pytest.mark.parametrize('consumer', STRATEGIES)
def test_missing_published_ledger_blocks_all_strategy_admission_and_restores_exactly(
        tmp_path, supplier, consumer):
    repo = Repository(tmp_path / 'fleet')
    lost, config = account(repo, supplier, 'Lost ' + supplier)
    advance(lost, config)
    assert Store(lost).read()['positions']
    original = Store(lost).read()
    history = Store(lost).history()
    backup = tmp_path / 'verified.sqlite'
    Store(lost).backup(backup)
    contender, config = account(repo, consumer, 'New ' + consumer)
    atomic_json(repo.root / 'shared_risk.json', dict(max_positions=100, max_committed_usdt=0.0))
    (lost / 'paper.sqlite').unlink()

    advance(contender, config)
    after = Store(contender).read()
    assert not after['positions'] and not after['pending']
    assert not Store(contender).history()
    assert any('сохранённые счета' in row['reason']
               for rows in after['diagnostics'].values() for row in rows)
    with pytest.raises(AccountingError, match='paper.sqlite'):
        portfolio_snapshot(repo.root)
    rows = snapshot_all(repo, Lifecycle(repo))
    damaged = next(row for row in rows if row['path'] == str(lost))
    assert damaged['status'] == 'Ошибка' and 'paper.sqlite' in damaged['error']
    assert not (lost / 'paper.sqlite').exists()

    shutil.copyfile(backup, lost / 'paper.sqlite')
    restored = Store(lost)
    restored.audit()
    assert restored.read() == original and restored.history() == history
    assert portfolio_snapshot(repo.root) == exposure(original)
    assert not next(row for row in snapshot_all(repo, Lifecycle(repo), include_stopped=True)
                    if row['path'] == str(lost))['error']


def test_recovery_error_does_not_strand_official_settlement_in_another_account(tmp_path):
    repo = Repository(tmp_path)
    lost, cfg = account(repo, 'VELES', 'Missing neighbour')
    advance(lost, cfg)
    healthy, cfg = account(repo, 'POLYMARKET', 'Must settle')
    advance(healthy, cfg)
    source = PolySource()
    source.resolved = True
    atomic_json(repo.root / 'shared_risk.json', dict(max_positions=1, max_committed_usdt=0.0))
    (lost / 'paper.sqlite').unlink()
    with risk_transaction(Store(healthy)) as (state, events):
        process_polymarket(cfg, state, events, source, source.now + 101)
    final = Store(healthy)
    assert not final.read()['positions']
    assert [event['action'] for event in reversed(final.history())] == ['OPEN', 'SETTLE']
    final.audit()


@pytest.mark.parametrize('strategy', STRATEGIES)
def test_fleet_risk_and_monitor_observe_one_import_commit_boundary(tmp_path, monkeypatch, strategy):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    repo = Repository(tmp_path / 'fleet')
    owner = Supervisor(repo)
    source, cfg = account(repo, strategy, 'Published')
    advance(source, cfg)
    expected = portfolio_snapshot(repo.root)
    archive = tmp_path / 'transfer.zip'
    owner.export_bots([source], archive)
    renamed, release = Event(), Event()
    publish = repo.publish
    def gated(staging):
        folder = publish(staging)
        renamed.set()
        assert release.wait(10)
        return folder
    monkeypatch.setattr(repo, 'publish', gated)
    with ThreadPoolExecutor(max_workers=1) as pool:
        importing = pool.submit(owner.import_bots, archive)
        try:
            assert renamed.wait(5)
            assert portfolio_snapshot(repo.root) == expected
            assert len(snapshot_all(repo, Lifecycle(repo), include_stopped=True)) == 1
        finally:
            release.set()
        imported = importing.result(timeout=10)
    assert len(imported) == 1
    slots, amount = portfolio_snapshot(repo.root)
    assert slots == 2 * expected[0] and amount == pytest.approx(2 * expected[1])
    assert len(snapshot_all(repo, Lifecycle(repo), include_stopped=True)) == 2
    Store(source).audit()
    Store(imported[0]).audit()


@pytest.mark.parametrize('witness', ['paper-account.json', 'process.json'])
def test_missing_ledger_surfaces_in_diagnostics_but_never_started_owner_is_not_an_error(tmp_path, witness):
    from bcc.support import collect
    repo = Repository(tmp_path / 'fleet')
    cfg = defaults('GRID')
    cfg['name'] = 'Never started'
    untouched = Supervisor(repo).create_bot(cfg)
    assert portfolio_snapshot(repo.root) == (0, 0)
    assert not snapshot_all(repo, Lifecycle(repo))
    assert not (untouched / 'paper.sqlite').exists()
    atomic_json(untouched / witness, {})
    with pytest.raises(AccountingError, match='paper.sqlite'):
        portfolio_snapshot(repo.root)
    report = collect(repo.root, tmp_path / 'diagnostics')
    assert any(error.get('bot') == untouched.name and 'paper.sqlite' in error.get('error', '')
               for error in report['errors'])
    assert not (untouched / 'paper.sqlite').exists()
