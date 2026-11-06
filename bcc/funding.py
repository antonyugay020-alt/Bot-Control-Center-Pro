"""Public funding rates with an explicit minute-mark estimate of funding value."""

import math

from .trading import event
from .position_journal import cycle_reference, receipt


def apply_funding(state, events, symbol, rates, marks, now):
    processed = state["strategy"].setdefault("funding_events", {})
    holdings = [(key, p, False) for key, p in state["positions"].items()]
    archive = state.get("funding_archive", {})
    holdings.extend((p["key"], p, True) for p in archive.values())
    for key, p, closed in holdings:
        if p["symbol"] != symbol or p.get("kind") == "POLYMARKET":
            continue
        for rate in rates:
            at = rate["at"]
            identifier = f"{cycle_reference(key, p)}:{at}"
            if at > now or at < p["opened"] or processed.get(identifier):
                continue
            mark = marks.get(at)
            if mark is None:
                continue
            if (
                not all(
                    type(x) in (int, float) and math.isfinite(x)
                    for x in (rate["rate"], mark)
                )
                or mark <= 0
            ):
                raise ValueError("Некорректные данные funding")
            qty = sum(
                x["delta"] for x in p.get("quantity_timeline", []) if x["at"] <= at
            )
            if qty <= 0:
                continue
            charge = qty * mark * rate["rate"] * (1 if p["side"] == "LONG" else -1)
            state["cash"] -= charge
            if closed:
                stats = state["stats"][key]
                was_win = p["cycle_pnl"] > 0
                p["cycle_pnl"] -= charge
                stats["net_pnl"] -= charge
                stats["wins"] += int(p["cycle_pnl"] > 0) - int(was_win)
            else:
                p["fees"] += charge
                p["funding_total"] = p.get("funding_total", 0) + charge
            processed[identifier] = True
            event(
                events,
                symbol,
                p["section"],
                "FUNDING",
                price=mark,
                qty=qty,
                fee=charge,
                cash_delta=-charge,
                net_pnl=-charge,
                rate=rate["rate"],
                funding_at=at,
                fee_model="public_rate_minute_mark_estimate",
                side=p['side'],
                **receipt(p, key),
            )
    published_through = max((r["at"] for r in rates if r["at"] <= now), default=0)
    for identifier, p in list(archive.items()):
        missing = any(
            p["opened"] <= r["at"] < p["closed"]
            and not processed.get(f"{cycle_reference(p['key'], p)}:{r['at']}")
            and sum(x["delta"] for x in p["quantity_timeline"] if x["at"] <= r["at"])
            > 0
            for r in rates
        )
        if p["symbol"] == symbol and published_through >= p["closed"] and not missing:
            del archive[identifier]
