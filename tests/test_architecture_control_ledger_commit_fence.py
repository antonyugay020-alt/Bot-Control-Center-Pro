"""STOP and ledger commit are linearly ordered across independent SQLite DBs."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

import pytest

from bcc.coordination import Coordinator
from bcc.storage import BotBusy, Store


def managed_account(tmp_path, name='A'):
    root = tmp_path / 'instance'
    folder = root / 'MY_BOTS' / 'VELES' / name
    folder.mkdir(parents=True, exist_ok=True)
    control = Coordinator(root / 'coordination.sqlite')
    control.initialize()
    key = f'VELES/{name}'
    control.register(key)
    control.set_desired(key, 'RUNNING')
    revision = control.get_registration(key)['command_version']
    store = Store(folder, 1000)
    store.claim_execution('run-' + name)
    store.bind_control_authority(control, key, revision)
    return control, key, store


def test_stop_waits_for_inflight_commit_then_revokes_old_worker(tmp_path):
    control, key, store = managed_account(tmp_path)
    entered, release = Event(), Event()
    def do_trade():
        with store.transaction(include_charts=False) as (state, events):
            state['strategy']['committed'] = True
            entered.set()
            assert release.wait(10)
    def do_stop():
        return control.set_desired(key, 'STOPPED')
    with ThreadPoolExecutor(max_workers=2) as pool:
        trade = pool.submit(do_trade)
        assert entered.wait(5)
        stop = pool.submit(do_stop)
        # Before the release, the STOP revision cannot be acknowledged.
        time.sleep(.1)
        assert not stop.done()
        release.set()
        trade.result(timeout=5)
        stop.result(timeout=5)
    assert store.read(include_charts=False)['strategy']['committed'] is True
    with pytest.raises(BotBusy, match='revision revoked'):
        with store.transaction(include_charts=False) as (state, events):
            state['strategy']['illegal'] = True
    assert 'illegal' not in store.read(include_charts=False)['strategy']


def test_stop_committed_first_rejects_stale_financial_commit(tmp_path):
    control, key, store = managed_account(tmp_path)
    control.set_desired(key, 'STOPPED')
    with pytest.raises(BotBusy, match='revision revoked'):
        with store.transaction(include_charts=False) as (state, events):
            state['strategy']['after_stop'] = True
    assert 'after_stop' not in store.read(include_charts=False)['strategy']


def test_owner_specific_gates_do_not_serialize_unrelated_accounts(tmp_path):
    control, key_a, store_a = managed_account(tmp_path, 'A')
    _, key_b, store_b = managed_account(tmp_path, 'B')
    entered, release = Event(), Event()
    def hold_first():
        with store_a.transaction(include_charts=False) as (state, events):
            entered.set()
            assert release.wait(10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(hold_first)
        assert entered.wait(5)
        # B has a different gate and independent SQLite ledger, not a fleet lock.
        second = pool.submit(control.set_desired, key_b, 'STOPPED')
        second.result(timeout=3)
        release.set()
        first.result(timeout=5)
    assert control.get_registration(key_b)['desired'] == 'STOPPED'
    assert control.get_registration(key_a)['desired'] == 'RUNNING'


def test_retirement_revokes_ledger_authority_without_global_stop(tmp_path):
    control, key, store = managed_account(tmp_path)
    control.begin_retirement(key)
    with pytest.raises(BotBusy, match='revision revoked'):
        with store.transaction(include_charts=False) as (state, events):
            state['strategy']['illegal'] = True
    assert store.read(include_charts=False)['strategy'] == {}


def test_real_os_process_holding_ledger_gate_orders_stop(tmp_path):
    """Thread-only locks are insufficient: verify an independent interpreter."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    control, key, store = managed_account(tmp_path)
    root = control.path.parent
    entered, release = tmp_path / 'entered', tmp_path / 'release'
    script = (
        'import sys,time\n'
        'from pathlib import Path\n'
        'from bcc.trade_fence import command_gate\n'
        'root,key,entered,release=sys.argv[1:]\n'
        'with command_gate(root,key):\n'
        ' Path(entered).write_text("locked")\n'
        ' deadline=time.monotonic()+10\n'
        ' while not Path(release).exists():\n'
        '  if time.monotonic()>deadline: raise TimeoutError("test release missing")\n'
        '  time.sleep(.01)\n'
    )
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    child = subprocess.Popen([sys.executable, '-c', script, str(root), key,
                              str(entered), str(release)], env=env)
    try:
        until = time.monotonic() + 5
        while not entered.is_file() and time.monotonic() < until:
            assert child.poll() is None
            time.sleep(.01)
        assert entered.is_file()
        with ThreadPoolExecutor(max_workers=1) as pool:
            stopped = pool.submit(control.set_desired, key, 'STOPPED')
            time.sleep(.1)
            assert not stopped.done()
            release.touch()
            assert stopped.result(timeout=5) is None
        assert child.wait(timeout=5) == 0
    finally:
        release.touch()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
