"""Execution boundary: strategy decisions are independent of their executor.

PaperExecutor consumes current public depth and lot constraints. A future live
executor implements this contract using exchange acknowledgements and fills;
the strategy never treats an order request as an executed position.
"""

import math
from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, ROUND_CEILING, Decimal

from .market_clock import ClockUnavailable, book_age_bounds, book_is_fresh


class ExecutionRejected(ValueError):
    pass


def _aligned_quantity(value, step, rounding):
    units = Decimal(str(value)) / step
    nearest = units.to_integral_value()
    if abs(units - nearest) < Decimal("0.00000001"):
        units = nearest
    return float(units.to_integral_value(rounding=rounding) * step)


def floor_quantity(value, step):
    return _aligned_quantity(value, step, ROUND_DOWN)


def ceil_quantity(value, step):
    return _aligned_quantity(value, step, ROUND_CEILING)


@dataclass(frozen=True)
class Fill:
    quantity: float
    price: float
    notional: float
    fee: float
    source_at: float
    requested_quantity: float
    source_age_low: float
    source_age_high: float
    full_close_request: bool = False

    def evidence(self):
        return dict(
            book_at=self.source_at,
            book_age_lower=self.source_age_low,
            book_age_upper=self.source_age_high,
            full_close_request=self.full_close_request,
        )

    @property
    def partial(self):
        return self.quantity < self.requested_quantity - max(
            1e-12, self.requested_quantity * 1e-10
        )


