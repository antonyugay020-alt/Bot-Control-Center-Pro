"""End-to-end, independent PAPER ledgers and import/export for every strategy."""
from copy import deepcopy

import pytest

from bcc.config import STRATEGIES, defaults
from bcc.storage import Repository, Store
from bcc.transfer import export_bots, import_bots


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_all_strategies_create_save_capital_and_clone_with_ledger(tmp_path, strategy):
    repo = Repository(tmp_path / "workspace")
    cfg = defaults(strategy)
    cfg["name"] = "Contract " + strategy
    original = repo.create(cfg)
    account = Store(original, cfg["capital"])
    account.audit()
    loaded, digest = repo.load(original)
    changed = deepcopy(loaded)
    changed["capital"] += 25.0
    repo.save(original, changed, digest)
    account.audit()
    assert account.effective_capital() == changed["capital"]
    assert len(account.history()) == 1
    replica = repo.copy(original, history=True)
    copied, _ = repo.load(replica)
    assert copied["strategy"] == strategy
    assert replica != original
    Store(replica).audit()
    assert Store(replica).read(include_charts=False) == account.read(include_charts=False)
    assert len(Store(replica).history()) == 1


def test_mass_transfer_preserves_eight_independent_paper_accounts(tmp_path):
    repo = Repository(tmp_path / "workspace")
    originals = []
    for strategy in STRATEGIES:
        cfg = defaults(strategy)
        cfg["name"] = "Mass " + strategy
        folder = repo.create(cfg)
        Store(folder, cfg["capital"]).audit()
        originals.append(folder)
    archive = tmp_path / "transfer.zip"
    export_bots(repo, originals, archive, history=True)
    restored = import_bots(repo, archive, history=True)
    assert len(restored) == len(originals) == len(STRATEGIES)
    assert len(repo.list()) == 2 * len(STRATEGIES)
    for before, after in zip(originals, restored):
        assert before != after
        old, _ = repo.load(before)
        new, _ = repo.load(after)
        assert old["strategy"] == new["strategy"]
        assert Store(before).read(include_charts=False) == Store(after).read(include_charts=False)
        Store(after).audit()
