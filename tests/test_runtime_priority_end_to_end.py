"""Priority shutdown remains bounded, durable and fair across large fleets."""
from bcc.coordination import Coordinator
from bcc.supervisor import Supervisor
from test_supervisor_bounded_reconcile import Life
from pathlib import Path


class Repo:
    def __init__(self, path, count=40):
        self.root = path
        self.bots = path / 'MY_BOTS'
        self.bots.mkdir()
        self.folders = []
        for i in range(count):
            p = self.bots / f'bot{i:03d}'
            p.mkdir()
            self.folders.append(p)
    def resolve(self, folder):
        p = Path(folder).resolve()
        if p not in self.folders:
            raise ValueError('missing bot')
        return p
    def list(self):
        return [(p, {'strategy':'VELES'}) for p in self.folders]


def test_urgent_force_stop_bypasses_normal_fleet_cursor(tmp_path):
    repo = Repo(tmp_path)
    sup = Supervisor(repo, lifecycle=Life())
    sup.inventory()
    key = sup._key(repo.folders[-1])
    sup.coordinator.set_desired(key, 'STOPPED', force=True)
    # No need to page through all preceding 39 registered bots.
    cursor, urgent = sup.reconcile_urgent(limit=1)
    assert cursor == key
    assert len(urgent) == 1 and urgent[0].key == key
    assert sup.coordinator.get_registration(key)['force_stop'] is False


def test_urgent_cursor_is_fair_even_when_stop_remains_unacknowledged(tmp_path):
    coord = Coordinator(tmp_path / 'coordination.sqlite')
    coord.initialize()
    for i in range(19):
        key = f'bot{i:03d}'
        coord.register(key)
        coord.set_desired(key, 'STOPPED', force=True)
    cursor, visited = None, set()
    for _ in range(7):
        keys = coord.urgent_force_stops(cursor, limit=3)
        visited.update(keys)
        cursor = keys[-1]
    assert len(visited) == 19
