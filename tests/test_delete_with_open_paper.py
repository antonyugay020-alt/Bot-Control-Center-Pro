"""Regression: Delete intentionally discards stopped PAPER state with confirmation.

Stop and Delete are distinct operations. A running bot or an unconfirmed
attempt may never be deleted, but open PAPER positions must not force a
market close when the user explicitly chooses irreversible deletion.
"""

import json
import sqlite3

import pytest

from bcc.config import defaults
from bcc.storage import BotBusy, BotLock, Repository, Store


def make_bot(tmp_path, *, strategy="VELES"):
    repo = Repository(tmp_path / "application")
    cfg = defaults(strategy)
    cfg["name"] = "Delete regression bot"
    bot = repo.create(cfg)
    ledger = Store(bot, cfg["capital"])
    return repo, bot, ledger


def fill_paper_positions(ledger, count=18):
    from test_core import attach
    from bcc.trading import enter, plan

    cfg = defaults("VELES")
    settings = cfg["settings"]
    with ledger.transaction() as (state, events):
        for i in range(count):
            attach(state)
            key = f"BTCUSDT:LONG_{i}"
            ok, reason = enter(
                state, events, key, "BTCUSDT", "LONG", settings,
                100, 1, plan(settings["orders"], "LONG", 100), f"LONG_{i}"
            )
            assert ok, reason
        state.pop("_books", None)
    ledger.audit()
    assert len(ledger.read()["positions"]) == count
    assert len(ledger.history(limit=100)) == count


def test_delete_with_18_positions_requires_explicit_ack_and_removes_entire_bot(tmp_path):
    repo, bot, ledger = make_bot(tmp_path)
    fill_paper_positions(ledger)
    summary = repo.deletion_impact(bot)
    assert summary == {"positions": 18, "pending": 0, "unknown": False}
    before = ledger.path.read_bytes()
    with pytest.raises(ValueError, match="явное подтверждение"):
        repo.delete(bot)
    assert ledger.path.read_bytes() == before
    assert len(repo.list()) == 1
    repo.delete(bot, discard_paper_state=True)
    assert not bot.exists()
    assert repo.list() == []
    assert not list(repo.bots.glob("*/.deleted-*"))


def test_pending_order_also_requires_explicit_ack(tmp_path):
    repo, bot, ledger = make_bot(tmp_path)
    with ledger.transaction(include_charts=False) as (state, events):
        state["pending"]["BTCUSDT:waiting"] = {"symbol": "BTCUSDT", "reason": "PAPER"}
    assert repo.deletion_impact(bot)["pending"] == 1
    with pytest.raises(ValueError, match="явное подтверждение"):
        repo.delete(bot)
    assert bot.is_dir()
    repo.delete(bot, discard_paper_state=True)
    assert not bot.exists()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_delete_broken_ledger_with_explicit_confirmation_only(tmp_path, damage):
    repo, bot, ledger = make_bot(tmp_path)
    if damage == "missing":
        ledger.path.unlink()
    else:
        ledger.path.write_bytes(b"broken sqlite")
    assert repo.deletion_impact(bot)["unknown"] is True
    with pytest.raises(ValueError, match="явное подтверждение"):
        repo.delete(bot)
    assert bot.is_dir()
    repo.delete(bot, discard_paper_state=True)
    assert not bot.exists()
    assert not ledger.path.exists()


def test_delete_is_blocked_if_bot_worker_holds_lock_even_after_ui_confirmation(tmp_path):
    repo, bot, ledger = make_bot(tmp_path)
    fill_paper_positions(ledger, count=1)
    with BotLock(bot):
        with pytest.raises(BotBusy):
            repo.delete(bot, discard_paper_state=True)
    assert bot.is_dir()
    assert len(ledger.read()["positions"]) == 1


def test_delete_new_empty_bot_without_state_does_not_require_special_ack(tmp_path):
    repo = Repository(tmp_path)
    cfg = defaults("VELES")
    cfg["name"] = "Empty delete regression"
    bot = repo.create(cfg)
    assert repo.deletion_impact(bot) == {"positions": 0, "pending": 0, "unknown": False}
    repo.delete(bot)
    assert not bot.exists()
