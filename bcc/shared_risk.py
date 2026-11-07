"""Optional portfolio limits over the authoritative per-bot PAPER ledgers.

A short OS lease serializes allocation decisions across workers. No second
balance or reservation ledger is maintained: every lease rereads committed
accounts, including stopped bots. Crashes release the lease and SQLite rolls
back the unfinished local transaction. Existing reserved grids and exits can
continue when a new allocation is blocked.
"""

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sqlite3

from .config import number
from .storage import BotBusy, BotLock, Store, atomic_json, load_json, iter_paper_accounts
from .margin import (
    MODEL,
    MarginUnavailable,
    margin_status,
    pending_reserve,
    reserved_position,
)

DEFAULT_POLICY = {"max_positions": 0, "max_committed_usdt": 0.0}


def policy_path(root):
    return Path(root) / "shared_risk.json"


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != set(DEFAULT_POLICY):
        raise ValueError("Неверная конфигурация общего риск-менеджера")
    number(policy["max_positions"], 0, 1_000_000, "Общий лимит позиций", integer=True)
    number(policy["max_committed_usdt"], 0, 1e12, "Общий лимит обеспечения PAPER")
    return deepcopy(policy)


def load_policy(root):
    path = policy_path(root)
    return (
        validate_policy(load_json(path)) if path.exists() else deepcopy(DEFAULT_POLICY)
    )


def save_policy(repo, life, policy, *, coordinator=None):
    from .coordination import BusyOwner, Coordinator

    policy = validate_policy(policy)
    control = coordinator or Coordinator(repo.root / 'coordination.sqlite')
    control.initialize()

    def verify_stopped():
        if any(life.status(folder) != "Остановлен" for folder, _ in repo.list()):
            raise BotBusy(
                "Остановите ботов перед изменением общих лимитов; счета и позиции сохранятся"
            )
    try:
        return control.quiescent_operator_change(
            verify_stopped, lambda: atomic_json(policy_path(repo.root), policy))
    except BusyOwner as exc:
        raise BotBusy(str(exc)) from exc


def exposure(state):
    slots = len(state["positions"]) + len(state["pending"])
    committed = sum(
        (
            p["cost"] + p.get("fee", 0)
            if p.get("kind") == "POLYMARKET"
            else reserved_position(state, p)
        )
        for p in state["positions"].values()
    ) + sum(pending_reserve(state, p) for p in state["pending"].values())
    return slots, committed


def allocation_reason(state, committed, slots=1):
    risk = state.get("_shared_risk")
    if not risk:
        return ""
    if risk.get("error"):
        return "Общий риск-менеджер: " + risk["error"]
    own_slots, own_committed = exposure(state)
    policy = risk["policy"]
    count = risk["other_slots"] + own_slots + slots
    amount = risk["other_committed"] + own_committed + committed
    if policy["max_positions"] and count > policy["max_positions"]:
        return f"Общий лимит позиций и сеток: {count} > {policy['max_positions']}"
    if policy["max_committed_usdt"] and amount > policy["max_committed_usdt"] + 1e-8:
        return f"Общий лимит обеспечения PAPER: {amount:g} > {policy['max_committed_usdt']:g} USDT"
    return ""


def portfolio_snapshot(root, exclude=None, *, require_prices=False):
    slots, committed = 0, 0.0
    for folder in iter_paper_accounts(root):
        if exclude is not None and folder / 'paper.sqlite' == exclude:
            continue
        account = Store(folder).read(include_charts=False)
        if require_prices and account.get("margin_model") == MODEL:
            error = margin_status(account)
            if error:
                raise MarginUnavailable(error)
        count, amount = exposure(account)
        slots += count
        committed += amount
    return slots, committed


@contextmanager
def risk_transaction(store, *, include_charts=False):
    folder = store.path.parent
    root = (
        folder.parents[2]
        if len(folder.parents) > 2 and folder.parent.parent.name == "MY_BOTS"
        else None
    )
    if root is None:
        with store.transaction(include_charts=include_charts) as values:
            yield values
        return
    try:
        policy = load_policy(root)
        enabled = any(policy.values())
        risk = {"policy": policy, "other_slots": 0, "other_committed": 0.0}
    except (OSError, ValueError, TypeError) as exc:
        enabled, risk = True, {"error": str(exc)}
    lease = None
    try:
        if enabled and not risk.get("error"):
            try:
                lock_folder = root / ".shared_risk"
                lock_folder.mkdir(exist_ok=True)
                lease = BotLock(lock_folder)
                lease.__enter__()
            except (BotBusy, OSError) as exc:
                lease = None
                risk["error"] = (
                    "другая операция распределяет капитал"
                    if isinstance(exc, BotBusy)
                    else str(exc)
                )
            if lease:
                try:
                    risk["other_slots"], risk["other_committed"] = portfolio_snapshot(
                        root,
                        exclude=store.path,
                        require_prices=bool(policy["max_committed_usdt"]),
                    )
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    RuntimeError,
                    sqlite3.Error,
                ) as exc:
                    risk["error"] = "не удалось проверить сохранённые счета: " + str(
                        exc
                    )
        with store.transaction(include_charts=include_charts) as (state, events):
            if enabled:
                state["_shared_risk"] = risk
            try:
                yield state, events
            finally:
                state.pop("_shared_risk", None)
    finally:
        if lease:
            lease.__exit__(None, None, None)
