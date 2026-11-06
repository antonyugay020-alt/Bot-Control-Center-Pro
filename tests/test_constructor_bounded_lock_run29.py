"""Real OS-process draft contention/recovery, with no trading side effects."""
from copy import deepcopy
import multiprocessing
import random
import time

import pytest

from bcc.config import defaults
from bcc.constructor_session import (
    _draft_lock, load_constructor_draft_snapshot,
    remove_constructor_draft_if_revision, save_constructor_draft_with_revision,
)


def _hold_lock(path, conn):
    with _draft_lock(path, timeout_seconds=10):
        conn.send('locked')
        conn.recv()


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
@pytest.mark.parametrize('seed', range(3))
def test_bounded_contention_does_not_touch_draft_and_recovers(tmp_path, strategy, seed):
    rng = random.Random(20261106029 + seed)
    cfg = defaults(strategy)
    cfg['name'] = f'Nontrading draft {strategy} {seed}'
    if strategy == 'POLYMARKET':
        cfg['polymarket']['entry_signal_mode'] = 'before_close'
        cfg['polymarket']['entry_seconds_before_close'] = rng.randint(60, 120)
    else:
        cfg['sections'][0]['entry']['conditions'][0]['timeframe'] = rng.choice((1, 2, 5, 10, 15, 30))
    path = tmp_path / 'draft.json'
    _, revision = save_constructor_draft_with_revision(path, cfg, expected_revision=None)
    raw = path.read_bytes()
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_hold_lock, args=(str(path), child))
    process.start()
    try:
        assert parent.poll(10), 'child did not acquire OS lock'
        assert parent.recv() == 'locked'
        changed = deepcopy(cfg)
        changed['name'] += ' edited'
        started = time.monotonic()
        with pytest.raises(TimeoutError, match='занят другим окном'):
            with _draft_lock(path, timeout_seconds=0.1):
                pytest.fail('lock should not have been acquired')
        assert time.monotonic() - started < 1.5
        assert path.read_bytes() == raw
        # Even deletion is blocked by the same stable lock.
        assert path.exists()
        parent.send('release')
        process.join(timeout=10)
        assert process.exitcode == 0
        assert save_constructor_draft_with_revision(
            path, changed, expected_revision=revision)[0] == changed
        assert load_constructor_draft_snapshot(path)[0] == changed
        assert not (tmp_path / 'MY_BOTS').exists()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        parent.close()
        child.close()


def test_os_crash_releases_draft_lock_without_corrupting_saved_json(tmp_path):
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Crash recovery for before-close policy'
    cfg['polymarket']['entry_signal_mode'] = 'before_close'
    cfg['polymarket']['entry_seconds_before_close'] = 77
    path = tmp_path / 'draft.json'
    _, revision = save_constructor_draft_with_revision(path, cfg, expected_revision=None)
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_hold_lock, args=(str(path), child))
    process.start()
    try:
        assert parent.poll(10) and parent.recv() == 'locked'
        process.terminate()
        process.join(timeout=10)
        assert not process.is_alive()
        assert load_constructor_draft_snapshot(path)[1] == revision
        assert remove_constructor_draft_if_revision(path, revision)
        assert not path.exists()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        parent.close()
        child.close()


def test_lock_timeout_parameter_rejects_invalid_values(tmp_path):
    for timeout in (-1, 61, True, '1'):
        with pytest.raises(ValueError):
            with _draft_lock(tmp_path / 'draft.json', timeout_seconds=timeout):
                pass
