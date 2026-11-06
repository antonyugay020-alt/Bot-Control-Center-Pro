"""GUI intent -> durable coordinator -> owning PAPER ledger, no GUI trading writes."""
from pathlib import Path

import pytest

from bcc.coordination import Coordinator
from bcc.runtime import close_requested_positions
from bcc.storage import Store
from bcc.supervisor import Supervisor
from bcc.config import defaults


class Repo:
    def __init__(self, root):
        self.root = root
        self.bots = root / 'MY_BOTS'
        self.folder = self.bots / 'VELES' / 'one'
        self.folder.mkdir(parents=True)
        self.cfg = defaults('VELES')
        self.cfg['symbols'] = ['BTCUSDT']

    def resolve(self, folder):
        value = Path(folder).resolve()
        if value != self.folder.resolve():
            raise ValueError('not registered')
        return value

    def load(self, folder):
        return self.cfg, None


class Life:
    def status(self, folder):
        return 'Работает'


def test_ui_command_reaches_financial_owner_and_is_acknowledged_after_commit(tmp_path):
    repo = Repo(tmp_path)
    ui = Supervisor(repo, lifecycle=Life())
    ui.submit(repo.folder, running=True)
    worker_epoch = ui.coordinator.get_registration('VELES/one')['command_version']
    store = Store(repo.folder, 500)
    result = ui.submit_manual_close(repo.folder)
    assert result.action == 'queued'
    assert not (repo.folder / 'close.request').exists(), 'GUI wrote directly into worker state'
    assert ui.coordinator.pending_manual_close('VELES/one', launch_revision=worker_epoch) == 1
    close_requested_positions(repo.folder, store, {}, control_revision=worker_epoch)
    assert store.read(include_charts=False)['strategy']['last_close_command'] == 'control-close:1'
    assert ui.coordinator.pending_manual_close('VELES/one', launch_revision=worker_epoch) is None
    # Replays do not create another PAPER operation.
    close_requested_positions(repo.folder, store, {}, control_revision=worker_epoch)
    store.audit()


def test_stop_start_never_replays_manual_close_from_old_worker_epoch(tmp_path):
    repo = Repo(tmp_path)
    ui = Supervisor(repo, lifecycle=Life())
    ui.submit(repo.folder, running=True)
    prior = ui.coordinator.get_registration('VELES/one')['command_version']
    ui.submit_manual_close(repo.folder)
    ui.submit(repo.folder, running=False)
    ui.submit(repo.folder, running=True)
    successor = ui.coordinator.get_registration('VELES/one')['command_version']
    store = Store(repo.folder, 500)
    assert successor != prior
    close_requested_positions(repo.folder, store, {}, control_revision=prior)
    close_requested_positions(repo.folder, store, {}, control_revision=successor)
    assert 'last_close_command' not in store.read(include_charts=False)['strategy']
    assert ui.coordinator.pending_manual_close('VELES/one', launch_revision=successor) is None
    ui.submit_manual_close(repo.folder)
    assert ui.coordinator.pending_manual_close('VELES/one', launch_revision=successor) == 2
    close_requested_positions(repo.folder, store, {}, control_revision=successor)
    assert store.read(include_charts=False)['strategy']['last_close_command'] == 'control-close:2'
    store.audit()


def test_new_close_request_is_not_erased_by_old_ack(tmp_path):
    coord = Coordinator(tmp_path / 'coordination.sqlite')
    coord.initialize()
    coord.register('bot')
    coord.set_desired('bot', 'RUNNING')
    epoch = coord.get_registration('bot')['command_version']
    first = coord.request_manual_close('bot')
    second = coord.request_manual_close('bot')
    assert (first, second) == (1, 2)
    assert not coord.acknowledge_manual_close('bot', first)
    assert coord.pending_manual_close('bot', launch_revision=epoch) == second
    assert coord.acknowledge_manual_close('bot', second)
    assert coord.pending_manual_close('bot', launch_revision=epoch) is None


