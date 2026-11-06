"""Cross-layer inventory recovery after interrupted destructive operations."""

from bcc.config import defaults
from bcc.storage import Repository
from bcc.supervisor import Supervisor
from bcc.coordination import Coordinator


def _make(repo, name):
    config = defaults('VELES')
    config['name'] = name
    return repo.create(config)


def test_retiring_first_bot_does_not_starve_later_inventory(tmp_path):
    repo = Repository(tmp_path)
    first = _make(repo, 'first')
    second = _make(repo, 'second')
    sup = Supervisor(repo)
    first_key, second_key = sup._key(first), sup._key(second)
    sup.coordinator.begin_retirement(first_key)

    # The original directory persists after a failed delete, but discovery
    # must not register it again or prevent the remaining park from discovery.
    for _ in range(10):
        examined, complete = sup.inventory_slice(limit=1)
        if complete:
            break
    else:
        raise AssertionError('inventory did not complete')
    assert sup.coordinator.get_registration(first_key)['retiring']
    assert sup.coordinator.get_registration(second_key) is not None
    assert sup.coordinator.get_registration(first_key)['desired'] == 'STOPPED'


def test_retired_id_never_reappears_when_inventory_is_replayed(tmp_path):
    db = Coordinator(tmp_path / 'coordination.sqlite')
    db.initialize()
    db.begin_retirement('VELES/old')
    assert db.unregister('VELES/old')
    assert db.register_discovered('VELES/old') is False
    assert db.get_registration('VELES/old') is None
    assert db.register_discovered('VELES/new') is True
    assert db.register_discovered('VELES/new') is True
    assert db.get_registration('VELES/new') is not None
