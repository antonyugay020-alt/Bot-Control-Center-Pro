"""One exclusive bot process. Ctrl+C/Stop preserves all PAPER positions."""

import argparse
import hashlib
import json
import logging
import math
import os
import signal
import time
import uuid
from copy import deepcopy
from pathlib import Path

from . import VERSION
from .config import effective, merge, poly_window_allows, resolve_poly_market, validate
from .control import arm_run, deal_closed
from .market import DataUnavailable, PolymarketData, PublicData, estimate_buy, verified_paper_market
from .market_clock import book_is_fresh, latest_now, market_now
from .launch_claim import verify_worker_token, complete_launch
from .shared_risk import allocation_reason
from .position_journal import receipt, terms_digest
from .storage import BotLock, Store, ControlRevoked, atomic_json
from .strategies import ENGINES, daily_ok
from .trading import equity, event


def required_bars(cfg, symbol, state=None):
    from .indicators import walk_conditions

    if cfg["strategy"] in ("GRID", "PUMP_DUMP"):
        return 150
    groups = []
    over = cfg["coin_overrides"].get(symbol, {})
    for s in cfg["sections"]:
        sec_over = over.get("sections", {}).get(s["id"], {})
        groups.extend(sec_over.get(k, over.get(k, s[k])) for k in ("entry", "exit"))
        settings = effective(cfg, symbol, s)
        groups.extend([settings["orders"]["signal"], settings["stop"]["signal"]])
    for p in (state or {}).get("positions", {}).values():
        if p["symbol"] == symbol and p.get("exit_signal"):
            groups.extend(
                [
                    p["exit_signal"],
                    p["settings"]["orders"]["signal"],
                    p["settings"]["stop"]["signal"],
                ]
            )
    from .indicators import EXTRA

    needs = []
    for g in groups:
        for c in walk_conditions(g):
            params = {**EXTRA.get(c["indicator"], {}), **c.get("params", {})}
            right = {
                **EXTRA.get(c.get("reference_indicator", c["indicator"]), {}),
                **c.get("reference_params", {}),
            }
            lengths = [max(c["period"], c.get("reference_period", 0)) * 3] + [
                int(p.get(k, 0))
                for p in (params, right)
                for k in ("rank", "slow", "signal", "kijun", "displacement", "streak")
            ]
            needs.append(
                (
                    max(lengths)
                    + max(c.get("shift", 0), c.get("reference_shift", 0))
                    + int(params.get("displacement", 0))
                    + int(right.get("displacement", 0))
                    + 30
                )
                * max(c["timeframe"], c.get("reference_timeframe", 1))
                + c["timeframe"]
            )
    return max([150] + needs)


