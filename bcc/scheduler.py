"""Bounded parallel public reads; accounting stays on the single worker thread."""

import math
import os
import time
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace

from .strategy_dispatch import StrategyDispatcher
from .market import DataUnavailable, SourceAccessDenied
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
    refresh_attempted: bool = False
    risk_only: bool = False
    risk_error: str = ''
    # Runtime must recheck the original exchange candle timestamp after an
    # asynchronous handoff, including each reuse alongside a fresh quote.
    live_history: bool = False
    history_denied: bool = False


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
        # Per-exposure first successful executable book: no arbitrary cap.
        # Prevent an endlessly recycled quote backlog from starving a late
        # position's initial independent quote/history handoff.
        self._quoted_once = set()
        self.quote_period = min(period, .5)
        self._history_only = set()
        self._history_results = {}
        self._quote_ready = {}
        self._retry_ticks = {}
        self._retry_jobs = {}
        # Failed discovery needs a bounded retry lane: otherwise its next
        # attempt waits for the cursor to traverse an entire large universe.
        self._scan_retries = OrderedDict()
        self._scan_retry_active = set()
        self._scan_retry_turn = True
        self.risk_pool = None
        self.risk_jobs = {}
        self.risk_due = {}
        # Independent tier renewal must cover exposed symbols before the
        # source's 300s risk timestamp expires, even with eight history jobs
        # blocked behind cold scanner candles. This lane cannot mutate trades.
        self.risk_capacity = max(1, workers // 2)
        # This capability is resolved before each collect admission; a standalone
        # _fetch adapter retains its historical metadata contract.
        self._risk_refresh_independent = False
        self._closed_cohort = None
        self._closed_sizes = {}
        self._closed_handoffs = 0

    @staticmethod
    def _oldest_tier_first(priority, state):
        """Put missing/oldest actual exchange tier timestamps first."""
        def tier_age_key(symbol):
            at = state.get('margin_data', {}).get(symbol, {}).get('risk_at')
            return at if type(at) in (int, float) and math.isfinite(at) else -math.inf
        return sorted(priority, key=tier_age_key)

    def _risk(self, symbol):
        try:
            return self.source.refresh_position_risk(symbol), ''
        except (DataUnavailable,ValueError,KeyError,TypeError) as exc:
            return None, str(exc)

    def retry_tick(self, tick):
        """Retry an aged handoff on its transport pool, never on the owner."""
        self._retry_ticks[tick.symbol] = tick
        self._quote_ready.pop(tick.symbol, None)

    def retry_data_blocked(self, symbol):
        """Reconsider a data-blocked scanner symbol using newly fetched inputs.

        This does not retain the old signal, book, or trading authority. The
        ordinary bounded retry lane re-fetches all inputs and re-evaluates the
        strategy; removed or newly prioritised symbols are pruned in collect.
        """
        if symbol in self.cfg['symbols']:
            self._scan_retries.setdefault(symbol, time.monotonic() + max(.05, self.period))

    def _retry_book(self, symbol, urgent):
        getter = getattr(self.source, 'position_orderbook', None) if urgent else None
        getter = getter if callable(getter) else self.source.orderbook
        try:
            return getter(symbol), ''
        except (DataUnavailable, ValueError, KeyError, TypeError) as exc:
            return None, str(exc)

    def _fetch(self, symbol, required, positions, manage=True, urgent=False, mark_only=False, history_only=False, closed_only=False):
        history_error = ''
        history_denied = False
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
                # A held position's indicator history has priority over cold
                # scanner discovery; it is *not* executable risk evidence.
                # Third-party adapters without this explicit capability keep
                # their original candles(symbol, required) contract.
                position_history = getattr(self.source, 'position_candles', None)
                closed_reader = getattr(self.source, "closed_candles", None)
                if closed_only and callable(closed_reader):
                    bars = closed_reader(symbol, required, priority=urgent)
                elif urgent and callable(position_history):
                    bars = position_history(symbol, required)
                else:
                    bars = self.source.candles(symbol, required)
            except (DataUnavailable, ValueError, KeyError, TypeError) as exc:
                history_error = str(exc)
                history_denied = isinstance(exc, SourceAccessDenied)
        rates = []
        marks = {}
        margin_data = None
        # Refresh slow risk tiers on the independent history/funding lane.
        # This result is committed by the single financial worker together
        # with the next fresh executable WS book, never from this pool.
        if history_only and not getattr(self, "_risk_refresh_independent", False):
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
                          margin_data, live_history=True, history_denied=history_denied)

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

    def _closed_symbols(self, state, symbols):
        """One eligibility calculation for normal and cohort history paths."""
        def has_intrabar(value):
            if isinstance(value,dict):
                return (value.get("trigger")=="intrabar"
                        or any(has_intrabar(v) for v in value.values()))
            return isinstance(value,(list,tuple)) and any(has_intrabar(v) for v in value)
        if (os.environ.get("BCC_BYBIT_MAINTAINED_MARK_CANDIDATE") != "1"
                or self.cfg.get("strategy") != "VELES"
                or not callable(getattr(self.source,"closed_candles",None))
                or has_intrabar(self.cfg)):
            return set()
        intrabar = {p.get("symbol") for p in
                    (*state["positions"].values(),*state.get("pending",{}).values())
                    if has_intrabar(p)}
        return set(symbols)-intrabar

    def _poll_closed_cohort(self, state, closed_symbols, priority_set):
        reader = getattr(self.source,"cached_closed_cohort",None)
        if (os.environ.get("BCC_BYBIT_PORTFOLIO_HISTORY_CANDIDATE")!="1"
                or self.period==0 or not callable(reader)):
            return
        from .market_clock import ClockUnavailable
        from .data_cache import CacheBusy,ProviderDenied
        from .closed_cohort import ClosedCohort
        if self._closed_cohort is None:
            if not closed_symbols:
                return
            self._closed_cohort = ClosedCohort(reader,recoverable=(
                DataUnavailable,ClockUnavailable,CacheBusy,ProviderDenied,OSError))
        try:
            boundary=int(market_now()//60)*60000
        except ClockUnavailable:
            boundary=None
        # Requirements already computed by the normal admission path.
        # Never recalculate all effective VELES configurations at quote cadence.
        for symbol in tuple(self._closed_sizes):
            if symbol not in closed_symbols:
                self._closed_sizes.pop(symbol,None)
        req = ({s:n for s,n in self._closed_sizes.items() if s in closed_symbols}
               if boundary is not None else {})
        fresh = self._closed_cohort.poll(req,boundary,time.monotonic())
        # Only the first ready handoff of a new closed minute accelerates
        # evaluation. Existing jobs stay single-flight and retain ownership.
        for symbol in fresh:
            if symbol in priority_set:
                self.quote_due[symbol]=0.
            else:
                self.due[symbol]=0.
                if symbol in self._scan_retries:
                    self._scan_retries[symbol]=0.

    def _apply_closed_cohort(self, completed, closed_symbols, state):
        cohort=self._closed_cohort
        if (cohort is None or
                os.environ.get("BCC_BYBIT_PORTFOLIO_HISTORY_CANDIDATE")!="1"):
            return 0
        try:
            boundary=int(market_now()//60)*60000
        except ValueError:
            return 0
        if cohort.boundary != boundary:
            return 0
        applied=0
        for symbol,tick,error in completed:
            if (error is not None or tick is None or tick.risk_only
                    or tick.history_denied or symbol not in closed_symbols):
                continue
            bars=cohort.ready.get(symbol)
            if bars is None:
                continue
            if (not tick.bars or tick.history_error
                    or tick.bars[-1]["t"] < bars[-1]["t"]):
                # Recheck this actual handoff against active persisted
                # settings (which can differ from the last admitted job).
                required=max(100,int(self.history_size(self.cfg,symbol,state)))
                self._closed_sizes[symbol]=required
                if len(bars)<max(required,len(tick.bars)):
                    continue
                # Only indicator history is replaced; retain the separately
                # admitted book, funding, risk metadata, and quote errors.
                tick.bars=bars
                tick.history_error=""
                tick.live_history=True
                history=self._history_results.get(symbol)
                if history is not None and not history.history_denied:
                    history.bars=bars
                    history.history_error=""
                    history.live_history=True
                applied+=1
        self._closed_handoffs+=applied
        return applied

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
        set_positions = getattr(self.source, 'set_position_symbols', None)
        if self._separate_quotes and callable(set_positions):
            set_positions(priority)
        priority_set = set(priority)
        # Bound scheduler CPU to the size of the input inventory. These
        # indexes are derived anew from the immutable owner state snapshot;
        # no stale opened-at evidence can survive a change of positions.
        priority_index = {symbol: i for i, symbol in enumerate(priority)}
        opened_by_symbol = {}
        for exposure in (*state["positions"].values(),
                         *state.get("funding_archive", {}).values()):
            opened_by_symbol.setdefault(exposure["symbol"], []).append(exposure["opened"])
        self._quoted_once.intersection_update(priority_set)
        scan = [s for s in symbols if s not in priority_set]
        scan_set = set(scan)
        configured_set = set(self.cfg['symbols'])
        closed_symbols = self._closed_symbols(state, configured_set | priority_set)
        for symbol in list(self._scan_retries):
            if symbol not in scan_set or symbol not in configured_set:
                del self._scan_retries[symbol]
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
        # Tier renewal is not a strategy-history task. It must run even if
        # candles or executable quote jobs are blocked, or risk mode is on.
        for symbol, job in list(self.risk_jobs.items()):
            if not job.done():
                continue
            del self.risk_jobs[symbol]
            self.risk_due[symbol] = time.monotonic()+15
            try:
                metadata,error = job.result()
                if symbol in priority_set and (metadata is not None or error):
                    completed.append((symbol,MarketTick(symbol,[],None,[],{},
                        margin_data=metadata,risk_only=True,risk_error=error),None))
            except Exception as exc:
                completed.append((symbol,None,exc))
        if self.risk_pool is not None:
            self.risk_pool.collect()
        enabled = getattr(self.source,'risk_maintenance_enabled',lambda:True)
        # Never send the same held-position metadata refresh through the
        # history pool when its dedicated position-risk pool is active.
        self._risk_refresh_independent = (
            callable(getattr(self.source,'refresh_position_risk',None)) and enabled()
        )
        if self._risk_refresh_independent:
            try:
                utc = market_now()
            except ValueError:
                utc = None
            # Configuration/insertion order is not a risk priority. Renew
            # missing or oldest exchange evidence before recent exposures.
            for symbol in self._oldest_tier_first(priority, state):
                if len(self.risk_jobs)>=self.risk_capacity:
                    break
                at = state.get('margin_data',{}).get(symbol,{}).get('risk_at')
                if (symbol in self.risk_jobs or time.monotonic()<self.risk_due.get(symbol,0)
                        or (utc is not None and type(at) in (int,float) and -0.5<=utc-at<90)):
                    continue
                if self.risk_pool is None:
                    self.risk_pool = StrategyDispatcher(workers=self.risk_capacity,
                        maximum_pending=self.risk_capacity,thread_name_prefix='position-risk')
                self.risk_jobs[symbol] = self.risk_pool.submit(symbol,self._risk,symbol)
        self._poll_closed_cohort(state, closed_symbols, priority_set)
        primary_drained = set()
        def drain():
            for symbol, job in list(self.jobs.items()):
                if not job.done():
                    continue
                del self.jobs[symbol]
                self._scan_retry_active.discard(symbol)
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
                    if symbol in scan_set and symbol in configured_set:
                        if tick.history_error or tick.quote_error or not tick.bars:
                            self._scan_retries[symbol] = time.monotonic() + max(.05, self.period)
                            self._scan_retries.move_to_end(symbol)
                        else:
                            self._scan_retries.pop(symbol, None)
                    if self._separate_quotes and (symbol in priority_set or symbol in self._history_only):
                        # This plane publishes indicator inputs only. A scanner
                        # admitted just before OPEN must not emit a second book
                        # for the same position alongside the quote lane.
                        self._history_results[symbol] = MarketTick(
                            symbol, tick.bars, None, tick.rates, tick.funding_marks,
                            tick.history_error, '', tick.margin_data,
                            live_history=tick.live_history,
                            history_denied=getattr(tick,"history_denied",False))
                    else:
                        completed.append((symbol, tick, None))
                except Exception as exc:
                    if symbol in scan_set and symbol in configured_set:
                        self._scan_retries[symbol] = time.monotonic() + max(.05, self.period)
                        self._scan_retries.move_to_end(symbol)
                    completed.append((symbol, None, exc))
                self._history_only.discard(symbol)
            self.pool.collect()

        drain()
        retried = set()
        for symbol, (job, urgent) in list(self._retry_jobs.items()):
            if not job.done():
                continue
            del self._retry_jobs[symbol]
            retried.add(symbol)
            primary_drained.add(symbol)
            if urgent:
                self.quote_due[symbol] = time.monotonic() + self.quote_period
            else:
                self.due[symbol] = time.monotonic() + self.period
            original = self._retry_ticks.pop(symbol)
            try:
                book, error = job.result()
                completed.append((symbol, replace(original, book=book,
                    quote_error=error, refresh_attempted=True), None))
            except Exception as exc:
                completed.append((symbol, None, exc))
        # Finished retry futures share the same bounded dispatchers as their
        # normal lane. Release their admission before scheduling replacements.
        self.pool.collect()
        if self.quote_pool is not None:
            self.quote_pool.collect()
        quote_delivered = set(retried)
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
                        self._quoted_once.add(symbol)
                    else:
                        self._quote_ready.pop(symbol, None)
                    history = self._history_results.get(symbol)
                    tick = MarketTick(symbol, history.bars if history else [], book,
                                      history.rates if history else [],
                                      history.funding_marks if history else {},
                                      history.history_error if history else '', error,
                                      history.margin_data if history else None,
                                      live_history=bool(history and history.live_history),
                                      history_denied=bool(history and history.history_denied))
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
        for symbol in sorted(self._retry_ticks, key=lambda s: s not in priority_set):
            if symbol in self._retry_jobs or symbol in self.jobs or symbol in self.quote_jobs:
                continue
            urgent = symbol in priority_set and self._separate_quotes
            occupied = sum(lane == urgent for _, lane in self._retry_jobs.values())
            if urgent:
                if len(self.quote_jobs) + occupied >= self.quote_capacity:
                    continue
                if self.quote_pool is None:
                    self.quote_pool = StrategyDispatcher(workers=self.quote_capacity,
                        maximum_pending=self.quote_capacity, thread_name_prefix='position-quotes')
                pool = self.quote_pool
            else:
                if len(self.jobs) + occupied >= self.capacity:
                    continue
                pool = self.pool
            self._retry_jobs[symbol] = (pool.submit(symbol, self._retry_book, symbol, urgent), urgent)
        if self._separate_quotes and priority:
            if self.quote_pool is None:
                self.quote_pool = StrategyDispatcher(workers=self.quote_capacity,
                    maximum_pending=self.quote_capacity, thread_name_prefix='position-quotes')
            # First quote of every exposed symbol must be admitted before
            # spending every fast-lane slot repeatedly refreshing symbols
            # already serviced. No pre-existing position gets a free pass to
            # EXECUTION: process_tick still revalidates book, mark and tiers.
            missing_initial = [s for s in priority if s not in self._quoted_once]
            next_due_regular = [priority[(self.quote_cursor+i) % len(priority)]
                                for i in range(len(priority))]
            for symbol in (*missing_initial, *next_due_regular):
                if len(self.quote_jobs) + sum(lane for _, lane in self._retry_jobs.values()) >= self.quote_capacity:
                    break
                if (symbol in self.quote_jobs or symbol in quote_delivered or symbol in self._retry_ticks
                        or self.quote_due.get(symbol, 0) > time.monotonic()):
                    continue
                self.quote_jobs[symbol] = self.quote_pool.submit(symbol, self._quote, symbol)
                self.quote_cursor = (priority_index[symbol]+1) % len(priority)
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
            if (symbol in delivered_this_cycle or symbol in self.jobs or symbol in self._retry_ticks
                    or (self.period == 0 and symbol in self._zero_period_round)
                    or self.due.get(symbol, 0) > now):
                return False
            history_only = self._separate_quotes and symbol in priority_set
            # Portfolio admission can be fenced by a different unmarked
            # position. That must not starve history for this position once
            # its own independent executable book is fresh: signal-based
            # protection and recovery still need current candles. The owner
            # retains its portfolio-wide entry/DCA risk checks.
            # A native position_candles adapter has an independently
            # prioritized history lane; it may refresh non-executable candles
            # while the quote is stale. Legacy adapters only have candles()
            # and require a fresh independently verified quote before starting
            # history, preserving their original safety/ordering contract.
            if (history_only and not callable(getattr(self.source, 'position_candles', None))
                    and not book_is_fresh(self._quote_ready.get(symbol, 0))):
                return False
            opened = opened_by_symbol.get(symbol, [])
            closed_only = symbol in closed_symbols
            required = self.history_size(self.cfg, symbol, state)
            if closed_only:
                self._closed_sizes[symbol]=max(100,int(required))
            self.jobs[symbol] = self.pool.submit(
                symbol, self._fetch,
                symbol,
                required,
                opened,
                symbol in self.cfg["symbols"] or symbol in priority_set,
                symbol in priority_set,
                risk_mode and symbol in priority_set and not history_only,
                history_only,
                *([True] if closed_only else []),
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
            if len(self.jobs) + sum(not lane for _, lane in self._retry_jobs.values()) >= self.capacity or (
                scan and active_jobs >= priority_limit
            ):
                break
            symbol = priority[self.priority_cursor % len(priority)]
            self.priority_cursor = (self.priority_cursor + 1) % len(priority)
            active_jobs += int(submit(symbol))
        admitted_scans = 0
        # Alternate a bounded failed-symbol retry with untouched discovery.
        # Limit concurrent retries by actual worker capacity, retain the
        # position lane, and in risk mode obey its one-scan admission rule.
        # If every eligible scan is already a retry, leaving most workers
        # idle needlessly prolongs recovery after a fleet-wide outage. Share
        # capacity only while untouched discovery can actually use it.
        discovery_ready = any(
            symbol not in self._scan_retries and symbol not in self.jobs
            and symbol not in self._retry_ticks
            and (self.period != 0 or symbol not in self._zero_period_round)
            and self.due.get(symbol, 0) <= now
            for symbol in scan
        )
        retry_limit = (max(1, self.capacity // 4) if discovery_ready else
                       max(1, self.capacity - int(bool(priority))))
        if self._scan_retry_turn and len(self._scan_retry_active) < retry_limit:
            for symbol, retry_at in list(self._scan_retries.items()):
                if len(self.jobs) + sum(not lane for _, lane in self._retry_jobs.values()) >= self.capacity:
                    break
                if retry_at > now:
                    continue
                if submit(symbol):
                    self._scan_retries.pop(symbol, None)
                    self._scan_retry_active.add(symbol)
                    self._scan_retry_turn = False
                    admitted_scans += 1
                    break
        discovered = False
        for _ in range(len(scan)):
            if len(self.jobs) + sum(not lane for _, lane in self._retry_jobs.values()) >= self.capacity or (risk_mode and admitted_scans >= 1):
                break
            symbol = scan[self.cursor % len(scan)]
            self.cursor = (self.cursor + 1) % len(scan)
            if symbol in self._scan_retries:
                continue
            if submit(symbol):
                admitted_scans += 1
                discovered = True
        if discovered or (not admitted_scans and self._scan_retries):
            self._scan_retry_turn = True
        pending_jobs = [*self.jobs.values(), *self.quote_jobs.values(),
                        *(job for job, _ in self._retry_jobs.values()),*self.risk_jobs.values()]
        if pending_jobs:
            wait(pending_jobs, timeout=timeout, return_when=FIRST_COMPLETED)
        else:
            time.sleep(min(timeout, self.period))
        drain()
        drain_quotes()
        # Newly arrived closed evidence can be delivered on the very next
        # admitted executable quote, even while the original history/funding
        # future is still occupied. No new order or duplicate trade tick.
        self._poll_closed_cohort(state, closed_symbols, priority_set)
        self._apply_closed_cohort(completed,closed_symbols,state)
        if self._closed_cohort is not None:
            cohort=self._closed_cohort
            self.last_pressure.update(closed_cohort_ready=len(cohort.ready),
                closed_cohort_boundary=cohort.boundary,
                closed_cohort_eligible=len(closed_symbols),
                closed_cohort_reads=cohort.reads,
                closed_cohort_delivered=cohort.delivered,
                closed_cohort_errors=cohort.errors,
                closed_cohort_handoffs=self._closed_handoffs)
        self.last_pressure.update(quote_jobs=len(self.quote_jobs), quote_capacity=self.quote_capacity,
                                  history_jobs=len(self.jobs), history_capacity=self.capacity,
                                  deferred_refreshes=len(self._retry_ticks),risk_jobs=len(self.risk_jobs))
        completed.sort(key=lambda row: (row[0] not in priority_set,))
        return completed

    def close(self):
        if hasattr(self.source, "stop_event"):
            self.source.stop_event.set()
        for job in self.jobs.values():
            job.cancel()
        for job in self.quote_jobs.values():
            job.cancel()
        for job, _ in self._retry_jobs.values():
            job.cancel()
        for job in self.risk_jobs.values():
            job.cancel()
        if self._closed_cohort is not None:
            self._closed_cohort.close()
        self.pool.close(wait=True)
        if self.quote_pool is not None:
            self.quote_pool.close(wait=True)
        if self.risk_pool is not None:
            self.risk_pool.close(wait=True)