def test_manual_close_rejected_for_stopped_or_polymarket_bots(tmp_path):
    repo = Repo(tmp_path)
    ui = Supervisor(repo, lifecycle=Life())
    with pytest.raises(ValueError):
        ui.submit_manual_close(repo.folder)
    ui.submit(repo.folder, running=True)
    repo.cfg['strategy'] = 'POLYMARKET'
    with pytest.raises(ValueError):
        ui.submit_manual_close(repo.folder)


def test_qt_close_action_uses_the_same_durable_control_boundary():
    source = (Path(__file__).parents[1] / 'bcc' / 'ui.py').read_text(encoding='utf-8')
    assert 'self.supervisor.submit_manual_close(self.selected())' in source
    assert 'self.life.close_positions(self.selected())' not in source


def test_durable_manual_close_executes_real_open_position_once(tmp_path):
    import math
    import time
    from bcc.config import range_section
    from bcc.runtime import process_tick
    from bcc.scheduler import MarketTick
    from bcc.strategies import Veles, startup_rsi_range
    from test_core import bars, book

    repo = Repo(tmp_path)
    repo.cfg['sections'] = [range_section(30, 40), range_section(40, 50),
                            range_section(50, 60), range_section(60, 70),
                            range_section(70, 60, 'SHORT'), range_section(60, 50, 'SHORT'),
                            range_section(50, 40, 'SHORT'), range_section(40, 30, 'SHORT')]
    repo.cfg['settings'].update(leverage=1, allocation_pct=10, fee_rate=.001, slippage_pct=0)
    repo.cfg['settings']['orders'].update(count=1, first_offset_pct=0)
    store = Store(repo.folder, 500)
    now_ms = int(time.time() * 1000)
    end = (now_ms // 60000 - 1) * 60000
    for phase in range(80):
        rows = bars([100 + 3 * math.sin(i / 4 + phase / 9) for i in range(180)],
                    base=end - 179 * 60000)
        if startup_rsi_range(repo.cfg, store.read(), 'BTCUSDT', rows, now_ms):
            break
    else:
        raise AssertionError('No actionable RSI range')
    process_tick(repo.cfg, store, Veles(), MarketTick('BTCUSDT', rows, book(100), [], {}))
    assert len(store.read()['positions']) == 1
    ui = Supervisor(repo, lifecycle=Life())
    ui.submit(repo.folder, running=True)
    epoch = ui.coordinator.get_registration('VELES/one')['command_version']
    ui.submit_manual_close(repo.folder)
    close_requested_positions(repo.folder, store, {'BTCUSDT': book(100)}, control_revision=epoch)
    assert not store.read()['positions']
    actions = [event['action'] for event in store.history()]
    assert actions.count('OPEN') == 1 and actions.count('CLOSE') == 1
    close_requested_positions(repo.folder, store, {'BTCUSDT': book(100)}, control_revision=epoch)
    assert [event['action'] for event in store.history()] == actions
    store.audit()


def test_legacy_file_and_new_durable_close_both_survive_migration(tmp_path):
    from bcc.storage import atomic_json
    repo = Repo(tmp_path)
    ui = Supervisor(repo, lifecycle=Life())
    ui.submit(repo.folder, running=True)
    epoch = ui.coordinator.get_registration('VELES/one')['command_version']
    store = Store(repo.folder, 500)
    atomic_json(repo.folder / 'close.request', {'id': 'legacy-command'})
    ui.submit_manual_close(repo.folder)
    close_requested_positions(repo.folder, store, {}, control_revision=epoch)
    assert store.read(include_charts=False)['strategy']['last_close_command'] == 'legacy-command'
    assert ui.coordinator.pending_manual_close('VELES/one', launch_revision=epoch) == 1
    close_requested_positions(repo.folder, store, {}, control_revision=epoch)
    assert store.read(include_charts=False)['strategy']['last_close_command'] == 'control-close:1'
    assert ui.coordinator.pending_manual_close('VELES/one', launch_revision=epoch) is None
    store.audit()
