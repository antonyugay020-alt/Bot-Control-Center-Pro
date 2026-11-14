"""Read-only public market transport. Only GET endpoints are available."""

import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from .data_cache import CacheBusy, CacheContention, ProviderDenied, PublicCache
from .market_clock import book_is_fresh, clock, market_now, observed_quote, utc_bounds


class DataUnavailable(RuntimeError):
    pass


class SourceAccessDenied(DataUnavailable):
    """An upstream provider denied access; not a missing candle or stale quote.

    HTTP 451 is a legal/geographic denial, and 403 is an access denial. Neither
    grants permission to substitute another exchange or trade on old data.
    The structured status is intentionally free of request parameters/tokens.
    """

    def __init__(self, host, http_status):
        self.host = host
        self.http_status = int(http_status)
        super().__init__(
            f"{host}: HTTP {self.http_status}: публичный источник отклонил доступ; "
            "торговые решения заблокированы"
        )


def executable_quote(book):
    """Use only positive-depth levels for marks, signals and liquidation.

    A zero-size best-price row is not an executable quote. The execution engine
    already skips zero-size levels; valuation and admission must agree with it.
    """
    if not isinstance(book, dict):
        raise DataUnavailable("Нет достоверного стакана")
    best = []
    for field in ("bids", "asks"):
        levels = book.get(field)
        if not isinstance(levels, list) or not levels:
            raise DataUnavailable("Нет исполнимой котировки для PAPER-решения")
        selected = None
        previous_price = None
        descending = field == "bids"
        for level in levels:
            if (not isinstance(level, (list, tuple)) or len(level) != 2 or
                    any(type(x) not in (int, float) or not math.isfinite(x)
                        for x in level) or level[0] <= 0 or level[1] < 0):
                raise DataUnavailable("Повреждена котировка: цены или объёмы стакана")
            price = level[0]
            # Validate the executable price ladder independently of upstream
            # adapters. Zero-size rows are not executable, and may be ignored
            # in valuation (including synthetic legacy snapshots).
            if level[1] > 0:
                if previous_price is not None and (
                    price >= previous_price if descending else price <= previous_price
                ):
                    raise DataUnavailable("Нарушен порядок исполнимых уровней стакана")
                previous_price = price
                if selected is None:
                    selected = price
        if selected is None:
            raise DataUnavailable("Нет положительной глубины стакана")
        best.append(selected)
    if best[0] > best[1]:
        raise DataUnavailable("Повреждена котировка: пересечённые исполнимые уровни стакана")
    return best[0], best[1]



def canonical_up_down_tokens(tokens):
    """Require exactly one distinct token for each binary outcome.

    This is checked both when decoding Gamma metadata and at PAPER admission:
    external fixtures or future data adapters must not bypass discovery checks.
    No decision is made about which outcome wins here.
    """
    if not isinstance(tokens, dict) or set(tokens) != {"UP", "DOWN"}:
        raise DataUnavailable("Неоднозначное соответствие токенов UP/DOWN")
    up, down = tokens["UP"], tokens["DOWN"]
    if (type(up) is not str or type(down) is not str
        or not up or not down or up == down
        or any(ch.isspace() for token in (up, down) for ch in token)):
        raise DataUnavailable("Некорректные или совпадающие токены UP/DOWN")
    return {"UP": up, "DOWN": down}


def verified_paper_market(market):
    """Reject malformed discovered markets before requesting quotes or changing PAPER state."""
    if not isinstance(market, dict):
        raise DataUnavailable("Не подтверждены данные рынка Polymarket")
    for field in ("condition", "slug"):
        value = market.get(field)
        if type(value) is not str or not value.strip() or value != value.strip():
            raise DataUnavailable("Не подтверждён идентификатор рынка Polymarket")
    start, end = market.get("start"), market.get("end")
    if (type(start) not in (float, int) or type(end) not in (float, int)
        or not math.isfinite(start) or not math.isfinite(end) or start >= end):
        raise DataUnavailable("Не подтверждены временные границы рынка Polymarket")
    return canonical_up_down_tokens(market.get("tokens"))

def check_book_time(stamp, source, details=None):
    now = time.time()
    low, high = utc_bounds()
    age = low - stamp
    if not book_is_fresh(stamp):
        hint = (
            "Снимок опережает сверенное UTC; PAPER-исполнение отложено"
            if age < -0.5
            else "Снимок старше допуска; ожидается свежий снимок источника"
        )
        raise DataUnavailable(
            f"Устаревший стакан {source}: возраст=[{age:+.3f}, {high - stamp:+.3f}] с; допуск=[-0.500, +3.000] с; локальное UTC={now:.3f}; время стакана UTC={stamp:.3f}; сверенное UTC=[{low:.3f}, {high:.3f}]. {hint}"
            + (
                "; получение=" + json.dumps(details, ensure_ascii=False)
                if details
                else ""
            )
        )


