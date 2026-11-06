"""Integrated, strategy-independent operator/control/account lifecycle.

This is a headless contract test; it deliberately does not certify Qt or native
Windows process execution. It checks the real Supervisor/Coordinator/Repository
and Store rather than mocks of individual methods.
"""
import pytest

from bcc.config import defaults
from bcc.coordination import Coordinator
from bcc.storage import Repository, Store
from bcc.supervisor import Supervisor
from bcc.transfer import export_bots


@pytest.mark.parametrize('strategy', ['VELES', 'GRID', 'PUMP_DUMP', 'POLYMARKET'])
def test_full_operator_cycle_is_owner_isolated_and_durable(tmp_path, strategy):
    repo = Repository(tmp_path / 'workspace')
    operator = Supervisor(repo)
    config = defaults(strategy)
    config['name'] = f'Integrated {strategy}'
    original = operator.create_bot(config)
    key = operator._key(original)
    assert operator.coordinator.get_registration(key)['desired'] == 'STOPPED'

    ledger = Store(original, config['capital'])
    before = ledger.read()
    existing, digest = repo.load(original)
    existing['name'] += ' revised'
    operator.save_bot(original, existing, digest)
    assert Store(original).read()['cash'] == before['cash']

    copied = operator.copy_bot(original, history=True)
    assert copied != original
    assert Store(copied).read()['cash'] == before['cash']
    assert operator.coordinator.get_registration(operator._key(copied))['desired'] == 'STOPPED'

    archive = tmp_path / 'transfer.zip'
    export_bots(repo, [original], archive, history=True)
    imported = operator.import_bots(archive, history=True)
    assert len(imported) == 1 and imported[0] not in (original, copied)
    assert Store(imported[0]).read()['cash'] == before['cash']

    # Commands must survive a new process/Coordinator object, and STOP must
    # invalidate the former launch revision without affecting other owners.
    operator.submit(original, running=True)
    ctl = Coordinator(operator.coordinator.path)
    running = ctl.get_registration(key)
    assert running['desired'] == 'RUNNING'
    assert ctl.launch_allowed(key, running['command_version'])
    operator.submit(original, running=False)
    assert not ctl.launch_allowed(key, running['command_version'])
    assert ctl.get_registration(operator._key(copied))['desired'] == 'STOPPED'

    operator.delete_bot(original, discard_paper_state=True)
    assert not original.exists()
    assert ctl.get_registration(key) is None
    with pytest.raises(ValueError, match='удалён'):
        ctl.submit_desired(key, 'RUNNING')
    assert Store(copied).read()['cash'] == before['cash']
    assert Store(imported[0]).read()['cash'] == before['cash']
