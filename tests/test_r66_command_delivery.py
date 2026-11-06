"""Crash safe, last-command-wins controller delivery and restart recovery."""
from contextlib import contextmanager

import pytest

from bcc.control import finish_requested
from bcc.runtime import close_requested_positions
from bcc.storage import Store, atomic_json


class DuringTransaction:
    def __init__(self, store, action):
        self.store, self.action = store, action

    @contextmanager
    def transaction(self, **kwargs):
        with self.store.transaction(**kwargs) as result:
            self.action()
            yield result


def test_cancel_arriving_while_finish_is_processing_is_not_lost(tmp_path):
    store = Store(tmp_path, 500)
    atomic_json(tmp_path / 'finish.request', {'id':'first','draining':True})
    calls = []
    def write_new():
        if not calls:
            atomic_json(tmp_path / 'finish.request', {'id':'second','draining':False})
        calls.append(True)
    finish_requested(tmp_path, DuringTransaction(store, write_new))
    state = store.read(False)
    assert state['strategy']['finish_command'] == 'second'
    assert state['strategy']['draining'] is False
    assert not (tmp_path / 'finish.request').exists()
    assert not (tmp_path / '.finish.request.processing').exists()


def test_in_flight_finish_replayed_after_crash_without_erasing_new_request(tmp_path):
    store = Store(tmp_path, 500)
    old = tmp_path / '.finish.request.processing'
    atomic_json(old, {'id':'earlier','draining':True})
    atomic_json(tmp_path / 'finish.request', {'id':'newer','draining':False})
    finish_requested(tmp_path, store)
    assert store.read(False)['strategy']['finish_command'] == 'newer'
    assert store.read(False)['strategy']['draining'] is False
    assert not old.exists()


def test_failed_finish_transaction_preserves_command_for_retry(tmp_path):
    store = Store(tmp_path, 500)
    atomic_json(tmp_path / 'finish.request', {'id':'retry','draining':True})
    class Broken:
        @contextmanager
        def transaction(self, **kwargs):
            raise RuntimeError('disk failure')
            yield
    with pytest.raises(RuntimeError, match='disk failure'):
        finish_requested(tmp_path, Broken())
    assert (tmp_path / '.finish.request.processing').is_file()
    assert store.read(False)['strategy'] == {}
    finish_requested(tmp_path, store)
    assert store.read(False)['strategy']['draining'] is True
    store.audit()


def test_close_command_updated_during_transaction_remains_pending(tmp_path):
    store = Store(tmp_path, 500)
    atomic_json(tmp_path / 'close.request', {'id':'old'})
    calls = []
    def new_close():
        if not calls:
            atomic_json(tmp_path / 'close.request', {'id':'new'})
        calls.append(True)
    close_requested_positions(tmp_path, DuringTransaction(store, new_close), {})
    assert store.read(False)['strategy']['last_close_command'] == 'old'
    assert (tmp_path / 'close.request').exists()
    close_requested_positions(tmp_path, store, {})
    assert store.read(False)['strategy']['last_close_command'] == 'new'
    assert not (tmp_path / 'close.request').exists()
    store.audit()


def test_replayed_close_command_skips_duplicate_close(tmp_path):
    store = Store(tmp_path, 500)
    atomic_json(tmp_path / '.close.request.processing', {'id':'completed'})
    with store.transaction(include_charts=False) as (state, events):
        state['strategy']['last_close_command'] = 'completed'
    close_requested_positions(tmp_path, store, {})
    assert not (tmp_path / '.close.request.processing').exists()
    assert store.history() == []