class PublicData:
    HOSTS = {
        "api.bybit.com",
        "gamma-api.polymarket.com",
        "clob.polymarket.com",
        "data-api.polymarket.com",
        "api.binance.com",
    }

    # This is the sole URL admission contract for both direct worker reads
    # and durable Hub subscriptions. A host allowlist alone is insufficient:
    # it permits private GET routes, alternate ports and query credentials.
    PUBLIC_PATHS = {
        "api.bybit.com": frozenset({
            "/v5/market/tickers", "/v5/market/orderbook",
            "/v5/market/instruments-info", "/v5/market/risk-limit",
            "/v5/market/kline", "/v5/market/mark-price-kline",
            "/v5/market/funding/history",
        }),
        "gamma-api.polymarket.com": frozenset({"/markets"}),
        "clob.polymarket.com": frozenset({"/book"}),
        "data-api.polymarket.com": frozenset({"/v2/tokens"}),
        "api.binance.com": frozenset({
            "/api/v3/time", "/api/v3/klines", "/api/v3/ticker/price",
        }),
    }


    # Exact route-bound public query schemas. Host and path checks alone do
    # not prevent a caller from persisting an API key under an arbitrary
    # innocuous-looking query name in the shared Hub subscription database.
    # Keep this mapping aligned with the concrete read-only adapters below.
    PUBLIC_QUERY_FIELDS = {
        "api.bybit.com": {
            "/v5/market/tickers": frozenset({"category", "symbol"}),
            "/v5/market/orderbook": frozenset({"category", "symbol", "limit"}),
            "/v5/market/instruments-info": frozenset({"category", "symbol", "limit", "cursor"}),
            "/v5/market/risk-limit": frozenset({"category", "symbol", "cursor"}),
            "/v5/market/kline": frozenset({"category", "symbol", "interval", "start", "end", "limit"}),
            "/v5/market/mark-price-kline": frozenset({"category", "symbol", "interval", "start", "end", "limit"}),
            "/v5/market/funding/history": frozenset({"category", "symbol", "startTime", "endTime", "limit"}),
        },
        "gamma-api.polymarket.com": {
            "/markets": frozenset({
                "slug", "active", "closed", "limit", "offset",
                "end_date_min", "end_date_max",
            }),
        },
        "clob.polymarket.com": {"/book": frozenset({"token_id"})},
        "data-api.polymarket.com": {"/v2/tokens": frozenset({"token_id"})},
        "api.binance.com": {
            "/api/v3/time": frozenset(),
            "/api/v3/klines": frozenset({"symbol", "interval", "startTime", "endTime", "limit"}),
            "/api/v3/ticker/price": frozenset({"symbol"}),
        },
    }
    _INTEGER_QUERY_NAMES = frozenset({
        "limit", "offset", "start", "end", "startTime", "endTime",
    })
    _TEXT_QUERY_PATTERNS = {
        "category": r"linear",
        "symbol": r"[A-Z0-9_]{1,40}",
        "interval": r"[A-Za-z0-9]{1,8}",
        "cursor": r"[A-Za-z0-9_+./=:%-]{1,1024}",
        "slug": r"[a-z0-9-]{1,256}",
        "token_id": r"[A-Za-z0-9_-]{1,256}",
        "active": r"(?:true|false)",
        "closed": r"(?:true|false)",
    }

    @classmethod
    def validated_public_host(cls, url):
        """Reject private/ambiguous routes before any cache or Hub side effect.

        Public query arguments must be supplied separately as `params`; the
        complete URL is persisted by the Hub, so embedded query credentials
        must never be admitted. The network authority is an exact HTTPS host
        without userinfo or an alternate port, even when DNS matches.
        """
        if (type(url) is not str or url != url.strip()
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in url)):
            raise ValueError("Непубличный источник или закрытые данные в URL")
        try:
            parsed = urlparse(url)
            host = parsed.hostname
        except ValueError as exc:
            raise ValueError("Непубличный источник или закрытые данные в URL") from exc
        path = parsed.path
        if (parsed.scheme != "https" or host not in cls.HOSTS
                or parsed.netloc != host or parsed.query or parsed.fragment
                or parsed.params or not path or "%" in path or "\\" in path
                or "//" in path or any(part in (".", "..") for part in path.split("/"))):
            raise ValueError("Непубличный источник или закрытые данные в URL")
        allowed = cls.PUBLIC_PATHS[host]
        if host == "clob.polymarket.com" and path.startswith("/clob-markets/"):
            valid_route = re.fullmatch(r"/clob-markets/[A-Za-z0-9_-]{1,128}", path) is not None
        else:
            valid_route = path in allowed
        if not valid_route:
            raise ValueError("Маршрут не относится к разрешённым публичным GET")
        return host

    # The Hub stores public GET arguments durably. Never allow a caller to
    # smuggle credentials into that registry (or send them in direct mode).
    # Public outcome identifiers are named token_id and are NOT auth tokens.
    _PRIVATE_QUERY_NAMES = frozenset({
        "apikey", "apisecret", "secret", "clientsecret", "privatekey",
        "accesskey", "accesstoken", "refreshtoken", "authorization",
        "auth", "password", "passphrase", "signature", "sign",
        "cookie", "session", "bearer", "jwt", "token",
    })

    @classmethod
    def validated_public_request(cls, url, params):
        """One route-specific admission before HTTP, Hub enqueue and dequeue.

        Only public query keys actually used by the read-only adapters may
        enter the durable subscription registry. Values have bounded,
        type-specific shapes; no caller-provided query names or arbitrary
        strings are logged on rejection.
        """
        host = cls.validated_public_host(url)
        path = urlparse(url).path
        allowed = cls.PUBLIC_QUERY_FIELDS[host].get(path)
        if allowed is None:
            # Only the validated, single-segment public CLOB metadata route
            # is dynamic. It never accepts query parameters.
            if host == "clob.polymarket.com" and path.startswith("/clob-markets/"):
                allowed = frozenset()
            else:
                raise ValueError("Неизвестная схема публичного GET")
        if params is None:
            return host
        if type(params) is not dict or len(params) > len(allowed):
            raise ValueError("Недопустимые параметры публичного GET")
        for name, value in params.items():
            if type(name) is not str or name not in allowed:
                raise ValueError("Непубличный параметр GET")
            if name in cls._INTEGER_QUERY_NAMES:
                if (type(value) is not int or value < (0 if name == "offset" else 1)
                        or value > (1000000 if name == "offset" else
                                    1000 if name == "limit" else 2 ** 63 - 1)):
                    raise ValueError("Недопустимое значение публичного GET")
            elif name in ("end_date_min", "end_date_max"):
                if type(value) is not str or len(value) > 40:
                    raise ValueError("Недопустимое значение публичного GET")
                try:
                    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("Недопустимое значение публичного GET") from exc
                if stamp.tzinfo is None:
                    raise ValueError("Недопустимое значение публичного GET")
            else:
                pattern = cls._TEXT_QUERY_PATTERNS.get(name)
                if (type(value) is not str or pattern is None
                        or re.fullmatch(pattern, value) is None):
                    raise ValueError("Недопустимое значение публичного GET")
        return host

    def __init__(self, *, quote_stream=None, orderbook_stream=None, kline_stream=None, polymarket_stream=None, binance_stream=None):
        # Explicit opt-in adapter; the caller owns its producer thread.
        # A missing/failed WebSocket quote never falls back to REST.
        self._quote_stream = quote_stream
        self._orderbook_stream = orderbook_stream
        self._kline_stream = kline_stream
        self._polymarket_stream = polymarket_stream
        self._binance_stream = binance_stream
        self._thread = threading.local()
        self._sessions = []
        self._lock = threading.RLock()
        self.stop_event = threading.Event()
        self.cooldowns = {}
        # Independent monotonic denial fence. A system clock jump cannot
        # accidentally re-enable a region-blocked venue or cached snapshot.
        self._access_denials = {}
        self.cache = {}
        self.shared = None
        self._cache_warning_at = -math.inf
        # A worker has one market transport shared by its scheduler threads.
        # Avoid creating/initializing the SQLite subscription schema for every
        # candle, book and instrument GET. Renew registration independently of
        # cache freshness, but at most once per key per 20 seconds.
        self._hub_registry = None
        self._hub_renew_at = {}
        self._hub_priority_until = {}
        path = os.environ.get("BCC_PUBLIC_CACHE")
        if path:
            from .data_cache import PublicCache

            self.shared = PublicCache(path)

    @property
    def session(self):
        if not hasattr(self._thread, "session"):
            self._thread.session = requests.Session()
            with self._lock:
                self._sessions.append(self._thread.session)
        return self._thread.session

    def close(self):
        self.stop_event.set()
        for session in self._sessions:
            session.close()
        if self.shared:
            self.shared.close()

    def _finish_shared(self, *args, **kwargs):
        # The hub must not throw away a successful HTTP result because an
        # unrelated subscriber briefly holds SQLite's single writer lock.
        # Retry the SAME already-budgeted owner lease. Never issue a second
        # upstream request, and never bypass the lease/host budget on failure.
        deadline = time.monotonic() + 1.5
        while True:
            try:
                return self.shared.finish(*args, **kwargs)
            except CacheBusy as exc:
                if time.monotonic() >= deadline or self.stop_event.is_set():
                    # Failed publication remains unavailable to PAPER workers;
                    # a lease we could not release will expire normally.
                    with self._lock:
                        if time.monotonic() >= self._cache_warning_at:
                            logging.warning("PUBLIC_CACHE: %s", exc)
                            self._cache_warning_at = time.monotonic() + 30
                    return False
                self.stop_event.wait(.05)

    def _record_shared_denial(self, key, owner, host, cooldown, status):
        """Durably publish observed HTTP refusal independently of lease age.

        SQLite contention is retried without repeating the provider request.
        An uncommitted refusal is an explicit fail-closed error, not success.
        """
        deadline = time.monotonic() + 2.0
        while True:
            try:
                self.shared.record_http_denial(
                    key, owner, host, cooldown=cooldown, denial_status=status
                )
                return
            except CacheBusy as exc:
                if time.monotonic() >= deadline:
                    raise DataUnavailable(
                        "Общий кэш: запрет публичного источника не сохранён " + host
                    ) from exc
                time.sleep(.05)

    def _position_critical_request(self, url, params):
        # An existing position requires BOTH instrument metadata and the
        # executable book. Reserving only the book request is not sufficient:
        # orderbook() fetches metadata first and may be unable to reach the
        # book at all when general discovery saturates the shared budget.
        if not getattr(self._thread, 'position_mark', False):
            return False
        if 'orderbook' in url or '/book' in url:
            return True
        return (isinstance(params, dict) and (
            any(route in url for route in ('/instruments-info', '/risk-limit'))
            and bool(params.get('symbol'))
            or '/tickers' in url and params.get('category') == 'linear'))

    def _assert_provider_allowed(self, host):
        """One admission contract for direct GET, Hub reads and post-HTTP use."""
        with self._lock:
            denial = self._access_denials.get(host)
            if denial is not None:
                expires, http_status = denial
                if time.monotonic() < expires:
                    raise SourceAccessDenied(host, http_status)
                del self._access_denials[host]
        if isinstance(self.shared, PublicCache):
            try:
                status = self.shared.denied_status(host)
            except CacheBusy as exc:
                raise DataUnavailable(
                    "Общий кэш: невозможно проверить запрет источника " + host
                ) from exc
            if status is not None:
                raise SourceAccessDenied(host, status)

    def _ws_ticker_get(self, url, params, host):
        """Publish a single current-generation WS quote using existing leases.

        The Hub producer is the only process given this stream. Client workers
        still use ordinary validated Hub subscriptions and shared-cache reads.
        A WS failure never turns into a REST request, even if a cached REST
        response from an earlier process happens to remain in SQLite.
        """
        from .ws_quotes import StreamUnavailable
        symbol = params["symbol"]
        key = owner = None
        if self.shared is not None:
            deadline = time.monotonic() + 3.0
            while True:
                try:
                    key, owner, cached = self.shared.begin(
                        url, params, host,
                        priority=self._position_critical_request(url, params),
                        websocket=True,
                    )
                    break
                except ProviderDenied as exc:
                    raise SourceAccessDenied(exc.host, exc.http_status) from exc
                except CacheBusy as exc:
                    if (str(exc) != "Данные уже запрашиваются другим ботом"
                            or time.monotonic() >= deadline):
                        raise DataUnavailable("WebSocket общий кэш недоступен") from exc
                    if self.stop_event.wait(.05):
                        raise DataUnavailable("Получение данных остановлено")
            if owner is None:
                if isinstance(cached, dict) and cached.get("_bcc_ws_quote") is True:
                    self._assert_provider_allowed(host)
                    return cached
                # Cache origin is not verified: never accept an older HTTP
                # ticker as WS data. The old TTL will expire naturally.
                raise DataUnavailable("WebSocket кэш: источник снимка не подтверждён")
        try:
            self._quote_stream.subscribe(symbol)
            bid, ask, at = self._quote_stream.quote(symbol)
            if (type(bid) not in (float, int) or type(ask) not in (float, int)
                    or type(at) not in (float, int)
                    or not all(math.isfinite(x) for x in (bid, ask, at))
                    or bid <= 0 or ask <= 0 or bid > ask
                    or not -5 <= market_now() - at <= 2):
                raise DataUnavailable("WebSocket Bybit: недостоверная котировка")
            data = {
                "retCode": 0, "retMsg": "OK", "_bcc_ws_quote": True,
                "time": int(at * 1000),
                "result": {"list": [{
                    "symbol": symbol, "bid1Price": str(bid),
                    "ask1Price": str(ask),
                }]},
            }
        except (StreamUnavailable, ValueError, TypeError, DataUnavailable) as exc:
            if self.shared is not None:
                self._finish_shared(key, owner, host)
            raise DataUnavailable("WebSocket Bybit: свежий снимок недоступен") from exc
        if self.shared is not None:
            published = self._finish_shared(key, owner, host, data, .35)
            if not published:
                raise DataUnavailable("WebSocket Bybit: публикация не подтверждена")
        self._assert_provider_allowed(host)
        return data

    def _ws_orderbook_get(self, url, params, host):
        """Fenced snapshot of public WS book into the existing shared Hub cache.

        REST-shaped result preserves all current PAPER adapter validation.
        An existing cached REST response never qualifies as a WS book.
        """
        from .ws_quotes import StreamUnavailable
        symbol = params["symbol"]
        key = owner = None
        if self.shared is not None:
            deadline = time.monotonic() + 3
            while True:
                try:
                    key, owner, cached = self.shared.begin(
                        url, params, host,
                        priority=self._position_critical_request(url, params),
                        websocket=True)
                    break
                except ProviderDenied as exc:
                    raise SourceAccessDenied(exc.host, exc.http_status) from exc
                except CacheBusy as exc:
                    if (str(exc) != "Данные уже запрашиваются другим ботом"
                            or time.monotonic() >= deadline):
                        raise DataUnavailable("WebSocket стакан: общий кэш недоступен") from exc
                    if self.stop_event.wait(.05):
                        raise DataUnavailable("Получение данных остановлено")
            if owner is None:
                if isinstance(cached, dict) and cached.get("_bcc_ws_book") is True:
                    self._assert_provider_allowed(host)
                    return cached
                raise DataUnavailable("WebSocket стакан: происхождение кэша не подтверждено")
        try:
            self._orderbook_stream.subscribe(symbol)
            book = self._orderbook_stream.get_orderbook(symbol)
            stamp = book["ts"]
            if (type(stamp) not in (float, int) or not math.isfinite(stamp)
                    or not -0.5 <= market_now() - stamp <= 3):
                raise DataUnavailable("WebSocket стакан: неверное время")
            data = {
                "retCode": 0, "retMsg": "OK", "_bcc_ws_book": True,
                "time": int(stamp * 1000),
                "result": {
                    "s": symbol, "ts": int(stamp * 1000),
                    "b": [[str(p), str(q)] for p, q in book["bids"]],
                    "a": [[str(p), str(q)] for p, q in book["asks"]],
                    "u": book["u"], "seq": book["seq"],
                },
            }
        except (StreamUnavailable, ValueError, KeyError, TypeError, DataUnavailable) as exc:
            if self.shared is not None:
                self._finish_shared(key, owner, host)
            raise DataUnavailable("WebSocket Bybit: стакан недоступен") from exc
        if self.shared is not None:
            if not self._finish_shared(key, owner, host, data, .2):
                raise DataUnavailable("WebSocket стакан: публикация не подтверждена")
        self._assert_provider_allowed(host)
        return data

    def _ws_polymarket_get(self, url, params, host):
        """Producer-side on-demand CLOB book, preserving legacy PAPER format."""
        from .ws_quotes import StreamUnavailable
        asset = params["token_id"]
        self._polymarket_stream.subscribe(asset)
        try:
            payload = self._polymarket_stream.get_book(asset)
        except (StreamUnavailable, ValueError) as exc:
            raise DataUnavailable("Polymarket WS: свежий стакан недоступен") from exc
        return {"_bcc_ws_polybook": True, **payload}

    def _ws_binance_price_get(self, url, params, host):
        """Public Binance last trade price compatible with PAPER impulse()."""
        from .ws_quotes import StreamUnavailable
        symbol = params["symbol"]
        self._binance_stream.subscribe(symbol)
        try:
            price, stamp = self._binance_stream.quote(symbol)
        except (StreamUnavailable, ValueError) as exc:
            raise DataUnavailable("Binance WebSocket: нет свежей цены импульса") from exc
        if not math.isfinite(price) or price <= 0:
            raise DataUnavailable("Binance WebSocket: повреждена цена импульса")
        return {"symbol": symbol, "price": str(price),
                "_bcc_ws_binance": True, "_ws_timestamp": stamp}

    def get(self, url, params=None, ttl=0):
        began = time.monotonic()

        def received(data, via):
            self._thread.last_read = dict(
                url=url,
                via=via,
                elapsed_seconds=time.monotonic() - began,
                received_pc=time.time(),
            )
            return data

        if self.stop_event.is_set():
            raise DataUnavailable("Получение данных остановлено")
        host = self.validated_public_request(url, params)
        # In explicitly selected WS mode a missing shared producer cannot
        # silently reactivate old per-coin HTTP polling. Broad risk marks,
        # historical backfills and other unmigrated REST routes stay allowed.
        if os.environ.get("BCC_MARKET_HUB_MODE") != "client":
            if (url == "https://api.bybit.com/v5/market/tickers"
                    and os.environ.get("BCC_BYBIT_WS_QUOTES") == "1"
                    and type(params) is dict and params.get("symbol")
                    and self._quote_stream is None):
                raise DataUnavailable("Bybit WebSocket котировки: Hub не подключён")
            if (url == "https://api.bybit.com/v5/market/orderbook"
                    and os.environ.get("BCC_BYBIT_WS_BOOKS") == "1"
                    and type(params) is dict and params.get("limit") == 50
                    and self._orderbook_stream is None):
                raise DataUnavailable("Bybit WebSocket стакан: Hub не подключён")
            if (url == "https://api.bybit.com/v5/market/kline"
                    and os.environ.get("BCC_BYBIT_WS_KLINES") == "1"
                    and type(params) is dict
                    and set(params) == {"category", "symbol", "interval", "limit"}
                    and params.get("interval") == "1" and params.get("limit") == 10
                    and self._kline_stream is None):
                raise DataUnavailable("Bybit WebSocket свеча: Hub не подключён")
        if (url == "https://clob.polymarket.com/book"
                and type(params) is dict and set(params) == {"token_id"}
                and os.environ.get("BCC_MARKET_HUB_MODE") != "client"
                and os.environ.get("BCC_POLY_WS_BOOKS") == "1"
                and self._polymarket_stream is None):
            raise DataUnavailable("Polymarket WebSocket market Hub недоступен")
        if (url == "https://api.binance.com/api/v3/ticker/price"
                and type(params) is dict and set(params) == {"symbol"}
                and os.environ.get("BCC_MARKET_HUB_MODE") != "client"
                and os.environ.get("BCC_BINANCE_WS_PRICE") == "1"
                and self._binance_stream is None):
            raise DataUnavailable("Binance WebSocket поток импульса не подключён")
        # A refusal is a fleet-wide admission fence, not a retryable cache miss.
        self._assert_provider_allowed(host)
        # Explicit producer-side WebSocket transport for symbol-specific Bybit
        # quotes. Workers never receive the stream or exchange connections.
        if (self._quote_stream is not None
                and url == "https://api.bybit.com/v5/market/tickers"
                and type(params) is dict and set(params) == {"category", "symbol"}
                and params["category"] == "linear"
                and os.environ.get("BCC_MARKET_HUB_MODE") != "client"):
            return received(self._ws_ticker_get(url, params, host), "bybit_ws_hub")
        if (self._orderbook_stream is not None
                and url == "https://api.bybit.com/v5/market/orderbook"
                and type(params) is dict
                and set(params) == {"category", "symbol", "limit"}
                and params["category"] == "linear" and params["limit"] == 50
                and os.environ.get("BCC_MARKET_HUB_MODE") != "client"):
            return received(self._ws_orderbook_get(url, params, host), "bybit_ws_book_hub")
        if (self._polymarket_stream is not None
                and url == "https://clob.polymarket.com/book"
                and type(params) is dict and set(params) == {"token_id"}
                and os.environ.get("BCC_MARKET_HUB_MODE") != "client"):
            return received(self._ws_polymarket_get(url, params, host), "polymarket_ws_book")
        if (self._binance_stream is not None
                and url == "https://api.binance.com/api/v3/ticker/price"
                and type(params) is dict and set(params) == {"symbol"}
                and os.environ.get("BCC_MARKET_HUB_MODE") != "client"):
            return received(self._ws_binance_price_get(url, params, host), "binance_ws_trade")
        # Optional hub consumer: this worker NEVER falls back to direct HTTP.
        # Registration is refreshed by actual demand, not by a synthetic scan.
        if os.environ.get("BCC_MARKET_HUB_MODE") == "client":
            if self.shared is None:
                raise DataUnavailable("Market Data Hub requires BCC_PUBLIC_CACHE")
            from .market_hub import HubRegistry
            subscription_key = HubRegistry.identity(url, params)[0]
            # Only executable books fetched for EXISTING positions get urgent
            # queue priority. Scanner books/candles cannot claim priority.
            urgent = self._position_critical_request(url, params)
            with self._lock:
                if self._hub_registry is None:
                    self._hub_registry = HubRegistry(self.shared.path)
                now_mono = time.monotonic()
                if (now_mono >= self._hub_renew_at.get(subscription_key, 0)
                    or (urgent and now_mono >= self._hub_priority_until.get(subscription_key, 0))):
                    try:
                        self._hub_registry.subscribe(url, params, lifetime=60, priority=urgent)
                    except (OSError, CacheBusy) as exc:
                        raise DataUnavailable("Market Data Hub subscription unavailable: " + str(exc)) from exc
                    self._hub_renew_at[subscription_key] = now_mono + 20
                    if urgent:
                        # Renew before the short-lived urgent lease expires.
                        self._hub_priority_until[subscription_key] = now_mono + 5
            # This is a bounded queue wait, NOT permission to use an expired
            # market snapshot. In an overloaded system the client fails closed.
            deadline = time.monotonic() + 8.0
            while not self.stop_event.is_set():
                try:
                    cached = self.shared.read_fresh(url, params, host)
                except ProviderDenied as exc:
                    raise SourceAccessDenied(exc.host, exc.http_status) from exc
                except CacheBusy as exc:
                    # A transient SQLite reader/writer conflict is not a
                    # failed upstream market request. Retry within the same
                    # bounded wait without ever falling back to direct HTTP.
                    if time.monotonic() >= deadline:
                        raise DataUnavailable("Market Data Hub: кэш SQLite занят: " + str(exc)) from exc
                    self.stop_event.wait(.05)
                    continue
                if cached is not None:
                    if (url == "https://api.bybit.com/v5/market/orderbook"
                            and os.environ.get("BCC_BYBIT_WS_BOOKS") == "1"
                            and type(params) is dict and params.get("limit") == 50
                            and cached.get("_bcc_ws_book") is not True):
                        raise DataUnavailable("Market Data Hub: WebSocket стакан не подтверждён")
                    if (url == "https://api.bybit.com/v5/market/tickers"
                            and os.environ.get("BCC_BYBIT_WS_QUOTES") == "1"
                            and type(params) is dict and params.get("symbol")
                            and cached.get("_bcc_ws_quote") is not True):
                        raise DataUnavailable("Market Data Hub: WebSocket котировка не подтверждена")
                    if (url == "https://api.binance.com/api/v3/ticker/price"
                            and os.environ.get("BCC_BINANCE_WS_PRICE") == "1"
                            and type(params) is dict and set(params) == {"symbol"}
                            and cached.get("_bcc_ws_binance") is not True):
                        raise DataUnavailable("Market Data Hub: Binance WS цена не подтверждена")
                    if (url == "https://clob.polymarket.com/book"
                            and os.environ.get("BCC_POLY_WS_BOOKS") == "1"
                            and type(params) is dict and set(params) == {"token_id"}
                            and cached.get("_bcc_ws_polybook") is not True):
                        raise DataUnavailable("Market Data Hub: Polymarket WS стакан не подтверждён")
                    if (url == "https://api.bybit.com/v5/market/kline"
                            and os.environ.get("BCC_BYBIT_WS_KLINES") == "1"
                            and type(params) is dict and
                            set(params) == {"category", "symbol", "interval", "limit"}
                            and params.get("category") == "linear"
                            and params.get("interval") == "1"
                            and params.get("limit") == 10
                            and cached.get("_bcc_ws_kline") is not True):
                        raise DataUnavailable("Market Data Hub: WebSocket свеча не подтверждена")
                    return received(cached, "market_hub")
                if time.monotonic() >= deadline:
                    raise DataUnavailable("Market Data Hub: нет свежего снимка (очередь или источник недоступен)")
                self.stop_event.wait(0.05)
            raise DataUnavailable("Получение данных остановлено")
        now = time.time()
        key = (url, json.dumps(params, sort_keys=True))
        with self._lock:
            # Shared-cache workers have one authoritative cache. A private
            # in-memory copy cannot be invalidated by another worker's 403/451,
            # so it must never bypass the shared host-scoped policy fence.
            if (self.shared is None and ttl > 0 and key in self.cache
                    and 0 <= now - self.cache[key][0] <= ttl):
                return received(self.cache[key][1], "memory_cache")
        if now < self.cooldowns.get(host, 0):
            # A local exchange cooldown must deny NEW requests, not a fresh
            # shared cache hit. Use a read-only lookup so a miss cannot
            # reserve a new provider request behind the cooldown.
            if self.shared:
                try:
                    cached = self.shared.read_fresh(url, params, host)
                except ProviderDenied as exc:
                    raise SourceAccessDenied(exc.host, exc.http_status) from exc
                except CacheBusy as exc:
                    raise DataUnavailable(str(exc)) from exc
                if cached is not None:
                    return received(cached, "shared_cache")
            raise DataUnavailable("Источник временно ограничил запросы: " + host)
        owner = None
        shared_key = None
        if self.shared:
            # A lease means another worker is already fetching this *same*
            # public response. Wait briefly for its committed shared result
            # instead of discarding the symbol's entire scheduler tick.
            # Only SQLite lock conflicts and this request's in-flight lease
            # can wait inside a bounded window. Provider budgets, cooldowns,
            # 403/451 policy and integrity failures never grant permission.
            # Re-admit through the full shared-cache transaction every retry.
            lease_deadline = time.monotonic() + 13.0
            # A SQLite lock is different from an in-flight shared request.
            # Report a sustained lock to the scheduler promptly so an
            # existing PAPER position can show diagnostics, preserve its mark
            # fence and resume on the next market tick. Waiting the full
            # lease window would deadlock recovery if the lock owner releases
            # it only after seeing that diagnostic.
            lock_deadline = time.monotonic() + 1.5
            while True:
                if self.stop_event.is_set():
                    raise DataUnavailable("Получение данных остановлено")
                try:
                    is_position_critical = (
                        host == 'api.bybit.com'
                        and self._position_critical_request(url, params)
                    )
                    shared_key, owner, cached = self.shared.begin(
                        url, params, host, priority=is_position_critical)
                    break
                except ProviderDenied as exc:
                    raise SourceAccessDenied(exc.host, exc.http_status) from exc
                except CacheBusy as e:
                    if isinstance(e, CacheContention):
                        deadline = min(lease_deadline, lock_deadline)
                    elif str(e) == "Данные уже запрашиваются другим ботом":
                        deadline = lease_deadline
                    else:
                        # A rate budget, provider cooldown, integrity failure
                        # or policy fence must never be reinterpreted as
                        # permission to issue a direct HTTP request.
                        raise DataUnavailable(str(e)) from e
                    if time.monotonic() >= deadline:
                        raise DataUnavailable(str(e)) from e
                    if self.stop_event.wait(0.075):
                        raise DataUnavailable("Получение данных остановлено")
            if owner is None:
                return received(cached, "shared_cache")
        try:
            # The URL/route admission fence applies to the *entire* HTTP
            # exchange, not only the first request. requests follows 3xx by
            # default, potentially reaching an unapproved host/private route
            # before this method sees the final status. No redirect is safe to
            # follow automatically, including a redirect within the same host.
            response = self.session.get(
                url, params=params, timeout=(3, 8), allow_redirects=False
            )
            if 300 <= response.status_code < 400:
                # 3xx is not an HTTPError in requests; accepting a JSON body
                # here would publish unverified market data to all workers.
                # Raise ValueError so the common failure path releases the
                # cross-process request lease without caching a response.
                raise ValueError("Публичный источник вернул перенаправление; переход запрещён")
            if response.status_code in (403, 451):
                # Never evade a provider's geographic/policy restrictions with
                # mirrors, other venues, cached prices, or silent retries.
                # The hub records a matching host-wide cooldown so hundreds
                # of workers cannot retry the same denied endpoint.
                try:
                    requested = float(response.headers.get("Retry-After", 90))
                except (TypeError, ValueError):
                    requested = 90
                delay = (3600 if response.status_code == 451 else
                         max(90, min(3600, requested)))
                with self._lock:
                    self._access_denials[host] = (
                        time.monotonic() + delay, response.status_code
                    )
                    self.cooldowns[host] = max(
                        self.cooldowns.get(host, 0), time.time() + delay
                    )
                if self.shared:
                    self._record_shared_denial(
                        shared_key, owner, host, delay, response.status_code
                    )
                raise SourceAccessDenied(host, response.status_code)
            if response.status_code in (418, 429):
                try:
                    delay = max(
                        90, min(3600, float(response.headers.get("Retry-After", 90)))
                    )
                except ValueError:
                    delay = 90
                self.cooldowns[host] = now + delay
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as e:
                raise ValueError(
                    f"Источник вернул не JSON (HTTP {response.status_code}, "
                    + response.headers.get("Content-Type", "неизвестный формат")
                    + ")"
                ) from e
        except (requests.RequestException, ValueError) as e:
            delay = max(10, self.cooldowns.get(host, now) - now)
            self.cooldowns[host] = max(self.cooldowns.get(host, 0), time.time() + delay)
            if self.shared:
                self._finish_shared(shared_key, owner, host, cooldown=delay)
            raise DataUnavailable(f"{host}: {e}") from e
        if self.shared:
            cache_ttl = ttl or (240 if "/risk-limit" in url else (
                0.35
                if "orderbook" in url or "/book" in url
                else (
                    1
                    if "kline" in url
                    else (
                        60
                        if "funding" in url
                        else 3600 if "instruments" in url else 0.5
                    )
                )
            )
            )
            published = self._finish_shared(shared_key, owner, host, data, cache_ttl)
        # Another in-flight request can receive a 403/451 while this HTTP
        # request is completing. Its host-wide denial takes precedence over
        # this otherwise successful response, even if our lease was valid.
        self._assert_provider_allowed(host)
        if self.shared is not None and not published:
            # A 200 response is not proof of market admission: an obsolete
            # singleflight owner or a locked shared SQLite writer can prevent
            # atomic publication. The producer and direct workers both fail
            # closed instead of trading on an uncommitted private snapshot.
            raise DataUnavailable(
                "Общий кэш: успешный HTTP-снимок не подтверждён транзакцией владельца"
            )
        if self.shared is None:
            with self._lock:
                self.cache[key] = (now, data)
                if len(self.cache) > 1000:
                    self.cache = {
                        k: v
                        for k, v in self.cache.items()
                        if now - v[0] < 600 or k[0] == "bars"
                    }
        return received(data, "http")

    def bybit(self, route, params):
        data = self.get(
            "https://api.bybit.com/v5/market/" + route, {"category": "linear", **params}
        )
        if data.get("retCode") != 0:
            raise DataUnavailable("Bybit: " + str(data.get("retMsg")))
        return data

    def quote(self, symbol):
        clock.ensure()
        if self._quote_stream is not None:
            from .ws_quotes import StreamUnavailable
            self._assert_provider_allowed("api.bybit.com")
            try:
                self._quote_stream.subscribe(symbol)
                observed = self._quote_stream.quote(symbol)
                if (type(observed) not in (tuple, list) or len(observed) != 3
                        or any(type(value) not in (int, float)
                               or not math.isfinite(value) for value in observed)):
                    raise DataUnavailable("Bybit WebSocket: повреждена котировка")
                bid, ask, stamp = observed
                if (bid <= 0 or ask <= 0 or bid > ask or
                        not -5 <= market_now() - stamp <= 2):
                    raise DataUnavailable("Bybit WebSocket: котировка устарела или недостоверна")
                return bid, ask, stamp
            except (StreamUnavailable, ValueError) as exc:
                raise DataUnavailable("Bybit WebSocket: свежая котировка недоступна") from exc
        data = self.bybit("tickers", {"symbol": symbol})
        try:
            rows = data["result"]["list"]
            if not isinstance(rows, list) or len(rows) != 1:
                raise DataUnavailable("Bybit: неоднозначный ответ котировки")
            item = rows[0]
            if not isinstance(item, dict) or item.get("symbol") != symbol:
                raise DataUnavailable("Bybit: котировка другой торговой пары")
            bid = float(item["bid1Price"])
            ask = float(item["ask1Price"])
        except (TypeError, KeyError, IndexError, ValueError, OverflowError) as exc:
            raise DataUnavailable("Bybit: повреждена котировка торговой пары") from exc
        if not all(math.isfinite(x) and x > 0 for x in (bid, ask)) or bid > ask:
            raise DataUnavailable("Невалидная котировка")
        remote = float(data["time"]) / 1000
        if not -5 <= market_now() - remote <= 15:
            raise DataUnavailable("Устаревшая котировка Bybit")
        return bid, ask, remote

    def list_symbols(self):
        from .config import valid_symbol

        cursor = None
        symbols = set()
        for _ in range(30):
            args = {"limit": 1000}
            if cursor:
                args["cursor"] = cursor
            result = self.bybit("instruments-info", args)["result"]
            symbols.update(
                x["symbol"]
                for x in result["list"]
                if x.get("status") == "Trading"
                and x.get("quoteCoin") == "USDT"
                and x.get("contractType") == "LinearPerpetual"
                and valid_symbol(x["symbol"], "VELES")
            )
            next_cursor = result.get("nextPageCursor")
            if not next_cursor:
                break
            if cursor == next_cursor:
                raise DataUnavailable("Не удалось получить полный список пар")
            cursor = next_cursor
        else:
            # Reaching the pagination cap is not proof of a complete catalog.
            # Explicitly reject instead of silently presenting a partial list.
            raise DataUnavailable("Не получен полный список пар Bybit (лимит страниц)")
        return sorted(symbols)

    def candles(self, symbol, required=500):
        clock.ensure()
        required = max(100, int(required))
        with self._lock:
            previous = self.cache.get(("bars", symbol), (0, []))[1]
        # Re-fetch the unfinished tail; its OHLCV must replace the older sample.
        recent_tail = previous and market_now() * 1000 - previous[-1]["t"] <= 9 * 60000
        count = 10 if len(previous) >= required and recent_tail else required
        found = {b["t"]: b for b in previous}
        end = None
        for _ in range((count + 999) // 1000):
            args = {"symbol": symbol, "interval": "1", "limit": min(1000, count)}
            if end is not None:
                args["end"] = end
            data = self.bybit("kline", args)
            rows = data["result"]["list"]
            if not rows:
                break
            for row in rows:
                found[int(row[0])] = dict(
                    t=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            end = min(int(row[0]) for row in rows) - 1
            count -= len(rows)
            if count <= 0:
                break
        bars = [found[t] for t in sorted(found)][-required:]
        if not bars or any(b["t"] - a["t"] != 60000 for a, b in zip(bars, bars[1:])):
            raise DataUnavailable("Разрывы в истории свечей")
        if not 0 <= market_now() - bars[-1]["t"] / 1000 < 120:
            raise DataUnavailable("Устаревшие свечи")
        from .indicators import aggregate

        aggregate(bars, 1, int(market_now() * 1000), "intrabar")
        with self._lock:
            self.cache[("bars", symbol)] = (time.time(), bars)
        return bars

    def position_orderbook(self, symbol):
        """Mark an existing PAPER position before slower strategy discovery.

        Thread-local intent affects only hub ordering; no direct HTTP fallback,
        no changed freshness rules and no additional host request budget.
        """
        prior = getattr(self._thread, 'position_mark', False)
        self._thread.position_mark = True
        try:
            return self.orderbook(symbol)
        finally:
            self._thread.position_mark = prior

    def margin_snapshot(self, symbol):
        from .margin import MarginUnavailable, parse_tiers

        clock.ensure()
        rows, cursor, seen = [], None, set()
        risk_at = None
        for _ in range(20):
            params = {"category": "linear", "symbol": symbol}
            if cursor:
                params["cursor"] = cursor
            response = self.get(
                "https://api.bybit.com/v5/market/risk-limit", params, ttl=240
            )
            if response.get("retCode") != 0:
                raise DataUnavailable("Не получены публичные ступени риска Bybit")
            at = float(response["time"]) / 1000
            risk_at = min(risk_at, at) if risk_at is not None else at
            rows.extend(response["result"]["list"])
            cursor = response["result"].get("nextPageCursor")
            if not cursor:
                break
            if cursor in seen:
                raise DataUnavailable("Повтор страницы ступеней риска")
            seen.add(cursor)
        else:
            raise DataUnavailable("Неполная таблица ступеней риска")
        try:
            tiers = parse_tiers(rows, symbol)
        except MarginUnavailable as exc:
            raise DataUnavailable(str(exc)) from exc
        ticker = self.get(
            "https://api.bybit.com/v5/market/tickers", {"category": "linear"}, ttl=0.5
        )
        if ticker.get("retCode") != 0:
            raise DataUnavailable("Не получен пакет mark-price Bybit")
        rows = ticker["result"]["list"]
        stamp = float(ticker["time"]) / 1000
        if not book_is_fresh(stamp):
            raise DataUnavailable("Не подтверждён свежий mark-price Bybit")
        account_marks = {}
        for item in rows:
            key = item.get("symbol")
            if not isinstance(key, str) or not key.endswith("USDT"):
                continue
            mark = float(item["markPrice"])
            if not math.isfinite(mark) or mark <= 0 or key in account_marks:
                raise DataUnavailable("Повреждён пакет mark-price Bybit")
            account_marks[key] = observed_quote(dict(bid=mark, ask=mark, at=stamp))
        if symbol not in account_marks:
            raise DataUnavailable("В пакете mark-price нет выбранной монеты")
        if risk_at is None or not -0.5 <= market_now() - risk_at <= 300:
            raise DataUnavailable("Устарели ступени риска Bybit")
        return dict(
            symbol=symbol,
            tiers=tiers,
            risk_at=risk_at,
            quote=account_marks[symbol],
            account_marks=account_marks,
        )

    def orderbook(self, symbol):
        clock.ensure()
        instrument = self.get(
            "https://api.bybit.com/v5/market/instruments-info",
            {"category": "linear", "symbol": symbol},
            ttl=3600,
        )["result"]["list"][0]
        if (
            instrument.get("symbol") != symbol
            or instrument.get("status") != "Trading"
            or instrument.get("contractType") != "LinearPerpetual"
            or instrument.get("quoteCoin") != "USDT"
        ):
            raise DataUnavailable(
                "Метаданные не подтверждают доступный бессрочный USDT-контракт "
                + symbol
            )
        # Margin data is independent of executable liquidity. Its failure must
        # block increases in exposure without blocking protective exits.
        margin_data, margin_error = None, ""
        try:
            margin_data = self.margin_snapshot(symbol)
        except (DataUnavailable, ValueError, KeyError, TypeError) as exc:
            margin_error = str(exc)
        # Metadata may require a separate network round trip on the first read.
        # Fetch the executable snapshot only after that round trip has finished.
        response = self.bybit("orderbook", {
            "symbol": symbol,
            "limit": 50 if (self._orderbook_stream is not None
                           or os.environ.get("BCC_BYBIT_WS_BOOKS") == "1") else 200,
        })
        data = response["result"]
        if data.get("s") != symbol:
            raise DataUnavailable("Стакан другой монеты")
        lots = instrument["lotSizeFilter"]
        stamp = float(data["ts"]) / 1000
        bids = [[float(p), float(q)] for p, q in data["b"]]
        asks = [[float(p), float(q)] for p, q in data["a"]]
        for levels, descending in ((bids, True), (asks, False)):
            if not levels or any(
                not math.isfinite(p) or not math.isfinite(q) or p <= 0 or q < 0
                for p, q in levels
            ):
                raise DataUnavailable("Повреждён стакан")
            if any(
                (a[0] <= b[0] if descending else a[0] >= b[0])
                for a, b in zip(levels, levels[1:])
            ):
                raise DataUnavailable("Порядок уровней стакана")
        executable_quote({"bids": bids, "asks": asks})
        transport = getattr(self._thread, "last_read", {})
        response_at = float(response["time"]) / 1000
        check_book_time(
            stamp,
            "Bybit",
            {**transport, "response_minus_book_seconds": response_at - stamp},
        )
        result = dict(
            margin_required=True,
            margin_data=margin_data,
            margin_marks=margin_data.pop("account_marks", {}) if margin_data else {},
            margin_error=margin_error,
            bids=bids,
            asks=asks,
            at=stamp,
            qty_step=float(lots["qtyStep"]),
            min_qty=float(lots["minOrderQty"]),
            min_notional=float(lots.get("minNotionalValue", 5)),
            tick_size=float(instrument["priceFilter"]["tickSize"]),
            max_qty=float(lots["maxMktOrderQty"]),
            response_at=response_at,
            transport=transport,
            engine_at=(
                float(data["cts"]) / 1000 if data.get("cts") is not None else None
            ),
            update_id=data.get("u"),
            sequence=data.get("seq"),
        )
        if any(
            not math.isfinite(result[key]) or result[key] <= 0
            for key in ("qty_step", "min_qty", "min_notional", "tick_size", "max_qty")
        ):
            raise DataUnavailable("Некорректные биржевые ограничения объёма / цены")
        return result

    def funding(self, symbol):
        data = self.get(
            "https://api.bybit.com/v5/market/funding/history",
            {"category": "linear", "symbol": symbol, "limit": 50},
            ttl=60,
        )
        if data.get("retCode") != 0:
            raise DataUnavailable("Нет истории funding")
        return [
            {
                "at": int(x["fundingRateTimestamp"]) / 1000,
                "rate": float(x["fundingRate"]),
            }
            for x in data["result"]["list"]
        ]

    def funding_mark(self, symbol, at):
        data = self.get(
            "https://api.bybit.com/v5/market/mark-price-kline",
            {
                "category": "linear",
                "symbol": symbol,
                "interval": "1",
                "start": int(at * 1000),
                "limit": 1,
            },
            ttl=3600,
        )
        rows = data.get("result", {}).get("list", [])
        if data.get("retCode") != 0 or not rows or int(rows[0][0]) != int(at * 1000):
            raise DataUnavailable("Нет mark-price на время funding")
        price = float(rows[0][1])
        if not math.isfinite(price) or price <= 0:
            raise DataUnavailable("Некорректная mark-price funding")
        return price


def iso_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


COIN_NAMES = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "xrp",
    "DOGE": "dogecoin",
    "HYPE": "hyperliquid",
    "ZEC": "zcash",
}


class PolymarketData(PublicData):
    """Gamma discovery + CLOB executable depth + official resolved payouts.

    References: docs.polymarket.com/market-data/discover-markets and
    docs.polymarket.com/market-data/public-analytics (GET /v2/tokens).
    """

    def list_symbols(self):
        """Return the strategy's selectable coin catalog, NOT live market availability.

        The Gamma markets endpoint lists markets, not supported coin symbols.
        Paging a small arbitrary window of active markets silently returns no
        matching coins when the catalogue is dominated by unrelated events.
        A concrete coin/duration market is verified by discover() on demand,
        and again at PAPER entry. No catalog entry promises a live orderbook.
        """
        return sorted(COIN_NAMES)

    def discover(self, coin, duration, now=None):
        clock.ensure()
        now = market_now(now)
        if duration in (300, 900):
            slug = f"{coin.lower()}-updown-{duration // 60}m-{int(now) // duration * duration}"
            rows = self.get(
                "https://gamma-api.polymarket.com/markets", {"slug": slug}, ttl=3
            )
        else:
            # Hourly slugs use local-language dates; discover them from metadata.
            rows = []
            for offset in range(0, 1000, 100):
                batch = self.get(
                    "https://gamma-api.polymarket.com/markets",
                    {
                        "active": "true",
                        "closed": "false",
                        "limit": 100,
                        "offset": offset,
                        "end_date_min": datetime.fromtimestamp(
                            now, timezone.utc
                        ).isoformat(),
                        "end_date_max": datetime.fromtimestamp(
                            now + 3700, timezone.utc
                        ).isoformat(),
                    },
                    ttl=20,
                )
                rows.extend(batch)
                if len(batch) < 100:
                    break
        candidates = []
        for row in rows:
            try:
                slug = row["slug"]
                title = row["question"].lower()
                if "up or down" not in title:
                    continue
                name = COIN_NAMES.get(coin, coin.lower())
                if not (
                    title.startswith(name + " ")
                    or slug.startswith(coin.lower() + "-")
                    or slug.startswith(name + "-")
                ):
                    continue
                end = iso_time(row["endDate"])
                matched = re.search(r"-updown-(5|15)m-(\d+)$", slug)
                if matched:
                    start = float(matched.group(2))
                    actual = int(matched.group(1)) * 60
                else:
                    event = (row.get("events") or [{}])[0]
                    start = iso_time(
                        row.get("eventStartTime")
                        or event.get("startTime")
                        or row.get("gameStartTime")
                        or ""
                    )
                    actual = int(round(end - start))
                if (
                    actual != duration
                    or abs(end - start - duration) > 1
                    or not start <= now < end
                    or row.get("closed")
                    or row.get("acceptingOrders") is not True
                ):
                    continue
                outcomes = row["outcomes"]
                tokens = row["clobTokenIds"]
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                if isinstance(tokens, str):
                    tokens = json.loads(tokens)
                # A truncated zip or duplicate outcome must not silently turn
                # ambiguous metadata into an apparently tradable UP/DOWN pair.
                if (not isinstance(outcomes, list) or not isinstance(tokens, list)
                    or len(outcomes) != 2 or len(tokens) != 2
                    or any(not isinstance(x, str) for x in outcomes + tokens)):
                    continue
                names = [name.strip().upper() for name in outcomes]
                ids = [token.strip() for token in tokens]
                if set(names) != {"UP", "DOWN"}:
                    continue
                try:
                    mapping = canonical_up_down_tokens(dict(zip(names, ids)))
                except DataUnavailable:
                    continue
                candidates.append(
                    dict(
                        id=str(row["id"]),
                        condition=row["conditionId"],
                        slug=slug,
                        start=start,
                        end=end,
                        tokens=mapping,
                        fees_enabled=row.get("feesEnabled"),
                        fee_schedule=(
                            row.get("feeSchedule")
                            if row.get("feesEnabled") is not False
                            else {"rate": 0.0, "exponent": 1}
                        ),
                        min_size=row.get("orderMinSize"),
                        tick_size=row.get("orderPriceMinTickSize"),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        if len(candidates) != 1:
            raise DataUnavailable(
                "Не найден единственный активный UP/DOWN рынок "
                + coin
                + f" {duration // 60}m"
            )
        return candidates[0]

    def impulse(self, coin, start, now):
        # Signal source is Binance spot; settlement never uses this price.
        rows = self.get(
            "https://api.binance.com/api/v3/klines",
            {
                "symbol": coin + "USDT",
                "interval": "1m",
                "startTime": int(start * 1000),
                "limit": 1,
            },
            ttl=60,
        )
        if not rows or int(rows[0][0]) != int(start * 1000):
            raise DataUnavailable("Нет цены начала рынка")
        first = float(rows[0][1])
        ticker = self.get(
            "https://api.binance.com/api/v3/ticker/price",
            {"symbol": coin + "USDT"},
            ttl=0.5,
        )
        # A response belonging to a different symbol must not invert the
        # impulse direction and purchase the wrong UP/DOWN PAPER outcome.
        if not isinstance(ticker, dict) or ticker.get("symbol") != coin + "USDT":
            raise DataUnavailable("Котировка другой торговой пары Binance")
        if ticker.get("_bcc_ws_binance") is True:
            # Existing REST-only strategies retain their old clock contract;
            # the new streaming path must prove its independent trading UTC.
            clock.ensure()
            try:
                stamp = float(ticker["_ws_timestamp"])
            except (KeyError, ValueError, TypeError, OverflowError) as exc:
                raise DataUnavailable("Binance WebSocket: неверное время импульса") from exc
            if not math.isfinite(stamp) or not -1 <= market_now() - stamp <= 2:
                raise DataUnavailable("Binance WebSocket: цена импульса устарела")
        last = float(ticker["price"])
        if not all(math.isfinite(x) and x > 0 for x in (first, last)):
            raise DataUnavailable("Некорректная цена импульса")
        return last - first, 100 * (last / first - 1)

    def indicator_bars(self, coin, required, now):
        """Verified closed Binance 1m OHLCV for optional indicator conditions.

        Public endpoint only. Never use incomplete current-minute candles,
        shifted timestamps, gaps, or data that may belong to another market.
        """
        from .poly_indicators import MAX_BARS
        from .indicators import aggregate
        if type(required) is not int or not 2 <= required <= MAX_BARS:
            raise DataUnavailable("Недопустимая длина истории для фильтра")
        if type(now) not in (float, int) or not math.isfinite(now):
            raise DataUnavailable("Некорректное время фильтра")
        minute_start = int(now // 60) * 60000
        rows = self.get("https://api.binance.com/api/v3/klines", {
            "symbol": coin + "USDT", "interval": "1m",
            "endTime": minute_start - 1, "limit": required,
        }, ttl=0)
        if not isinstance(rows, list) or len(rows) < required:
            raise DataUnavailable("Недостаточно свечей для фильтра")
        candles = []
        for row in rows[-required:]:
            try:
                t = int(row[0])
                close_at = int(row[6])
                values = [float(row[x]) for x in (1, 2, 3, 4, 5)]
            except (IndexError, TypeError, ValueError, OverflowError) as exc:
                raise DataUnavailable("Повреждён ответ свечей Binance") from exc
            if t % 60000 or close_at != t + 59999 or close_at >= minute_start:
                raise DataUnavailable("Незакрытая или смещённая свеча Binance")
            candles.append(dict(t=t, open=values[0], high=values[1], low=values[2],
                                close=values[3], volume=values[4]))
        if candles[-1]["t"] != minute_start - 60000:
            raise DataUnavailable("Нет последней закрытой свечи Binance")
        if any(b["t"] - a["t"] != 60000 for a, b in zip(candles, candles[1:])):
            raise DataUnavailable("Пропуск минутных свечей Binance")
        try:
            aggregate(candles, 1, minute_start, "bar_close")
        except (TypeError, ValueError, KeyError) as exc:
            raise DataUnavailable("Недостоверные данные OHLCV Binance") from exc
        return candles

    def book(self, token):
        clock.ensure()
        data = self.get("https://clob.polymarket.com/book", {"token_id": token})
        if str(data.get("asset_id")) != token:
            raise DataUnavailable("Стакан другого токена")
        stamp = float(data["timestamp"]) / 1000
        check_book_time(stamp, "Polymarket")
        return data["asks"], stamp

    def execution_terms(self, market, require_fees=True):
        schedule = market.get("fee_schedule")
        if (
            not schedule
            and require_fees
            or market.get("min_size") is None
            or market.get("tick_size") is None
        ):
            data = self.get(
                "https://clob.polymarket.com/clob-markets/" + market["condition"],
                ttl=60,
            )
            details = data.get("fd", {})
            if not schedule:
                schedule = {"rate": details.get("r"), "exponent": details.get("e")}
            market["min_size"] = data.get("mos")
            market["tick_size"] = data.get("mts")
        minimum = float(market["min_size"])
        tick = float(market["tick_size"])
        if (
            not math.isfinite(minimum)
            or minimum <= 0
            or not math.isfinite(tick)
            or not 0 < tick < 1
        ):
            raise DataUnavailable("Не подтверждены ограничения ордеров рынка")
        if not require_fees:
            return {"fee_schedule": None, "min_size": minimum, "tick_size": tick}
        rate = schedule.get("rate")
        exponent = schedule.get("exponent")
        if (
            any(
                type(x) not in (int, float) or not math.isfinite(x)
                for x in (rate, exponent)
            )
            or not 0 <= rate <= 1
            or not 0 <= exponent <= 4
        ):
            raise DataUnavailable("Не подтверждены публичные параметры комиссии")
        return {
            "fee_schedule": {"rate": rate, "exponent": exponent},
            "min_size": minimum,
            "tick_size": tick,
        }

    def payout(self, token):
        data = self.get(
            "https://data-api.polymarket.com/v2/tokens", {"token_id": token}, ttl=5
        )
        rows = data.get("data", []) if isinstance(data, dict) else []
        if isinstance(rows, dict):
            rows = rows.get("items", [])
        if not isinstance(rows, list):
            raise DataUnavailable("Неверная структура официальных исходов")
        matches = []
        for row in rows:
            if not isinstance(row, dict):
                raise DataUnavailable("Повреждён ответ официальных исходов")
            asset = str(
                row.get("asset_id", row.get("assetId", row.get("token_id", "")))
            )
            if asset != token:
                continue
            # Any unresolved or invalid duplicate makes the official result
            # ambiguous. Never choose the first record or settle twice.
            if row.get("resolved") is not True:
                return None
            value = row.get("final_price", row.get("finalPrice"))
            try:
                payout = float(value) if value is not None else float("nan")
            except (ValueError, TypeError, OverflowError):
                return None
            if not math.isfinite(payout) or not 0 <= payout <= 1:
                return None
            matches.append(payout)
        if not matches:
            return None
        if any(payout != matches[0] for payout in matches[1:]):
            raise DataUnavailable("Противоречивые официальные исходы рынка")
        return matches[0]

    def outcome_quote(self, token):
        clock.ensure()
        data = self.get("https://clob.polymarket.com/book", {"token_id": token})
        if str(data.get("asset_id")) != token:
            raise DataUnavailable("Стакан другого исхода")
        at = float(data["timestamp"]) / 1000
        if not book_is_fresh(at):
            raise DataUnavailable("Устаревшая оценка позиции")
        # Validate *every* level before choosing executable bid/ask depth.
        # Ignoring a negative, NaN or infinite size can make a damaged CLOB
        # response look like a trustworthy PAPER collateral mark.
        def executable_prices(side):
            rows = data.get(side)
            if not isinstance(rows, list):
                raise DataUnavailable("Повреждён стакан оценки позиции")
            prices = []
            for row in rows:
                if not isinstance(row, dict):
                    raise DataUnavailable("Повреждён стакан оценки позиции")
                try:
                    price, size = float(row["price"]), float(row["size"])
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    raise DataUnavailable("Повреждён стакан оценки позиции") from exc
                if (not math.isfinite(price) or not math.isfinite(size)
                        or not 0 < price < 1 or size < 0):
                    raise DataUnavailable("Повреждён стакан оценки позиции")
                if size > 0:
                    prices.append(price)
            return prices

        bids = executable_prices("bids")
        asks = executable_prices("asks")
        if not bids or not asks or max(bids) > min(asks):
            raise DataUnavailable("Нет достоверной оценки позиции")
        return observed_quote({"bid": max(bids), "ask": min(asks), "at": at})


def estimate_buy(asks, budget, max_ask, fee_rate, fee_schedule=None):
    clean = []
    for row in asks:
        price = float(row["price"])
        size = float(row["size"])
        if (
            not math.isfinite(price)
            or not math.isfinite(size)
            or not 0 < price < 1
            or size < 0
        ):
            raise DataUnavailable("Повреждён стакан")
        if price <= max_ask:
            clean.append((price, size))
    clean.sort()
    left = budget
    cost = shares = fee = 0
    for price, size in clean:
        per_share = (
            fee_schedule["rate"] * (price * (1 - price)) ** fee_schedule["exponent"]
            if fee_schedule is not None
            else price * fee_rate
        )
        qty = min(size, left / (price + per_share))
        cost += price * qty
        fee += per_share * qty
        shares += qty
        left -= (price + per_share) * qty
        if left <= 1e-8:
            break
    if left > 1e-7 or shares <= 0:
        raise DataUnavailable("Недостаточная глубина стакана")
    return cost, shares, fee
