"""Constructor lifecycle is optimistic and non-trading, across both strategies."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import random
import threading

import pytest

from bcc.config import defaults, resolve_poly_market
from bcc.constructor_session import (
    draft_revision, load_constructor_draft, load_constructor_draft_snapshot,
    remove_constructor_draft_if_revision, save_constructor_draft,
    save_constructor_draft_with_revision,
)


def unfinished(strategy, seed):
    rng = random.Random(seed)
    c = defaults(strategy)
    c['name'] = ''
    if strategy == 'POLYMARKET':
        c['symbols'] = ['BTC', 'ETH']
        c['polymarket']['durations'] = [300, 900]
        c['polymarket']['entry_signal_mode'] = 'before_close'
        c['polymarket']['entry_seconds_before_close'] = rng.randint(60, 120)
        c['coin_overrides']['ETH'] = {'parameters': {
            'entry_seconds_before_close': rng.randint(30, 55)}}
        c['polymarket']['overrides']['ETH:300'] = {
            'entry_seconds_before_close': rng.randint(10, 25)}
    else:
        c['symbols'] = ['BTCUSDT', 'ETHUSDT']
        c['sections'][0]['entry']['conditions'][0]['timeframe'] = rng.choice([1, 2, 5, 10, 15, 30])
        c['sections'][0]['exit']['conditions'][0]['timeframe'] = rng.choice([1, 2, 5, 10, 15, 30])
        c['coin_overrides']['ETHUSDT'] = {'settings': {
            'leverage': rng.randint(2, 8)}}
    return c


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
@pytest.mark.parametrize('seed', range(45))
def test_two_editors_stale_revision_never_clobbers_latest(tmp_path, strategy, seed):
    path = tmp_path / f'draft_{strategy}.json'
    original = unfinished(strategy, seed + 9012)
    before = deepcopy(original)
    saved, rev0 = save_constructor_draft_with_revision(path, original, expected_revision=None)
    assert saved == before and original == before
    editor_a, a_rev = load_constructor_draft_snapshot(path, expected_strategy=strategy)
    editor_b, b_rev = load_constructor_draft_snapshot(path, expected_strategy=strategy)
    assert a_rev == b_rev == rev0 == draft_revision(path)
    editor_a['name'] = 'A'
    editor_b['name'] = 'B'
    if strategy == 'POLYMARKET':
        editor_a['polymarket']['overrides']['ETH:300']['entry_seconds_before_close'] += 1
        editor_b['polymarket']['overrides']['ETH:300']['entry_seconds_before_close'] += 2
    else:
        editor_a['coin_overrides']['ETHUSDT']['settings']['leverage'] += 1
        editor_b['coin_overrides']['ETHUSDT']['settings']['leverage'] += 2
    _, rev1 = save_constructor_draft_with_revision(path, editor_a, expected_revision=a_rev)
    stable = path.read_bytes()
    with pytest.raises(ValueError, match='другом окне'):
        save_constructor_draft(path, editor_b, expected_revision=b_rev)
    assert path.read_bytes() == stable
    assert not remove_constructor_draft_if_revision(path, b_rev)
    assert path.read_bytes() == stable
    reopened = load_constructor_draft(path, expected_strategy=strategy)
    assert reopened == editor_a
    if strategy == 'POLYMARKET':
        assert resolve_poly_market(reopened, 'ETH', 300)['entry_seconds_before_close'] == editor_a['polymarket']['overrides']['ETH:300']['entry_seconds_before_close']
    assert remove_constructor_draft_if_revision(path, rev1)
    assert not path.exists()
    assert not (tmp_path / 'MY_BOTS').exists()


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
def test_external_file_edit_is_detected_before_save_and_delete(tmp_path, strategy):
    path = tmp_path / 'draft.json'
    config = unfinished(strategy, 44)
    _, revision = save_constructor_draft_with_revision(path, config, expected_revision=None)
    raw = json.loads(path.read_text(encoding='utf-8'))
    raw['name'] = 'edited externally'
    path.write_text(json.dumps(raw), encoding='utf-8')
    assert draft_revision(path) != revision
    with pytest.raises(ValueError, match='другом окне'):
        save_constructor_draft(path, config, expected_revision=revision)
    assert not remove_constructor_draft_if_revision(path, revision)
    assert load_constructor_draft(path)['name'] == 'edited externally'


def test_missing_file_is_a_real_revision_and_does_not_allow_lost_updates(tmp_path):
    path = tmp_path / 'draft.json'
    first = unfinished('POLYMARKET', 1)
    second = unfinished('POLYMARKET', 2)
    assert draft_revision(path) is None
    _, first_revision = save_constructor_draft_with_revision(path, first, expected_revision=None)
    with pytest.raises(ValueError, match='другом окне'):
        save_constructor_draft(path, second, expected_revision=None)
    assert not remove_constructor_draft_if_revision(path, None)
    assert load_constructor_draft(path) == first
    assert remove_constructor_draft_if_revision(path, first_revision)


def test_cross_strategy_and_corrupt_existing_draft_cannot_be_replaced_by_stale_window(tmp_path):
    path = tmp_path / 'draft.json'
    original = unfinished('VELES', 99)
    _, revision = save_constructor_draft_with_revision(path, original, expected_revision=None)
    with pytest.raises(ValueError, match='другой стратегии'):
        load_constructor_draft_snapshot(path, expected_strategy='POLYMARKET')
    path.write_text('{ invalid json', encoding='utf-8')
    corrupted = path.read_bytes()
    with pytest.raises(ValueError, match='другом окне'):
        save_constructor_draft(path, original, expected_revision=revision)
    assert path.read_bytes() == corrupted


def test_parallel_writers_using_same_revision_only_one_can_commit(tmp_path):
    path = tmp_path / 'draft.json'
    base = unfinished('POLYMARKET', 5)
    _, revision = save_constructor_draft_with_revision(path, base, expected_revision=None)
    barrier = threading.Barrier(10)

    def worker(index):
        changed = deepcopy(base)
        changed['name'] = f'Editor {index}'
        changed['polymarket']['entry_seconds_before_close'] = 60 + index
        barrier.wait(timeout=15)
        try:
            return save_constructor_draft_with_revision(
                path, changed, expected_revision=revision)[0]['name']
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        winners = list(executor.map(worker, range(10)))
    assert sum(x is not None for x in winners) == 1
    assert load_constructor_draft(path)['name'] == next(x for x in winners if x)
