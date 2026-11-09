"""The worker event loop: parallel reads, serial decisions, atomic commits."""

import json
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from .control import claim_command, finish_requested, finished
from .funding import apply_funding
from .market import DataUnavailable, PublicData, executable_quote
from .data_cache import CacheBusy
from .market_clock import book_is_fresh, market_now, observed_quote, quote_is_recent
from .margin import MODEL, attach_snapshot, margin_status
from .liquidation import REASONS, is_liquidating, status as liquidation_status
from .scheduler import MarketScheduler
from .portfolio_marks import commit_portfolio_marks
from .strategies import record_completed_exit
from .shared_risk import risk_transaction
from .storage import atomic_json, load_json
from .trading import (exit_position, fill_price, liquidations,
                      update_equity_metrics, valid_position_mark, protective_exit)


def log_events(events):
    for e in events:
        logging.info(
            "%s %s %s %s qty=%s price=%s fee=%s pnl=%s %s",
            e["symbol"],
            e["section"],
            e["action"],
            e.get("side", ""),
            e.get("qty", ""),
            e.get("price", ""),
            e.get("fee", ""),
            e.get("net_pnl", ""),
            e.get("reason", ""),
        )


def process_tick(cfg, store, engine, tick):
    now = market_now()
    symbol = tick.symbol
    book = tick.book
    if book is None:
        # Published funding is still valid accounting evidence when the book is
        # unavailable, or the coin was removed after its last trade was closed.
        with risk_transaction(store) as (state, events):
            # A rejected refresh revokes the prior mark immediately. A mark
            # that remains younger than the generic TTL must not authorize
            # another section's PAPER collateral after its source has failed.
            state["marks"].pop(symbol, None)
            # An unavailable executable book must revoke executable prices.
            # It is NOT evidence that the independent public margin mark or
            # risk tiers are invalid. MarginModel snapshot() still enforces
            # strict 3-second freshness and rejects missing public inputs.
            if state.get("margin_model") != MODEL:
                state.get("margin_data", {}).pop(symbol, None)
            apply_funding(state, events, symbol, tick.rates, tick.funding_marks, now)
            # Preserve both independent market failures. With no book we
            # cannot trade, but losing the history error makes a multi-bot
            # outage look like an unexplained lack of RSI signals.
            failures = []
            if tick.quote_error:
                failures.append({"section": "Стакан", "reason": tick.quote_error})
            if tick.history_error:
                failures.append({"section": "Свечи", "reason": tick.history_error})
            if failures:
                state["diagnostics"][symbol] = failures
            elif not tick.bars:
                state["diagnostics"][symbol] = [
                    {"section": "Рынок", "reason": "Нет стакана и пригодной истории свечей"}
                ]
            else:
                state["diagnostics"][symbol] = [
                    {"section": "Стакан", "reason": "Нет исполнимого стакана"}
                ]
            if tick.bars:
                state.setdefault("charts", {})[symbol] = tick.bars[-120:]
            update_equity_metrics(state)
        log_events(events)
        return
    if not book_is_fresh(book["at"]):
        raise DataUnavailable("Стакан устарел до торгового решения")
    bid, ask = executable_quote(book)
    with risk_transaction(store) as (state, events):
        attach_snapshot(state, symbol, book)
        state["marks"][symbol] = observed_quote(
            {"bid": bid, "ask": ask, "at": book["at"]}
        )
        state["_books"] = {symbol: book}
        apply_funding(state, events, symbol, tick.rates, tick.funding_marks, now)
        had_manual_pending = any(
            p.get("exit_pending", {}).get("reason") == "MANUAL_PAPER_CLOSE"
            for p in state["positions"].values()
        )
        for key, p in list(state["positions"].items()):
            if p["symbol"] == symbol and p.get("exit_pending") and not is_liquidating(p):
                pending = p["exit_pending"]
                exit_position(
                    state,
                    events,
                    key,
                    fill_price(p["side"], "EXIT", bid, ask, p["settings"]),
                    pending["reason"],
                    pending["ratio"],
                    limit=pending.get("limit"),
                    target_index=pending.get("target_index"),
                    execution_policy=pending.get("execution_policy"),
                )
                record_completed_exit(cfg, state, key, p, int(now * 1000))
        liquidation_breach = liquidations(state, events)
        # The MODEL portfolio is valued/liquidated exclusively on validated
        # public mark-price data. Do not substitute another coin's executable
        # depth freshness for portfolio solvency. Legacy financial models
        # continue to require a fresh book for every existing exposure.
        stale = ([
            p["symbol"]
            for p in state["positions"].values()
            if (not valid_position_mark(p, state["marks"].get(p["symbol"]))
                or not quote_is_recent(state["marks"].get(p["symbol"])))
        ] if state.get("margin_model") != MODEL else [])
        if stale:
            state["_entry_blocked"] = (
                "Нет свежих котировок открытых позиций: "
                + ", ".join(sorted(set(stale)))
            )
        if symbol not in cfg["symbols"]:
            state["_entry_blocked"] = (
                "Монета удалена из настроек; разрешены только выходы и учёт"
            )
        pending_manual = had_manual_pending or any(
            p.get("exit_pending", {}).get("reason") == "MANUAL_PAPER_CLOSE"
            for p in state["positions"].values()
        )
        if pending_manual:
            state["_entry_blocked"] = "Выполняется ручное закрытие PAPER-позиций"
        liquidating = any(is_liquidating(p) for p in state["positions"].values())
        margin_error = margin_status(state) if state.get("margin_model") == MODEL else ""
        if liquidating or liquidation_breach or margin_error:
            state["_entry_blocked"] = margin_error or "Сокращение риска; новые входы и DCA приостановлены"
        if not tick.bars or tick.history_error:
            for key, position in list(state["positions"].items()):
                if position["symbol"] != symbol or position.get("exit_pending") or is_liquidating(position):
                    continue
                if protective_exit(state, events, key, bid, ask) and key not in state["positions"]:
                    record_completed_exit(cfg, state, key, position, int(now * 1000))
            state["diagnostics"][symbol] = [{"section": "Свечи", "reason": tick.history_error or "Нет пригодной истории закрытых свечей"}]
        else:
            engine.process(cfg, state, events, symbol, tick.bars, bid, ask, int(now * 1000))
        if not liquidating:
            liquidations(state, events)
        for position in state["positions"].values():
            if is_liquidating(position):
                state["diagnostics"].setdefault(symbol, []).append({
                    "section": position["section"] + " / маржа",
                    "reason": liquidation_status(position) + (" · " + position["liquidation_error"] if position.get("liquidation_error") else ""),
                })
        state.pop("_books", None)
        state.pop("_entry_blocked", None)
        if tick.bars:
            state.setdefault("charts", {})[symbol] = tick.bars[-120:]
        update_equity_metrics(state)
    log_events(events)



