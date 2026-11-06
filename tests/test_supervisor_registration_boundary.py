"""End-to-end registry ownership boundary: inventory writes; fleet polling reads."""
from bcc.supervisor import Supervisor
from bcc.storage import Repository
from bcc.config import defaults


def test_reconcile_never_registers_inside_service_poll(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Registry boundary'
    folder = repo.create(cfg)
    host = Supervisor(repo)
    host.inventory()

    def prohibited(*_args, **_kwargs):
        raise AssertionError('SQLite registration write in high-frequency reconcile')

    monkeypatch.setattr(host.coordinator, 'register', prohibited)
    _, results = host.reconcile_batch(limit=10, sync_inventory=False)
    assert len(results) == 1
    assert results[0].action == 'none'


def test_unregistered_bot_never_implicitly_added_by_reconciliation(tmp_path):
    repo = Repository(tmp_path)
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Registry boundary'
    folder = repo.create(cfg)
    host = Supervisor(repo)
    try:
        host.reconcile_one(folder)
    except KeyError:
        pass
    else:
        raise AssertionError('unregistered bot was implicitly created')
    assert host.coordinator.get_registration(host._key(folder)) is None
