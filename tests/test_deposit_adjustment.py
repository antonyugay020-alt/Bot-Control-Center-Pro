import math
import uuid

import pytest

from bcc.config import defaults
from bcc.storage import Repository, Store, AccountingError, DEPOSIT_INTENT, atomic_json, recover_deposit_change


def setup(tmp_path):
    repo = Repository(tmp_path)
    cfg = defaults('VELES')
    cfg['name'] = 'Deposit test'
    folder = repo.create(cfg)
    Store(folder, cfg['capital'])
    return repo, folder, cfg


def test_deposit_increase_preserves_original_and_pnl(tmp_path):
    repo, folder, cfg = setup(tmp_path)
    changed = dict(cfg, capital=cfg['capital'] + 120)
    repo.save(folder, changed, repo.load(folder)[1])
    state = Store(folder, changed['capital']).read()
    assert state['initial'] == cfg['capital']
    assert state['cash'] == changed['capital']
    assert state['realized'] == 0
    assert len(Store(folder).history()) == 1
    assert Store(folder).history()[0]['action'] == 'CAPITAL_ADJUST'
    Store(folder).audit()
    assert repo.load(folder)[0]['capital'] == changed['capital']
    assert not (folder / DEPOSIT_INTENT).exists()


def test_deposit_decrease_and_repeated_adjustments(tmp_path):
    repo, folder, cfg = setup(tmp_path)
    for amount in (100, -40, 15):
        config, digest = repo.load(folder)
        repo.save(folder, dict(config, capital=config['capital'] + amount), digest)
    assert math.isclose(Store(folder).effective_capital(), cfg['capital'] + 75)
    assert len(Store(folder).history()) == 3
    Store(folder, cfg['capital'] + 75).audit()


def test_withdrawal_insufficient_refuses_no_mutation(tmp_path):
    repo, folder, cfg = setup(tmp_path)
    proposed = dict(cfg, capital=0.01)
    # Exhaust available cash without changing initial capital through an execution.
    with Store(folder).transaction(include_charts=False) as (state, events):
        state['cash'] -= 499.5
        events.append(dict(id=uuid.uuid4().hex, at=1, symbol='BTCUSDT', section='S', action='FEE', cash_delta=-499.5, net_pnl=0.0))
    digest = repo.load(folder)[1]
    with pytest.raises(ValueError, match='Недостаточно|Вывод'):
        repo.save(folder, proposed, digest)
    assert repo.load(folder)[1] == digest
    assert Store(folder).read()['cash'] == pytest.approx(0.5)
    assert Store(folder).history()[0]['action'] == 'FEE'
    assert not (folder / DEPOSIT_INTENT).exists()


def test_recovery_after_sqlite_commit_before_config(tmp_path):
    repo, folder, cfg = setup(tmp_path)
    new = dict(cfg, capital=cfg['capital'] + 25)
    eid = uuid.uuid4().hex
    atomic_json(folder / DEPOSIT_INTENT, dict(version=1, event_id=eid, old_config=cfg, new_config=new))
    Store(folder).adjust_capital(25, eid)
    recover_deposit_change(folder)
    assert repo.load(folder)[0]['capital'] == new['capital']
    assert Store(folder, new['capital']).read()['cash'] == new['capital']
    assert not (folder / DEPOSIT_INTENT).exists()


def test_recovery_before_sqlite_commit_keeps_old(tmp_path):
    repo, folder, cfg = setup(tmp_path)
    new = dict(cfg, capital=cfg['capital'] + 25)
    atomic_json(folder / DEPOSIT_INTENT, dict(version=1, event_id=uuid.uuid4().hex, old_config=cfg, new_config=new))
    recover_deposit_change(folder)
    assert repo.load(folder)[0]['capital'] == cfg['capital']
    assert Store(folder).read()['cash'] == cfg['capital']


