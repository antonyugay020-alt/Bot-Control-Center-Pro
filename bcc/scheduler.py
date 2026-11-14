"""Bounded parallel public reads; accounting stays on the single worker thread."""

import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

from .strategy_dispatch import StrategyDispatcher
from .market import DataUnavailable
from .market_clock import market_now, quote_is_recent, book_is_fresh


@dataclass
class MarketTick:
    symbol: str
    bars: list
    book: dict | None
    rates: list
    funding_marks: dict
    history_error: str = ""
    quote_error: str = ""
    margin_data: dict | None = None


class MarketScheduler:
    def __init__(self, source, cfg, history_size, *, workers=8, period=2):
        self.source = source
        self.cfg = cfg
        self.history_size = history_size
        self.period = period
        # Market fetches are bounded independently of PAPER accounting.
        # The financial state is still mutated only by the owning run_loop.
        self.pool = StrategyDispatcher(workers=workers, maximum_pending=workers, thread_name_prefix="public-market")
        self.capacity = workers
        self.jobs = {}
        self.due = {}
        self.cursor = 0
        self.priority_cursor = 0
        self.priority_limit = max(1, self.capacity - max(1, workers // 2))
        # With a single worker, reserving one priority slot would starve scans
        # forever. Alternate admissions only when BOTH queues are ready.
        self.scan_turn = False
        # Prevent fast symbols being re-fetched repeatedly while slower
        # symbols from the same zero-period test batch are still in flight.
        self._zero_period_round = set()
        # An indicator/funding GET can block for seconds. Open exposure has a
        # separate bounded quote lane; neither completed books nor their next
        # refresh may wait for those history futures. Legacy sources without
        # a dedicated position reader keep the combined transport adapter.
        self._separate_quotes = callable(getattr(source, 'position_orderbook', None))
        self.quote_capacity = max(1, workers // 2)
        self.quote_pool = None
        self.quote_jobs = {}
        self.quote_due = {}
        self.quote_cursor = 0
        self.quote_period = min(period, .5)
        self._history_only = set()
        self._history_results = {}
        self._quote_ready = {}

    def _fetch(self, symbol, required, positions, manage=True, urgent=False, mark_only=False, history_only=False):
        history_error = ''
        quote_error = ''
        book = None
        # Existing positions need executable prices for protection and
        # valuation BEFORE slow candles, instrument history or funding.
        priority_getter = getattr(self.source, 'position_orderbook', None)
        priority_capable = callable(priority_getter)
        if manage and urgent and priority_capable and not history_only:
            try:
                book = priority_getter(symbol)
            except (DataUnavailable, ValueError, KeyError, TypeError) as exc:
                quote_error = str(exc)
        if mark_only and urgent and priority_capable:
            # Deliver critical fresh mark immediately to the owner. No
            # candle/funding request may hold an unmarked position hostage.
            return MarketTick(symbol, [], book, [], {}, '', quote_error)
        bars = []
        if manage:
            try:
                bars = self.source.candles(symbol, required)
            except (DataUnavailable, ValueError, KeyError, TypeError) as exc:
                history_error = str(exc)
        rates = []
        marks = {}
        margin_data = None
        # Refresh slow risk tiers on the independent history/funding lane.
        # This result is committed by the single financial worker together
        # with the next fresh executable WS book, never from this pool.
        if history_only:
            refresh_risk = getattr(self.source, "refresh_position_risk", None)
            if callable(refresh_risk):
                try:
                    margin_data = refresh_risk(symbol)
                except (DataUnavailable, ValueError, KeyError, TypeError):
                    pass  # Retain the old timestamp; admission fails closed.
        if positions:
            try:
                rates = self.source.funding(symbol)
                earliest = min(positions)
                for rate in rates:
                    if earliest <= rate['at'] <= market_now():
                        marks[rate['at']] = self.source.funding_mark(symbol, rate['at'])
            except (DataUnavailable, ValueError, KeyError, TypeError):
                pass
        if not manage:
            return MarketTick(symbol, bars, None, rates, marks)
        if not history_only and (not urgent or not priority_capable):
            try:
                book = self.source.orderbook(symbol)
            except (DataUnavailable, ValueError, KeyError, TypeError) as exc:
                quote_error = str(exc)
        if urgent and book is not None and not book_is_fresh(book['at']):
            # A full indicator/funding read can outlive the market snapshot.
            # Never publish its old book as an executable quote.
            try:
                getter = getattr(self.source, 'position_orderbook', None) or self.source.orderbook
                book = getter(symbol)
            except (DataUnavailable, ValueError, KeyError, TypeError) as exc:
                book = None
                quote_error = str(exc)
        return MarketTick(symbol, bars, book, rates, marks, history_error, quote_error,
                          margin_data)

    def _quote(self, symbol):
        try:
            book = self.source.position_orderbook(symbol)
            if not book_is_fresh(book['at']):
                # Preserve the rejected observation so the owning run_loop
                # can perform its existing last-moment refresh. It never
                # authorizes history admission or execution: process_tick
                # independently rejects it until a new fresh book is read.
                return book, 'Стакан позиции устарел до передачи владельцу счёта'
            return book, ''
        except (DataUnavailable, ValueError, KeyError, TypeError) as exc:
            return None, str(exc)

    def collect(self, state, timeout=0.2):
        symbols = list(
            dict.fromkeys(
                self.cfg["symbols"]
                + [p["symbol"] for p in state["positions"].values()]
                + [p["symbol"] for p in state.get("pending", {}).values()]
                + [p["symbol"] for p in state.get("funding_archive", {}).values()]
            )
        )
        priority = list(
            dict.fromkeys(
                p["symbol"]
                for p in list(state["positions"].values())
                + list(state.get("pending", {}).values())
            )
        )
        priority_set = set(priority)
        scan = [s for s in symbols if s not in priority_set]
        # A portfolio with unmarked/unfresh positions is in risk-management
        # mode: do not spend HTTP/worker capacity discovering new entries.
        marks = state.get('marks', {})
        risk_mode = any(not quote_is_recent(marks.get(symbol), maximum=3)
                        for symbol in priority_set)
        # A reserved trickle of scanning must remain available: it can
        # produce history/snapshots but cannot admit trades while marks are
        # missing (process_tick's portfolio-wide _entry_blocked guard).
        # Complete scanner starvation also breaks recovery for newly added
        # symbols and users' configured portfolios.
        self.last_pressure = {
            'configured_symbols': len(self.cfg['symbols']),
            'priority_symbols': len(priority),
            'unmarked_priority': sum(not quote_is_recent(marks.get(s), maximum=3)
                                     for s in priority_set),
            'scan_paused': False,
            'entry_admission_paused': risk_mode,
            'scan_throttled': risk_mode,
        }
        # Drain finished jobs before admitting new work. Without this, an
        # already-finished future occupies a slot for a whole collect cycle.
        completed = []
        primary_drained = set()
        def drain():
            for symbol, job in list(self.jobs.items()):
                if not job.done():
                    continue
                del self.jobs[symbol]
                primary_drained.add(symbol)
                # A zero-period round may still have other symbols in flight.
                # Remember completions from BOTH drains (before admission and
                # after wait), or a fast symbol can be scheduled again while
                # its slower sibling has not yet delivered its first tick.
                if self.period == 0:
                    self._zero_period_round.add(symbol)
                self.due[symbol] = time.monotonic() + self.period
                try:
                    tick = job.result()
                    if self._separate_quotes and (symbol in priority_set or symbol in self._history_only):
                        # This plane publishes indicator inputs only. A scanner
                        # admitted just before OPEN must not emit a second book
                        # for the same position alongside the quote lane.
                        self._history_results[symbol] = MarketTick(
                            symbol, tick.bars, None, tick.rates, tick.funding_marks,
                            tick.history_error, '', tick.margin_data)
                    else:
                        completed.append((symbol, tick, None))
                except Exception as exc:
                    completed.append((symbol, None, exc))
                self._history_only.discard(symbol)
            self.pool.collect()

        drain()
        quote_delivered = set()
        def drain_quotes():
            for symbol, job in list(self.quote_jobs.items()):
                if not job.done():
                    continue
                del self.quote_jobs[symbol]
                self.quote_due[symbol] = time.monotonic() + self.quote_period
                if symbol not in priority_set:
                    continue
                quote_delivered.add(symbol)
                try:
                    book, error = job.result()
                    if book is not None and book_is_fresh(book['at']):
                        self._quote_ready[symbol] = book['at']
                    else:
                        self._quote_ready.pop(symbol, None)
                    history = self._history_results.get(symbol)
                    tick = MarketTick(symbol, history.bars if history else [], book,
                                      history.rates if history else [],
                                      history.funding_marks if history else {},
                                      history.history_error if history else '', error,
                                      history.margin_data if history else None)
                    if history is not None:
                        # A refreshed margin snapshot is delivered once;
                        # replaying it on each quote would regress newer
                        # mark-price evidence back to an older risk snapshot.
                        history.margin_data = None
                    completed.append((symbol, tick, None))
                except Exception as exc:
                    completed.append((symbol, None, exc))
            if self.quote_pool is not None:
                self.quote_pool.collect()
        drain_quotes()
        if self._separate_quotes and priority:
            if self.quote_pool is None:
                self.quote_pool = StrategyDispatcher(workers=self.quote_capacity,
                    maximum_pending=self.quote_capacity, thread_name_prefix='position-quotes')
            for _ in range(len(priority)):
                if len(self.quote_jobs) >= self.quote_capacity:
                    break
                symbol = priority[self.quote_cursor % len(priority)]
                self.quote_cursor = (self.quote_cursor + 1) % len(priority)
                if (symbol in self.quote_jobs or symbol in quote_delivered
                        or self.quote_due.get(symbol, 0) > time.monotonic()):
                    continue
                self.quote_jobs[symbol] = self.quote_pool.submit(symbol, self._quote, symbol)
        # A zero-period batch must hand its final results to the worker before
        # beginning another round. Otherwise very fast symbols reoccupy slots
        # before the owner has processed the completed ticks.
        if self.period == 0 and completed and not self.jobs and not self.quote_jobs:
            self._zero_period_round.clear()
            completed.sort(key=lambda row: (row[0] not in priority_set,))
            return completed
        if self.period == 0:
            self._zero_period_round.update(symbol for symbol, _, _ in completed)
            if not self.jobs:
                self._zero_period_round.clear()
        # Even with period=0, a completed market tick is delivered to the
        # owner before another request for the same symbol is admitted.
        delivered_this_cycle = primary_drained
        now = time.monotonic()

        def submit(symbol):
            if (symbol in delivered_this_cycle or symbol in self.jobs
                    or (self.period == 0 and symbol in self._zero_period_round)
                    or self.due.get(symbol, 0) > now):
                return False
            history_only = self._separate_quotes and symbol in priority_set
            if history_only and (risk_mode or not book_is_fresh(self._quote_ready.get(symbol, 0))):
                return False
            opened = [
                p["opened"]
                for p in list(state["positions"].values())
                + list(state.get("funding_archive", {}).values())
                if p["symbol"] == symbol
            ]
            self.jobs[symbol] = self.pool.submit(
                symbol, self._fetch,
                symbol,
                self.history_size(self.cfg, symbol, state),
                opened,
                symbol in self.cfg["symbols"] or symbol in priority_set,
                symbol in priority_set,
                risk_mode and symbol in priority_set and not history_only,
                history_only,
            )
            if history_only:
                self._history_only.add(symbol)
            return True

        # A single slot cannot be split between portfolio management and new
        # market discovery. Alternate when both classes are ready, otherwise
        # give that slot to whichever class actually has a due request.
        if self.capacity == 1 and not self.jobs and priority and scan:
            eligible_priority = [s for s in priority if self.due.get(s, 0) <= now]
            eligible_scan = [s for s in scan if self.due.get(s, 0) <= now]
            if eligible_priority and eligible_scan:
                chosen = eligible_scan if self.scan_turn else eligible_priority
                if chosen is eligible_scan:
                    symbol = chosen[self.cursor % len(chosen)]
                    self.cursor += 1
                else:
                    symbol = chosen[self.priority_cursor % len(chosen)]
                    self.priority_cursor += 1
                if submit(symbol):
                    self.scan_turn = not self.scan_turn
                # Normal queues below cannot submit a second job.

        # Revisit positions and accepted grids before continuing a large scan.
        # Reserve some jobs for discovery so many open deals cannot starve it.
        active_jobs = sum(s in priority_set for s in self.jobs)
        priority_limit = (max(1, self.capacity-1) if risk_mode else self.priority_limit)
        for _ in range(len(priority)):
            if len(self.jobs) >= self.capacity or (
                scan and active_jobs >= priority_limit
            ):
                break
            symbol = priority[self.priority_cursor % len(priority)]
            self.priority_cursor = (self.priority_cursor + 1) % len(priority)
            active_jobs += int(submit(symbol))
        admitted_scans = 0
        for _ in range(len(scan)):
            if len(self.jobs) >= self.capacity or (risk_mode and admitted_scans >= 1):
                break
            symbol = scan[self.cursor % len(scan)]
            self.cursor = (self.cursor + 1) % len(scan)
            admitted_scans += int(submit(symbol))
        pending_jobs = [*self.jobs.values(), *self.quote_jobs.values()]
        if pending_jobs:
            wait(pending_jobs, timeout=timeout, return_when=FIRST_COMPLETED)
        else:
            time.sleep(min(timeout, self.period))
        drain()
        drain_quotes()
        self.last_pressure.update(quote_jobs=len(self.quote_jobs), quote_capacity=self.quote_capacity,
                                  history_jobs=len(self.jobs), history_capacity=self.capacity)
        completed.sort(key=lambda row: (row[0] not in priority_set,))
        return completed

    def close(self):
        if hasattr(self.source, "stop_event"):
            self.source.stop_event.set()
        for job in self.jobs.values():
            job.cancel()
        for job in self.quote_jobs.values():
            job.cancel()
        self.pool.close(wait=True)
        if self.quote_pool is not None:
            self.quote_pool.close(wait=True)
