"""Offline synthetic-market acceptance: actual engine + SQLite, no real orders."""
from copy import deepcopy

import pytest

from bcc.config import validate
from bcc.storage import Store, Repository
from bcc.worker import process_polymarket
from bcc.strategies import Veles
from bcc.trading import enter, plan, exit_position
from test_core import attach, bars
from test_poly_contract_seconds import cfg, ScheduledSource
from test_veles_rsi_continuity import setup_positions, fake_signal


def test_polymarket_full_cycle_pause_reopen_official_settle_once(tmp_path):
    configuration = cfg()
    configuration.update(name="paper acceptance", symbols=["BTC"])
    configuration["polymarket"]["durations"] = [300]
    configuration = validate(configuration)
    repo = Repository(tmp_path)
    bot = repo.create(configuration)
    feed = ScheduledSource(42)
    with Store(bot, configuration["capital"]).transaction() as (state, events):
        process_polymarket(configuration, state, events, feed, feed.now)
    one = Store(bot)
    assert len(one.read()["positions"]) == 1
    open_event = one.history()[0]
    assert open_event["action"] == "OPEN"
    immutable = deepcopy(next(iter(one.read()["positions"].values()))["entry_rule"])
    one.audit()

    # Stop/restart is simulated by closing and reopening the store; unresolved
    # official outcome must leave the ledger exactly unchanged.
    suspended = Store(bot)
    balance_before = suspended.read()["cash"]
    with suspended.transaction() as (state, events):
        process_polymarket(configuration, state, events, feed, feed.now + 43)
    assert suspended.read()["cash"] == balance_before
    assert len(suspended.read()["positions"]) == 1

    # Later official resolution, new database handle after another restart.
    feed.resolved = True
    resumed = Store(bot)
    with resumed.transaction() as (state, events):
        process_polymarket(configuration, state, events, feed, feed.now + 43)
    assert not resumed.read()["positions"]
    actions = [e["action"] for e in reversed(resumed.history())]
    assert actions == ["OPEN", "SETTLE"]
    assert open_event["applied_entry_rule"] == immutable
    settled_balance = resumed.read()["cash"]
    with Store(bot).transaction() as (state, events):
        process_polymarket(configuration, state, events, feed, feed.now + 43)
    final = Store(bot)
    assert [e["action"] for e in reversed(final.history())] == actions
    assert final.read()["cash"] == pytest.approx(settled_balance)
    assert final.read()["stats"]["BTC:300"]["cycles"] == 1
    final.audit()


def test_veles_full_close_open_close_on_one_sqlite_ledger(tmp_path, monkeypatch):
    configuration, _, _, source_key = setup_positions()
    configuration["name"] = "veles acceptance"
    configuration = validate(configuration)
    store = Store(tmp_path, 500)
    with store.transaction() as (state, events):
        attach(state)
        section = configuration["sections"][0]
        success, reason = enter(state, events, source_key, "BTCUSDT", "LONG",
            configuration["settings"], 100, 50,
            plan(configuration["settings"]["orders"], "LONG", 100), section["id"])
        assert success, reason
        state["positions"][source_key]["exit_signal"] = deepcopy(section["exit"])
        state.pop("_books")
    store.audit()

    monkeypatch.setattr("bcc.strategies.signal", fake_signal)
    with Store(tmp_path).transaction() as (state, events):
        attach(state)
        Veles().process(configuration, state, events, "BTCUSDT", bars([100,100]),
                        99.99,100.01,120000)
        assert [e["action"] for e in events] == ["CLOSE", "OPEN"]
        state.pop("_books")
    nextkey = "BTCUSDT:" + configuration["sections"][1]["id"]
    assert list(Store(tmp_path).read()["positions"]) == [nextkey]
    with Store(tmp_path).transaction() as (state, events):
        attach(state, price=105)
        ok = exit_position(state, events, nextkey, 105, "SIGNAL_EXIT")
        assert ok
        state.pop("_books")
    final = Store(tmp_path)
    final.audit()
    assert not final.read()["positions"]
    assert [e["action"] for e in reversed(final.history())] == ["OPEN","CLOSE","OPEN","CLOSE"]
    assert final.read()["stats"][source_key]["cycles"] == 1
    assert final.read()["stats"][nextkey]["cycles"] == 1
    assert final.read()["cash"] == pytest.approx(500 + sum(e.get("cash_delta", 0) for e in final.history()))
