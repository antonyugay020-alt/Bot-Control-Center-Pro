"""Bot-wide finish policy, separate from immediate process Stop and deal exits."""

import logging
from pathlib import Path

from .storage import load_json


def stop_requested(folder, token):
    path = folder / "stop.request"
    try:
        command = load_json(path)
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError) as exc:
        # A malformed optional request must not crash the trading worker;
        # the operator can replace it with an authenticated stop command.
        logging.error("Invalid stop.request: %s", exc)
        return False
    if not isinstance(command, dict):
        logging.error("Invalid stop.request: expected an object")
        return False
    # Every supported stop request carries a command ID.  A partially
    # written or otherwise malformed request must never stop a bot.
    if not isinstance(command.get("id"), str) or not command["id"]:
        return False
    if "token" in command:
        # Wrong-token requests may belong to a stopped, older launch.
        return type(command["token"]) is str and command["token"] == token
    # Legacy requests were untagged but did carry their own command IDs.
    return True


def arm_run(state, cfg):
    control = state["strategy"]
    control["finish_on_close"] = cfg.get("stop_after_deals", False)
    control["draining"] = bool(
        (control.get("draining") or control["finish_on_close"])
        and (state["positions"] or state["pending"])
    )


def deal_closed(state):
    if state["strategy"].get("finish_on_close"):
        state["strategy"]["draining"] = True


def claim_command(folder, filename):
    """Durably claim one replaceable controller command without losing updates.

    The worker is the sole reader under BotLock. A previous .processing file
    is replayed after a crash; a new request can arrive while it is handled
    without being deleted by the old command's acknowledgement.
    """
    folder = Path(folder)
    incoming = folder / filename
    processing = folder / ("." + filename + ".processing")
    if processing.exists():
        return processing
    try:
        incoming.replace(processing)
    except FileNotFoundError:
        return None
    return processing


def finish_requested(folder, store, *, expected_token=None):
    # Process the oldest durable in-flight request first. Then consume any
    # newer command that arrived while the previous transaction was running.
    # A bounded loop prevents controller spam from monopolizing the worker.
    for _ in range(16):
        path = claim_command(folder, "finish.request")
        if path is None:
            return
        command = load_json(path)
        if not isinstance(command, dict) or not isinstance(command.get("id"), str) or not command["id"]:
            raise ValueError("Повреждена команда завершения")
        # A filesystem command belongs to the verified worker incarnation.
        # A previous worker may have crashed after the command was published,
        # leaving its .processing file behind. Never replay that command into
        # a newer launch, even if the bot name and PAPER account are unchanged.
        if expected_token is not None and command.get("token") != expected_token:
            logging.warning("Ignoring finish intent from an obsolete worker launch")
            path.unlink()
            continue
        draining = command.get("draining", True)
        if type(draining) is not bool:
            raise ValueError("Неверный режим завершения сделок")
        with store.transaction(include_charts=False) as (state, events):
            if state["strategy"].get("finish_command") != command["id"]:
                state["strategy"]["draining"] = draining
                if not draining:
                    state["strategy"]["finish_on_close"] = False
                state["strategy"]["finish_command"] = command["id"]
        path.unlink()
        logging.info("FINISH policy: draining=%s", draining)


def finished(state):
    return bool(
        state["strategy"].get("draining")
        and not state["positions"]
        and not state["pending"]
    )