def close_requested_positions(folder, store, books, *, control_revision=None):
    from pathlib import Path
    folder = Path(folder)
    # New GUI requests live in the same durable command registry as Start/Stop.
    # The PAPER worker is the sole financial executor. Legacy on-disk
    # requests remain supported for existing accounts; no GUI writes them.
    coordinator = None
    owner_key = None
    revision = None
    for parent in folder.parents:
        if parent.name == 'MY_BOTS':
            database = parent.parent / 'coordination.sqlite'
            if database.is_file():
                from .coordination import Coordinator
                coordinator = Coordinator(database)
                owner_key = folder.relative_to(parent).as_posix()
                revision = coordinator.pending_manual_close(owner_key, launch_revision=control_revision)
            break
    command = claim_command(folder, "close.request")
    if command is None and revision is None:
        return
    cmd = (load_json(command) if command is not None else
           {'id': f'control-close:{revision}'})
    if not isinstance(cmd, dict) or not isinstance(cmd.get("id"), str) or not cmd["id"]:
        raise ValueError("Повреждена команда закрытия PAPER-позиций")
    with store.transaction(include_charts=False) as (state, events):
        if state["strategy"].get("last_close_command") != cmd["id"]:
            state["_books"] = {s: b for s, b in books.items() if book_is_fresh(b["at"])}
            for position_key, p in list(state["positions"].items()):
                q = state["marks"].get(p["symbol"], {})
                price = q.get("bid" if p["side"] == "LONG" else "ask", p["average"])
                # Missing liquidity creates a durable pending exit; it can never
                # silently consume the command while leaving an untracked deal.
                exit_position(state, events, position_key, price, "MANUAL_PAPER_CLOSE")
            state["pending"].clear()
            state["strategy"]["last_close_command"] = cmd["id"]
            state.pop("_books", None)
    if command is not None:
        command.unlink()
    if revision is not None and command is None:
        # Cash, positions and the idempotency marker were committed before
        # acknowledging the controller. A crash here safely replays the same
        # revision; concurrent newer requests survive the revision CAS.
        coordinator.acknowledge_manual_close(owner_key, revision)
    log_events(events)