def test_no_paper_account_change_only_config(tmp_path):
    repo = Repository(tmp_path)
    cfg = defaults('VELES')
    cfg['name'] = 'Deposit test'
    folder = repo.create(cfg)
    changed = dict(cfg, capital=cfg['capital'] + 99)
    repo.save(folder, changed, repo.load(folder)[1])
    assert repo.load(folder)[0]['capital'] == changed['capital']
    assert not (folder / 'paper.sqlite').exists()


def test_copy_with_history_after_funding(tmp_path):
    repo, folder, cfg = setup(tmp_path)
    repo.save(folder, dict(cfg, capital=675), repo.load(folder)[1])
    clone = repo.copy(folder, history=True)
    state = Store(clone, 675).read()
    assert state['initial'] == 500
    assert state['cash'] == 675
    assert Store(clone).effective_capital() == 675
    Store(clone).audit()


def test_adjustment_keeps_performance_baselines(tmp_path):
    repo, folder, cfg = setup(tmp_path)
    with Store(folder).transaction(include_charts=False) as (state, events):
        state['daily'] = dict(day='2026-11-01', start=500)
        state['equity_peak'] = 550
    repo.save(folder, dict(cfg, capital=620), repo.load(folder)[1])
    state = Store(folder).read()
    assert state['daily']['start'] == 620
    assert state['equity_peak'] == 670
    assert state['realized'] == 0


def test_export_import_history_after_funding(tmp_path):
    from bcc.transfer import export_bots, import_bots
    repo, folder, cfg = setup(tmp_path)
    repo.save(folder, dict(cfg, capital=620), repo.load(folder)[1])
    archive = tmp_path / 'transfer.zip'
    export_bots(repo, [folder], archive, history=True)
    other = Repository(tmp_path / 'other')
    imported = import_bots(other, archive, history=True)
    assert imported
    for target in imported:
        assert Store(target, 620).read()['initial'] == 500
        assert Store(target).read()['cash'] == 620
        Store(target).audit()


def test_pending_margin_prevents_withdrawal(tmp_path):
    repo, folder, cfg = setup(tmp_path)
    with Store(folder).transaction(include_charts=False) as (state, events):
        state['pending'][f"{cfg['symbols'][0]}:{cfg['sections'][0]['id']}"] = {'margin': 450, 'symbol': cfg['symbols'][0]}
    before, digest = repo.load(folder)
    with pytest.raises(ValueError, match='Недостаточно'):
        repo.save(folder, dict(before, capital=400), digest)
    assert repo.load(folder)[1] == digest
    assert Store(folder).read()['cash'] == 500
    assert list(Store(folder).read()['pending'].values())[0]['margin'] == 450


@pytest.mark.parametrize('strategy', ['VELES','POLYMARKET','GRID','PUMP_DUMP','EMA','MACD','BOLLINGER','EMA_RSI_VOLUME'])
def test_deposit_save_all_strategies(strategy, tmp_path):
    repo = Repository(tmp_path)
    cfg = defaults(strategy)
    cfg['name'] = 'Deposit test'
    folder = repo.create(cfg)
    Store(folder, cfg['capital'])
    amended = dict(cfg, capital=cfg['capital'] + 25)
    repo.save(folder, amended, repo.load(folder)[1])
    assert Store(folder, amended['capital']).read()['cash'] == amended['capital']
    Store(folder).audit()


def test_recovery_when_config_published_after_commit(tmp_path):
    repo, folder, cfg = setup(tmp_path)
    new = dict(cfg, capital=cfg['capital'] + 20)
    eid = uuid.uuid4().hex
    atomic_json(folder / DEPOSIT_INTENT, dict(version=1, event_id=eid, old_config=cfg, new_config=new))
    Store(folder).adjust_capital(20, eid)
    atomic_json(folder / 'config.json', new)
    recover_deposit_change(folder)
    assert Store(folder).effective_capital() == new['capital']
    assert Store(folder).history()[0]['id'] == eid
    assert not (folder / DEPOSIT_INTENT).exists()