def process_polymarket(cfg, state, events, source, now, should_stop=lambda: False):
    params = cfg["polymarket"]
    diagnostics = []
    # Settlement runs even after a coin/duration was disabled or its market ended.
    for key, p in list(state["positions"].items()):
        if should_stop():
            return
        if now < p["end"]:
            try:
                state["marks"][p["token"]] = source.outcome_quote(p["token"])
            except (DataUnavailable, ValueError, KeyError, TypeError):
                # An earlier mark may still pass the usual age check. Once a
                # refresh has failed integrity validation, it is no longer
                # safe collateral for another PAPER entry in this same tick.
                state["marks"].pop(p["token"], None)
                diagnostics.append(
                    {
                        "section": p["section"],
                        "reason": "Оценка позиции временно недоступна; исход ожидается официально",
                        "category": "market_error",
                    }
                )
            continue
        try:
            value = source.payout(p["token"])
        except (DataUnavailable, ValueError, KeyError, TypeError):
            value = None
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not 0 <= value <= 1):
            value = None
        if value is None:
            diagnostics.append(
                {"section": key, "reason": "Ожидание официального исхода"}
            )
            continue
        payout = p["shares"] * value
        net = payout - p["cost"] - p["fee"]
        state["cash"] += payout
        event(
            events,
            p["symbol"],
            p["section"],
            "SETTLE",
            price=value,
            qty=p["shares"],
            fee=0,
            cash_delta=payout,
            net_pnl=payout - p["cost"],
            cycle_net_pnl=net,
            reason="OFFICIAL_RESOLUTION",
            side=p["side"],
            slug=p["slug"],
            **receipt(p, key),
        )
        stats = state["stats"].setdefault(
            p["section"], {"cycles": 0, "wins": 0, "net_pnl": 0}
        )
        stats["cycles"] += 1
        stats["wins"] += int(net > 0)
        stats["net_pnl"] += net
        if p.get("settings", effective(cfg, p["symbol"]))["stop_after_cycle"]:
            state["done"]["cycle:" + p["section"]] = True
        del state["positions"][key]
        deal_closed(state)
    for coin in cfg["symbols"]:
        if state["strategy"].get("draining"):
            break
        finance = effective(cfg, coin)
        if finance["disabled"]:
            continue
        for duration in params["durations"]:
            if should_stop():
                return
            section = f"{coin}:{duration}"
            settings = resolve_poly_market(cfg, coin, duration)
            if settings["disabled"]:
                continue
            if finance["stop_after_cycle"] and state["done"].get("cycle:" + section):
                diagnostics.append(
                    {
                        "section": section,
                        "reason": "Остановлено после завершения сделки",
                    }
                )
                continue
            if not daily_ok(cfg, state, coin, finance, int(now * 1000)):
                diagnostics.append(
                    {"section": section, "reason": "Достигнут дневной лимит убытка"}
                )
                continue
            try:
                market = source.discover(coin, duration, market_now(now))
                # Recheck the admission boundary even if a future adapter or
                # test source bypasses Gamma discovery validation.
                verified_tokens = verified_paper_market(market)
                now = market_now(now)
                key = market["condition"]
                if state["done"].get(key):
                    why = "Уже торговали этот рынок"
                else:
                    from .market_clock import utc_bounds, clock
                    low, high = ((now, now) if clock._reference is None
                                 and clock._attempted is None else utc_bounds())
                    if not poly_window_allows(
                        settings, market["start"], market["end"], low, high
                    ):
                        why = "Ожидание окна входа"
                    else:
                        usd, pct = source.impulse(coin, market["start"], now)
                        impulse = usd if settings["impulse_mode"] == "usd" else pct
                        if any(type(x) not in (int, float) or not math.isfinite(x)
                               for x in (usd, pct)):
                            raise DataUnavailable("Некорректный импульс рынка")
                        if abs(impulse) < settings["impulse"]:
                            why = "Импульс ниже порога"
                        else:
                            from .poly_indicators import check_filter
                            indicator_ok, indicator_evidence = check_filter(
                                settings.get("indicator_filter"), source, coin, now)
                            if not indicator_ok:
                                diagnostics.append({
                                    "section": section,
                                    "reason": "Фильтр индикаторов не выполнен или данные не подтверждены",
                                })
                                continue
                            side = "UP" if impulse > 0 else "DOWN"
                            token = verified_tokens[side]
                            terms = source.execution_terms(
                                market, settings["fee_mode"] == "public"
                            )
                            schedule = terms["fee_schedule"]
                            asks, stamp = source.book(token)
                            best = min(
                                (
                                    float(x["price"])
                                    for x in asks
                                    if float(x["size"]) > 0
                                ),
                                default=2,
                            )
                            if not settings["ask_min"] <= best <= settings["ask_max"]:
                                why = "ASK вне коридора"
                            else:
                                if cfg["reinvest"]:
                                    value, unknown = equity(state)
                                    if unknown:
                                        diagnostics.append({
                                            "section": section,
                                            "reason": "Нет подтверждённой оценки открытых PAPER-позиций для реинвестирования",
                                        })
                                        continue
                                    base = max(0, value)
                                else:
                                    base = cfg["capital"]
                                budget = base * settings["stake_pct"] / 100
                                risk_reason = allocation_reason(state, budget)
                                if budget > state["cash"] or budget <= 0:
                                    why = "Недостаточно свободных средств"
                                elif risk_reason:
                                    why = risk_reason
                                else:
                                    cost, shares, fee = estimate_buy(
                                        asks,
                                        budget,
                                        settings["ask_max"],
                                        settings["fee_rate"],
                                        fee_schedule=schedule,
                                    )
                                    if shares < terms["min_size"]:
                                        raise DataUnavailable(
                                            "Объём ниже минимума ордера этого рынка"
                                        )
                                    if not book_is_fresh(stamp):
                                        raise DataUnavailable(
                                            "Стакан устарел до PAPER-исполнения"
                                        )
                                    # Revalidate time after network reads to avoid crossing expiry.
                                    if should_stop():
                                        return
                                    # Recheck BOTH edges after quotes, with the
                                    # same immutable policy used on first admission.
                                    final_low, final_high = utc_bounds()
                                    if not poly_window_allows(settings,
                                            market["start"], market["end"],
                                            final_low, final_high):
                                        raise DataUnavailable(
                                            "Окно PAPER-входа завершилось при сборе данных"
                                        )
                                    from .market_clock import book_age_bounds

                                    age_low, age_high = book_age_bounds(stamp)
                                    if age_low < -0.5 or age_high > 3:
                                        raise DataUnavailable(
                                            "Стакан устарел перед фиксацией PAPER-входа"
                                        )
                                    state["cash"] -= cost + fee
                                    state["done"][key] = True
                                    observation = dict(
                                        impulse_usd=usd, impulse_pct=pct,
                                        selected_impulse=impulse,
                                        observed_ask=best,
                                        book_at=stamp,
                                        **({"indicator_evidence": deepcopy(indicator_evidence)}
                                           if indicator_evidence is not None else {}),
                                        entry_ts_utc=final_low,
                                        seconds_into_market=final_low - market["start"],
                                        seconds_before_close=market["end"] - final_low,
                                        market_start=market["start"],
                                        market_end=market["end"],
                                    )
                                    state["positions"][key] = dict(
                                        position_id=uuid.uuid4().hex,
                                        kind="POLYMARKET",
                                        symbol=coin,
                                        section=section,
                                        side=side,
                                        token=token,
                                        shares=shares,
                                        qty=shares,
                                        average=cost / shares,
                                        cost=cost,
                                        fee=fee,
                                        margin=0,
                                        opened=final_low,
                                        entry_observation=observation,
                                        entry_rule=deepcopy({k: settings[k] for k in (
                                            "entry_signal_mode", "entry_seconds_before_close",
                                            "entry_window_seconds", "entry_min_seconds_left",
                                            "ask_min", "ask_max", "impulse", "impulse_mode",
                                            "stake_pct", "fee_mode", "fee_rate",
                                            "indicator_filter")}),
                                        end=market["end"],
                                        slug=market["slug"],
                                        settings=deepcopy(finance),
                                    )
                                    event(
                                        events,
                                        coin,
                                        section,
                                        "OPEN",
                                        **receipt(state['positions'][key], key),
                                        position_terms_sha256=terms_digest(state['positions'][key]),
                                        price=cost / shares,
                                        qty=shares,
                                        fee=fee,
                                        cash_delta=-cost - fee,
                                        net_pnl=-fee,
                                        side=side,
                                        slug=market["slug"],
                                        entry_ts_utc=final_low,
                                        seconds_into_market=final_low - market["start"],
                                        applied_entry_rule=deepcopy(state["positions"][key]["entry_rule"]),
                                        entry_observation=deepcopy(observation),
                                        duration=duration,
                                        book_at=stamp,
                                        book_age_lower=age_low,
                                        book_age_upper=age_high,
                                        fee_model="public_schedule"
                                        if schedule is not None
                                        else "configured_estimate",
                                        fee_schedule=schedule,
                                    )
                                    why = "PAPER-вход " + side
                diagnostics.append({"section": section, "reason": why})
            except (DataUnavailable, KeyError, TypeError, ValueError) as e:
                diagnostics.append({"section": section, "reason": str(e),
                                    "category": "market_error"})
    state["diagnostics"]["POLYMARKET"] = diagnostics
    # Inform the process supervisor of data degradation without promoting a
    # valid process to a fictitious healthy trading session.
    return sum(d.get("category") == "market_error" for d in diagnostics)


