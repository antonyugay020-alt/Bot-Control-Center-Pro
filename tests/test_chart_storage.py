"""Chart history remains atomic and portable without rewriting all charts per tick."""

import json

import pytest
from test_core import bars, config
from test_execution_runtime import open_position

from bcc.storage import AccountingError, Store


def legacy_store(folder):
    store = Store(folder, 500)
    # Construct the pre-09 state document, as an existing Windows account has it.
    state = store.read()
    state["charts"] = {f"COIN{i}USDT": bars([99, 100, 101]) for i in range(813)}
    with store.connect() as db:
        db.execute("UPDATE state SET document=? WHERE id=1", (json.dumps(state),))
    return store


def test_migration_retains_813_charts_and_fast_transaction_changes_only_one(tmp_path):
    store = legacy_store(tmp_path)
    before = store.read()
    assert store.chart_symbols() == list(before["charts"])
    assert store.chart("COIN812USDT") == before["charts"]["COIN812USDT"]
    with store.transaction(include_charts=False) as (state, events):
        assert state["charts"] == {}
    assert store.read() == before
    with store.connect() as db:
        raw = db.execute("SELECT document FROM state").fetchone()[0]
        assert len(raw) < 1000 and json.loads(raw)["_chart_table"] == 1
        db.execute("CREATE TABLE chart_writes(symbol TEXT)")
        db.execute(
            "CREATE TRIGGER track_chart_insert AFTER INSERT ON charts BEGIN INSERT INTO chart_writes VALUES(new.symbol); END"
        )
    changed = bars([10, 20, 30])
    with store.transaction(include_charts=False) as (state, events):
        state["charts"]["COIN0USDT"] = changed
    with store.connect() as db:
        assert db.execute("SELECT symbol FROM chart_writes").fetchall() == [
            ("COIN0USDT",)
        ]
    assert len(store.chart_symbols()) == 813
    assert store.chart("COIN0USDT") == changed
    assert store.chart("COIN812USDT") == before["charts"]["COIN812USDT"]
    assert store.read(False)["charts"] == {}
    store.audit()


def test_chart_position_and_execution_roll_back_together_then_survive_backup(tmp_path):
    store = legacy_store(tmp_path)
    before = store.read()
    settings = config()["settings"]
    with pytest.raises(RuntimeError, match="rollback"):
        with store.transaction(include_charts=False) as (state, events):
            open_position(state, events, settings)
            state.pop("_books")
            state["charts"]["BTCUSDT"] = bars([100, 101])
            raise RuntimeError("rollback")
    assert store.read() == before and store.history() == []
    with store.transaction(include_charts=False) as (state, events):
        open_position(state, events, settings)
        state.pop("_books")
        state["charts"]["BTCUSDT"] = bars([100, 101])
    store.audit()
    destination = tmp_path / "backup"
    destination.mkdir()
    store.backup(destination / "paper.sqlite")
    restored = Store(destination)
    assert restored.read() == store.read()
    assert restored.history() == store.history()
    restored.audit()


def test_legacy_or_new_bad_chart_data_never_commits(tmp_path):
    store = legacy_store(tmp_path)
    before = store.read()
    with pytest.raises(AccountingError):
        with store.transaction(include_charts=False) as (state, events):
            state["charts"]["BTCUSDT"] = [{"close": float("nan")}]
    assert store.read() == before
    with store.transaction(include_charts=False):
        pass
    with pytest.raises(AccountingError):
        with store.transaction(include_charts=False) as (state, events):
            state["cash"] -= 1
    store.audit()
