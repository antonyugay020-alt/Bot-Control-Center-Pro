"""Full control-plane inventory/reconciliation contract under changing fleet size."""
from bcc.config import defaults
from bcc.storage import Repository
from bcc.supervisor import Supervisor


def test_streaming_inventory_registers_complete_fleet_in_bounded_steps(tmp_path):
    repo = Repository(tmp_path)
    expected = set()
    for index in range(37):
        config = defaults('POLYMARKET' if index % 2 else 'VELES')
        config['name'] = f'Paper {index}'
        folder = repo.create(config)
        expected.add(str(folder.relative_to(repo.bots)).replace('\\', '/'))
    host = Supervisor(repo)
    registered = 0
    while True:
        n, completed = host.inventory_slice(limit=3)
        assert 0 <= n <= 3
        registered += n
        if completed:
            break
    assert registered == 37
    assert {r['key'] for r in host.coordinator.snapshot()} == expected
    # Changes to the published tree are picked up on a later pass, without
    # being treated as evidence of a fixed maximum number of bots.
    config = defaults('GRID')
    config['name'] = 'new grid'
    folder = repo.create(config)
    while True:
        _, completed = host.inventory_slice(limit=3)
        if completed:
            break
    assert host.coordinator.get_registration(host._key(folder)) is not None


def test_inventory_slice_does_not_call_complete_dashboard_list(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    for index in range(7):
        config = defaults('VELES')
        config['name'] = f'VELES {index}'
        repo.create(config)
    host = Supervisor(repo)
    monkeypatch.setattr(repo, 'list', lambda: (_ for _ in ()).throw(
        AssertionError('full dashboard inventory in runtime hot path')))
    counts = []
    while True:
        size, complete = host.inventory_slice(limit=2)
        counts.append(size)
        if complete:
            break
    assert counts == [2, 2, 2, 1]
    assert len(host.coordinator.snapshot()) == 7