def run(folder, token, *, once=False, control_revision=None):
    folder = Path(folder).resolve()
    stop = False

    def request_stop(*args):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logging.info("INITIALIZE pid=%s parent=%s", os.getpid(), os.getppid())
    with BotLock(folder):
        verify_worker_token(folder, token)
        try:
            control = None
            key = None
            if control_revision is not None:
                from .coordination import Coordinator
                root = folder.parents[2]
                key = folder.relative_to(root / 'MY_BOTS').as_posix()
                control = Coordinator(root / 'coordination.sqlite')
                if not control.launch_allowed(key, control_revision):
                    raise RuntimeError('Устаревшая команда запуска: worker не допущен к PAPER-операциям')
            from .storage import recover_deposit_change
            recover_deposit_change(folder)
            raw = (folder / "config.json").read_bytes()
            cfg = validate(json.loads(raw))
            store = Store(folder, cfg["capital"])
            state = store.read(include_charts=False)
            store.audit()
            # This lease lives inside paper.sqlite, next to the ledger it guards.
            # The OS lock remains a startup/lifecycle guard, not trading authority.
            store.claim_execution(token, recovered_under_os_lock=True)
            if control is not None:
                store.bind_control_authority(control, key, control_revision)
            if not math.isclose(store.effective_capital(), cfg["capital"], rel_tol=1e-10, abs_tol=1e-7):
                raise ValueError("Депозит не совпадает с журналом PAPER-операций")
            # The controller may have issued STOP while account recovery was
            # being audited. Do not modify strategy state on an obsolete start.
            if control is not None and not control.launch_allowed(key, control_revision):
                raise RuntimeError('Команда запуска отозвана до PAPER-транзакции')
            # Never erase a controller finish/cancel request at startup: a
            # request written during the launch handshake must be replayed.
            with store.transaction(include_charts=False) as (state, events):
                if cfg["strategy"] != "POLYMARKET":
                    from .margin import MODEL
                    state["margin_model"] = MODEL
                arm_run(state, cfg)
            from .process_identity import current_creation_time

            started = current_creation_time()
            status = dict(
                app_version=VERSION,
                pid=os.getpid(),
                parent_pid=os.getppid(),
                created=started,
                token=token,
                config_hash=hashlib.sha256(raw).hexdigest(),
                active_config=cfg,
                status="RUNNING",
                heartbeat=time.time(),
                draining=state["strategy"].get("draining", False),
            )
            atomic_json(folder / "process.json", status)
            complete_launch(folder, token)
            source = PolymarketData() if cfg["strategy"] == "POLYMARKET" else PublicData()
            engine = None if cfg["strategy"] == "POLYMARKET" else ENGINES[cfg["strategy"]]()
            from .runtime import run_loop

            checked_at = 0.0
            permitted = True
            def should_stop():
                nonlocal checked_at, permitted
                from .control import stop_requested
                if stop:
                    status['exit_reason'] = 'os_signal'
                    return True
                if stop_requested(folder, token):
                    status['exit_reason'] = 'authorized_stop_request'
                    return True
                if control is not None and time.monotonic() - checked_at >= 0.2:
                    checked_at = time.monotonic()
                    # A durable stop is effective even if the host service dies
                    # before sending an OS signal to this worker.
                    permitted = control.execution_authorized(key, control_revision)
                if not permitted:
                    status['exit_reason'] = 'control_revision_revoked'
                return not permitted

            logging.info("START %s %s", cfg["strategy"], cfg["name"])
            try:
                run_loop(
                    folder,
                    cfg,
                    store,
                    source,
                    engine,
                    status,
                    should_stop,
                    required_bars,
                    process_polymarket,
                    once,
                    control_revision=control_revision,
                )
            except ControlRevoked:
                # A STOP winning the command/ledger gate is a normal, safe
                # shutdown. The uncommitted PAPER transaction was rolled back,
                # so it must not be classified as a worker crash or restart storm.
                status['exit_reason'] = 'control_revision_revoked'
            except BaseException as exc:
                status.update(status="ERROR", last_error=str(exc))
                raise
            finally:
                if status["status"] != "ERROR":
                    status["status"] = "STOPPED"
                status["heartbeat"] = time.time()
                atomic_json(folder / "process.json", status)
                (folder / "stop.request").unlink(missing_ok=True)
                if hasattr(source, "close"):
                    source.close()
                else:
                    source.session.close()
                store.release_execution()
                logging.info("STOP %s %s", cfg["name"], status["status"])
        except BaseException as exc:
            # Startup failures can happen before run_loop (invalid config,
            # missing ledger, transport initialization). Report them under
            # the worker's exclusive lease, never leave RUNNING on disk.
            from .process_identity import current_creation_time

            failure = dict(
                app_version=VERSION, pid=os.getpid(),
                parent_pid=os.getppid(), created=current_creation_time(),
                token=token, heartbeat=time.time(),
            )
            if "status" in locals() and status.get("token") == token:
                failure.update(status)
            failure.update(status="ERROR", heartbeat=time.time(),
                           last_error=f"{type(exc).__name__}: {exc}")
            atomic_json(folder / "process.json", failure)
            # No second Dashboard can steal this claim before the failure has
            # been recorded, because this worker still holds the OS lease.
            complete_launch(folder, token)
            if "source" in locals():
                try:
                    if hasattr(source, "close"):
                        source.close()
                    else:
                        source.session.close()
                except Exception:
                    logging.exception("Не удалось закрыть источник после сбоя")
            # A failure after ledger claim but before entering run_loop must
            # release the same generation. Otherwise STOP during account
            # recovery leaves a stale live lease and makes restart depend on
            # its TTL rather than the confirmed OS process death.
            if "store" in locals():
                try:
                    store.release_execution()
                except Exception:
                    logging.exception("Не удалось освободить PAPER lease после сбоя запуска")
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--control-revision", type=int)
    args = parser.parse_args()
    folder = Path(args.folder)
    logging.basicConfig(
        filename=folder / "engine.log",
        encoding="utf-8",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        run(folder, args.token, once=args.once, control_revision=args.control_revision)
    except Exception:
        logging.exception("Движок остановлен с ошибкой")
        raise


if __name__ == "__main__":
    main()
