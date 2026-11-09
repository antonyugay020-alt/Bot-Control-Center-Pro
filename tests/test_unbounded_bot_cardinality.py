"""Bot cardinality is not a setting or a bounded dispatcher-slot count.

These tests use only disposable filesystem and SQLite registrations; they
never launch trading processes or contact an exchange.
"""

from bcc.config import defaults
from bcc.coordination import Coordinator
from bcc.fleet import Fleet
from bcc.storage import Repository


def test_ordinary_repository_stores_and_exposes_more_than_thousand_bots(tmp_path):
    repository = Repository(tmp_path)
    configuration = defaults('VELES')
    seen = set()
    for index in range(1101):
        configuration['name'] = f'Independent PAPER bot {index}'
        folder = repository.create(configuration)
        assert folder not in seen
        seen.add(folder)
    assert len(seen) == 1101
    assert {folder for folder, _ in repository.iter_inventory()} == seen
    assert {folder for folder, _ in repository.list()} == seen


def test_runtime_registry_pages_all_2049_owners_without_cardinality_cutoff(tmp_path):
    control = Coordinator(tmp_path / 'fleet.sqlite')
    control.initialize()
    expected = {f'bot-{i:06d}' for i in range(2049)}
    for key in sorted(expected):
        control.register(key)
    seen = set()
    cursor = None
    while len(seen) < len(expected):
        batch = control.next_keys(cursor, limit=37)
        assert batch
        for key in batch:
            if key in seen:
                assert len(seen) == len(expected)
                break
            seen.add(key)
        cursor = batch[-1]
    assert seen == expected


def test_limited_dispatch_queue_does_not_limit_bot_registration():
    fleet = Fleet(workers=1, max_pending=1)
    try:
        for number in range(1501):
            fleet.register(f'bot-{number}', lambda: None)
        assert len(fleet._registered) == 1501
    finally:
        fleet.close(wait=True)