class PaperExecutor:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def estimate(
        self,
        side,
        leg,
        settings,
        *,
        notional=None,
        quantity=None,
        limit=None,
        _validated=False,
        consume=True,
        position_quantity=None,
    ):
        book = self.snapshot
        if side not in ("LONG", "SHORT") or leg not in ("ENTRY", "EXIT"):
            raise ExecutionRejected("Неверное направление исполнения")
        if position_quantity is not None:
            if (
                leg != "EXIT"
                or type(position_quantity) not in (int, float)
                or not math.isfinite(position_quantity)
                or position_quantity <= 0
                or type(quantity) not in (int, float)
                or not math.isfinite(quantity)
                or quantity > position_quantity + max(1e-12, position_quantity * 1e-10)
            ):
                raise ExecutionRejected(
                    "Выход не подтверждён объёмом существующей позиции"
                )
        if (quantity is None and notional is None) or any(
            type(v) not in (int, float) or not math.isfinite(v) or v <= 0
            for v in (quantity, notional)
            if v is not None
        ):
            raise ExecutionRejected("Неверный объём исполнения")
        if not book_is_fresh(book["at"]):
            raise ExecutionRejected("Устаревший стакан: PAPER-исполнение отложено")
        if any(
            type(book.get(k)) not in (int, float)
            or not math.isfinite(book[k])
            or book[k] <= 0
            for k in ("qty_step", "min_qty", "min_notional")
        ):
            raise ExecutionRejected("Повреждены ограничения объёма")
        maximum = book.get("max_qty")
        if maximum is not None and (
            type(maximum) not in (int, float)
            or not math.isfinite(maximum)
            or maximum <= 0
        ):
            raise ExecutionRejected("Повреждён максимум объёма")
        if limit is not None and (
            type(limit) not in (int, float) or not math.isfinite(limit) or limit <= 0
        ):
            raise ExecutionRejected("Неверная лимитная цена")
        buy = (side == "LONG") == (leg == "ENTRY")
        levels = book.get("asks" if buy else "bids")
        # Validate the entire executable side before touching any depth.  A
        # mixed list/tuple snapshot could otherwise fill the first level and
        # fail while assigning to a later immutable row, leaving partially
        # consumed liquidity after an unsuccessful PAPER execution.
        if (
            not isinstance(levels, list)
            or not levels
            or any(
                not isinstance(level, list)
                or len(level) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v)
                       for v in level)
                or level[0] <= 0
                or level[1] < 0
                for level in levels
            )
        ):
            raise ExecutionRejected("Пустой или повреждённый стакан")
        if any(
            previous[0] >= current[0] if buy else previous[0] <= current[0]
            for previous, current in zip(levels, levels[1:])
        ):
            raise ExecutionRejected("Нарушен порядок уровней стакана")
        best = next((p for p, q in levels if q > 0), None)
        if best is None:
            raise ExecutionRejected("Пустой стакан")
        buy_factor = 1 + settings["slippage_pct"] / 100 if buy else 1
        qty = quantity if quantity is not None else notional / (best * buy_factor)
        step = Decimal(str(book["qty_step"]))
        # Quantities are accumulated as floats in the state document. Restore an
        # already aligned unit count within floating point error before flooring.
        qty = floor_quantity(qty, step)
        full_close = position_quantity is not None and qty >= position_quantity - max(
            1e-12, position_quantity * 1e-10
        )
        if (
            qty <= 0
            or not _validated
            and not full_close
            and qty < book["min_qty"]
            or not _validated
            and leg == "ENTRY"
            and qty * best < book["min_notional"]
        ):
            minimum = max(
                book["min_qty"] * best, book["min_notional"] if leg == "ENTRY" else 0
            )
            raise ExecutionRejected(
                f"Объём ниже биржевого минимума: {qty:g}; минимум {book['min_qty']:g}, шаг {book['qty_step']:g}. Минимальный номинал около {minimum:.8g} USDT"
            )
        if book.get("max_qty") is not None and qty > book["max_qty"] + max(
            1e-12, book["max_qty"] * 1e-12
        ):
            raise ExecutionRejected("Объём выше биржевого максимума рыночного ордера")
        left = qty
        cost = 0
        consumed = []
        for idx, (price, size) in enumerate(levels):
            if limit is not None and (price > limit if buy else price < limit):
                break
            take = min(left, size)
            if take > 0:
                cost += take * price
                left -= take
                consumed.append((idx, take))
            if left <= max(1e-12, qty * 1e-10):
                break
        if left > max(1e-12, qty * 1e-10):
            if settings["execution_policy"] == "ioc":
                # Sum executed levels directly. Subtracting almost equal floats
                # could turn exactly 0.2 into 0.199999... and discard a whole lot.
                available = floor_quantity(
                    sum(Decimal(str(take)) for _, take in consumed), step
                )
                if available <= 0:
                    raise ExecutionRejected("Нет исполнимого объёма в стакане")
                fill = self.estimate(
                    side,
                    leg,
                    settings,
                    quantity=available,
                    notional=notional,
                    limit=limit,
                    _validated=True,
                    consume=consume,
                    position_quantity=position_quantity,
                )
                return replace(
                    fill, requested_quantity=qty, full_close_request=full_close
                )
            raise ExecutionRejected(
                "Недостаточная глубина: полный PAPER-ордер не исполнен"
            )
        # Notional is an upper bound. Depth impact can otherwise spend more than
        # the reserved order budget after sizing from the best price.
        adjusted_cost = cost * (1 + settings["slippage_pct"] / 100 * (1 if buy else -1))
        if notional is not None and adjusted_cost > notional * (1 + 1e-10):
            qty = floor_quantity(qty * notional / adjusted_cost, step)
            if qty <= 0:
                raise ExecutionRejected("Недостаточный бюджет с учётом глубины")
            return self.estimate(
                side,
                leg,
                settings,
                quantity=qty,
                limit=limit,
                consume=consume,
                position_quantity=position_quantity,
            )
        avg = cost / qty
        adverse = 1 if buy else -1
        adjusted = avg * (1 + adverse * settings["slippage_pct"] / 100)
        if limit is not None and (adjusted > limit if buy else adjusted < limit):
            adjusted = limit
        fee = adjusted * qty * settings["fee_rate"]
        try:
            age_low, age_high = book_age_bounds(book["at"])
        except (ClockUnavailable, ValueError, TypeError) as exc:
            raise ExecutionRejected("Недоступно время исполнения") from exc
        if age_low < -0.5 or age_high > 3:
            raise ExecutionRejected("Стакан устарел во время расчёта PAPER-исполнения")
        # Mutate only after a complete estimate. The same snapshot cannot provide
        # its full top-level liquidity to every independent section.
        if consume:
            for idx, take in consumed:
                levels[idx][1] = max(0, levels[idx][1] - take)
        return Fill(
            qty,
            adjusted,
            qty * adjusted,
            fee,
            book["at"],
            qty,
            age_low,
            age_high,
            full_close,
        )
