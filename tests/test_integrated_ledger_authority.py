import sqlite3
import pytest
from bcc.storage import Store, BotBusy


def test_one_owner_and_transactional_rejection(tmp_path):
    folder = tmp_path/'bot'; folder.mkdir()
    first = Store(folder, 1000)
    other = Store(folder)
    gen = first.claim_execution('worker-a', ttl=30)
    assert gen == 1
    with pytest.raises(BotBusy):
        other.claim_execution('worker-b', ttl=30)
    with pytest.raises(BotBusy):
        with other.transaction(include_charts=False) as (state, events):
            state['cash'] = 12
    with first.transaction(include_charts=False) as (state, events):
        assert state['cash'] == 1000
    with sqlite3.connect(first.path) as db:
        db.execute('UPDATE execution_owner SET generation=generation+1 WHERE id=1')
    with pytest.raises(BotBusy):
        with first.transaction(include_charts=False) as (state, events):
            state['cash'] = 0
    assert other.read(include_charts=False)['cash'] == 1000


def test_release_and_successor(tmp_path):
    folder = tmp_path/'bot'; folder.mkdir()
    first = Store(folder, 500)
    g1 = first.claim_execution('a')
    first.release_execution()
    second = Store(folder)
    g2 = second.claim_execution('b')
    assert g2 > g1
    with second.transaction(include_charts=False) as (state, events):
        assert state['cash'] == 500
    second.release_execution()