def run_loop(
    folder,
    cfg,
    store,
    source,
    engine,
    status,
    should_stop,
    history_size,
    polymarket,
    once=False,
    control_revision=None,
):
    scheduler = (
        MarketScheduler(source, cfg, history_size) if engine is not None else None
    )
    books = {}
    seen = set()
    next_heartbeat = 0
    next_poly = 0
    errors = {}
    next_perf = 0
    # A dedicated single in-flight Bybit ticker batch refreshes the entire
    # open portfolio independently of per-symbol depth/RSI scanning. One
    # missing executable book can no longer starve every collateral mark.
    portfolio_pool = None
    portfolio_job = None
    portfolio_due = 0.0
    portfolio_errors_at = 0.0
    perf = {"processed": 0, "refreshed": 0, "data_errors": 0, "last_tick_ms": 0.0}
    market_health = {"completed_ticks": 0, "valid_history_ticks": 0,
                     "market_errors": 0, "last_error": "", "last_symbol": "",
                     "last_completed_utc": None, "last_valid_history_utc": None}

    def heartbeat(force=False):
        nonlocal next_heartbeat
        if force or time.monotonic() >= next_heartbeat:
            status["heartbeat"] = time.time()
            # Durable read-only telemetry: distinguish a live worker from a
            # worker that has never received usable closed-candle history.
            status["market_health"] = dict(market_health)
            if scheduler is not None:
                status["market_health"]["scheduler_pressure"] = dict(
                    getattr(scheduler, "last_pressure", {})
                )
            status["draining"] = store.read(include_charts=False)["strategy"].get(
                "draining", False
            )
            atomic_json(folder / "process.json", status)
            next_heartbeat = time.monotonic() + 1

    try:
        while not should_stop():
            finish_requested(folder, store, expected_token=status.get("token"))
            if finished(store.read(include_charts=False)):
                status['exit_reason'] = 'drained'
                break
            if scheduler:
                close_requested_positions(folder, store, books, control_revision=control_revision)
                state = store.read(include_charts=False)
                exposed = sorted({p["symbol"] for p in state["positions"].values()}
                                 | {p["symbol"] for p in state.get("pending", {}).values()})
                # Official Bybit transport only. Replay adapters derived from
                # PublicData override their upstream feed and must not receive
                # a new real/external mark request through an inherited method.
                reader = source.portfolio_marks if type(source) is PublicData else None
                if callable(reader) and exposed:
                    if portfolio_job is not None and portfolio_job.done():
                        try:
                            marks = portfolio_job.result()
                            changed = commit_portfolio_marks(store, marks)
                            if changed:
                                market_health["portfolio_mark_count"] = changed
                                market_health["last_portfolio_mark_utc"] = time.time()
                        except (DataUnavailable, ValueError, KeyError, TypeError, OSError) as exc:
                            # No catch-up using stale quotes: the existing
                            # three-second margin validator fails closed.
                            if time.monotonic() - portfolio_errors_at > 30:
                                logging.warning("PORTFOLIO_MARKS: %s", exc)
                                portfolio_errors_at = time.monotonic()
                        finally:
                            portfolio_job = None
                    if portfolio_job is None and time.monotonic() >= portfolio_due:
                        if portfolio_pool is None:
                            portfolio_pool = ThreadPoolExecutor(
                                max_workers=1, thread_name_prefix="portfolio-mark")
                        portfolio_job = portfolio_pool.submit(reader, exposed)
                        portfolio_due = time.monotonic() + 1.0
                completed = scheduler.collect(state)
                for symbol, tick, error in completed:
                    if should_stop():
                        break
                    seen.add(symbol)
                    tick_start = time.monotonic()
                    try:
                        finish_requested(folder, store, expected_token=status.get("token"))
                        if error:
                            raise error
                        heartbeat()
                        if tick.book is not None and not book_is_fresh(tick.book["at"]):
                            # Refresh a quote that aged in the decision queue.
                            # The original exchange timestamp is never changed.
                            perf["refreshed"] += 1
                            tick.book = source.orderbook(symbol)
                        process_tick(cfg, store, engine, tick)
                        market_health["completed_ticks"] += 1
                        market_health["last_completed_utc"] = time.time()
                        market_health["last_symbol"] = symbol
                        if tick.bars and not tick.history_error:
                            market_health["valid_history_ticks"] += 1
                            market_health["last_valid_history_utc"] = time.time()
                        if tick.book is not None:
                            perf["processed"] += 1
                            books[symbol] = tick.book
                        else:
                            books.pop(symbol, None)
                            if tick.quote_error:
                                raise DataUnavailable(tick.quote_error)
                    except (
                        DataUnavailable,
                        ValueError,
                        KeyError,
                        IndexError,
                        TypeError,
                    ) as exc:
                        perf["data_errors"] += 1
                        market_health["market_errors"] += 1
                        market_health["last_symbol"] = symbol
                        market_health["last_error"] = str(exc)[:500]
                        # Do not reuse even a recently accepted book for a
                        # manual close after a failed market refresh.
                        books.pop(symbol, None)
                        with risk_transaction(store) as (state, events):
                            state["marks"].pop(symbol, None)
                            state["diagnostics"][symbol] = [
                                {"section": "Данные", "reason": str(exc)}
                            ]
                        old = errors.get(symbol, ("", 0))
                        if old[0] != str(exc) or time.time() - old[1] > 60:
                            logging.warning("%s data: %s", symbol, exc)
                            errors[symbol] = (str(exc), time.time())
                    perf["last_tick_ms"] = round(
                        (time.monotonic() - tick_start) * 1000, 3
                    )
                    heartbeat(force=bool(error or (tick is not None and tick.history_error)))
                close_requested_positions(folder, store, books, control_revision=control_revision)
            elif time.monotonic() >= next_poly:
                # External quote/settlement failures are recoverable; an
                # exception during the transaction rolls back the ENTIRE
                # Polymarket tick (cash, positions, executions, and marks).
                # Never turn a programming bug or damaged PAPER ledger into a
                # silent market error: only recognized transport/busy failures
                # may be retried.
                try:
                    with risk_transaction(store) as (state, events):
                        issues = polymarket(
                            cfg,
                            state,
                            events,
                            source,
                            time.time(),
                            lambda: should_stop() or (folder / "finish.request").exists(),
                        )
                        update_equity_metrics(state)
                except (DataUnavailable, CacheBusy, sqlite3.OperationalError) as exc:
                    if (isinstance(exc, sqlite3.OperationalError)
                            and not any(term in str(exc).lower() for term in ("locked", "busy"))):
                        raise
                    market_health["market_errors"] += 1
                    market_health["last_symbol"] = "POLYMARKET"
                    market_health["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
                    perf["data_errors"] += 1
                    logging.warning("POLYMARKET public data unavailable, tick rolled back: %s", exc)
                    # The failure is durable even if the process is stopped
                    # before its next healthy tick. Do not clear old positions.
                    heartbeat(force=True)
                    next_poly = time.monotonic() + 2
                else:
                    log_events(events)
                    seen.update(cfg["symbols"])
                    market_health["completed_ticks"] += 1
                    if type(issues) is int and issues > 0:
                        market_health["market_errors"] += issues
                        market_health["last_error"] = "Нет подтверждённых данных по отдельным рынкам Polymarket"
                        market_health["last_symbol"] = "POLYMARKET"
                        perf["data_errors"] += issues
                    else:
                        market_health["last_completed_utc"] = time.time()
                    heartbeat(force=True)
                    next_poly = time.monotonic() + 2
            else:
                time.sleep(0.1)
            heartbeat()
            if time.monotonic() >= next_perf:
                shared = getattr(source, "shared", None)
                logging.info(
                    "RUNTIME_PERF %s",
                    json.dumps({**perf, "cache": getattr(shared, "stats", None)}),
                )
                next_perf = time.monotonic() + 30
            if once and seen >= set(cfg["symbols"]):
                status['exit_reason'] = 'single_cycle'
                break
            if finished(store.read(include_charts=False)):
                status['exit_reason'] = 'drained'
                break
    finally:
        if portfolio_pool is not None:
            if portfolio_job is not None:
                portfolio_job.cancel()
            portfolio_pool.shutdown(wait=True, cancel_futures=True)
        if scheduler:
            scheduler.close()
