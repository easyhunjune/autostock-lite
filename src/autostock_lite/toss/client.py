"""토스증권 읽기 전용 클라이언트 골격과 rate limiter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import re
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from .models import Candle, PriceResponse, StockInfo, StrictModelError
from ..status_codes import InvalidArgumentError


BASE_URL = "https://openapi.tossinvest.com"
KST = timezone(timedelta(hours=9))


class ReadonlyPathError(ValueError):
    pass


class RateLimitGroup(str, Enum):
    STOCK = "STOCK"
    MARKET_DATA = "MARKET_DATA"
    MARKET_DATA_CHART = "MARKET_DATA_CHART"
    RANKING = "RANKING"
    STOCK_TRADING_TREND = "STOCK_TRADING_TREND"
    MARKET_INDICATOR = "MARKET_INDICATOR"
    MARKET_INFO = "MARKET_INFO"


DEFAULT_TPS: dict[RateLimitGroup, float] = {
    RateLimitGroup.STOCK: 5.0,
    RateLimitGroup.MARKET_DATA: 10.0,
    RateLimitGroup.MARKET_DATA_CHART: 5.0,
    RateLimitGroup.RANKING: 5.0,
    RateLimitGroup.STOCK_TRADING_TREND: 1.0,
    RateLimitGroup.MARKET_INDICATOR: 1.0,
    RateLimitGroup.MARKET_INFO: 3.0,
}


class Operation(str, Enum):
    GET_STOCKS = "getStocks"
    GET_PRICES = "getPrices"
    GET_CANDLES = "getCandles"
    GET_ORDERBOOK = "getOrderbook"
    GET_STOCK_WARNINGS = "getStockWarnings"
    GET_PRICE_LIMITS = "getPriceLimit"
    GET_RANKINGS = "getRankings"
    GET_STOCK_INVESTOR_TRADING = "getStockInvestorTrading"
    GET_MARKET_INVESTOR_TRADING = "getMarketIndicatorInvestorTrading"
    GET_MARKET_CALENDAR = "getKrMarketCalendar"
    GET_EXCHANGE_RATE = "getExchangeRate"


@dataclass(frozen=True)
class OperationContract:
    method: str
    path_pattern: re.Pattern[str]
    rate_limit_group: RateLimitGroup


def _path(pattern: str) -> re.Pattern[str]:
    return re.compile(f"^{pattern}$")


OPERATION_CONTRACTS: dict[Operation, OperationContract] = {
    Operation.GET_STOCKS: OperationContract("GET", _path(r"/api/v1/stocks"), RateLimitGroup.STOCK),
    Operation.GET_PRICES: OperationContract("GET", _path(r"/api/v1/prices"), RateLimitGroup.MARKET_DATA),
    Operation.GET_CANDLES: OperationContract("GET", _path(r"/api/v1/candles"), RateLimitGroup.MARKET_DATA_CHART),
    Operation.GET_ORDERBOOK: OperationContract("GET", _path(r"/api/v1/orderbook"), RateLimitGroup.MARKET_DATA),
    Operation.GET_STOCK_WARNINGS: OperationContract("GET", _path(r"/api/v1/stocks/[^/]+/warnings"), RateLimitGroup.STOCK),
    Operation.GET_PRICE_LIMITS: OperationContract("GET", _path(r"/api/v1/price-limits"), RateLimitGroup.MARKET_DATA),
    Operation.GET_RANKINGS: OperationContract("GET", _path(r"/api/v1/rankings"), RateLimitGroup.RANKING),
    Operation.GET_STOCK_INVESTOR_TRADING: OperationContract("GET", _path(r"/api/v1/stocks/[^/]+/investor-trading"), RateLimitGroup.STOCK_TRADING_TREND),
    Operation.GET_MARKET_INVESTOR_TRADING: OperationContract("GET", _path(r"/api/v1/market-indicators/[^/]+/investor-trading"), RateLimitGroup.MARKET_INDICATOR),
    Operation.GET_MARKET_CALENDAR: OperationContract("GET", _path(r"/api/v1/market-calendar/KR"), RateLimitGroup.MARKET_INFO),
    Operation.GET_EXCHANGE_RATE: OperationContract("GET", _path(r"/api/v1/exchange-rate"), RateLimitGroup.MARKET_INFO),
}


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class TokenBucket:
    rate: float
    capacity: float
    tokens: float
    updated_at: float
    blocked_until: float = 0.0

    def refill(self, now: float) -> None:
        if now < self.updated_at:
            raise ValueError("clock가 뒤로 이동했다")
        had_header_block = self.blocked_until > 0
        self.tokens = min(self.capacity, self.tokens + (now - self.updated_at) * self.rate)
        self.updated_at = now
        if had_header_block and now >= self.blocked_until and self.tokens == 0:
            self.tokens = min(self.capacity, 1.0)
        if had_header_block and now >= self.blocked_until:
            self.blocked_until = 0.0


class RateLimiter:
    def __init__(self, *, clock: Clock | None = None) -> None:
        self.clock = clock or SystemClock()
        now = self.clock.now()
        self.buckets = {
            group: TokenBucket(rate=tps, capacity=tps, tokens=tps, updated_at=now)
            for group, tps in DEFAULT_TPS.items()
        }

    def acquire(self, group: RateLimitGroup) -> None:
        bucket = self.buckets[group]
        now = self.clock.now()
        if now < bucket.blocked_until:
            self.clock.sleep(bucket.blocked_until - now)
            now = self.clock.now()
        bucket.refill(now)
        if bucket.tokens < 1:
            delay = (1 - bucket.tokens) / bucket.rate
            self.clock.sleep(delay)
            bucket.refill(self.clock.now())
        bucket.tokens -= 1

    def update_from_headers(self, group: RateLimitGroup, headers: Mapping[str, str]) -> None:
        normalized = {key.lower(): value for key, value in headers.items()}
        bucket = self.buckets[group]
        now = self.clock.now()
        limit_text = normalized.get("x-ratelimit-limit")
        remaining_text = normalized.get("x-ratelimit-remaining")
        reset_text = normalized.get("x-ratelimit-reset")
        if limit_text is not None:
            limit = float(limit_text)
            if limit <= 0:
                raise ValueError("X-RateLimit-Limit는 양수여야 한다")
            bucket.rate = limit
            bucket.capacity = limit
            bucket.tokens = min(bucket.tokens, limit)
        if remaining_text is not None:
            remaining = float(remaining_text)
            if remaining < 0:
                raise ValueError("X-RateLimit-Remaining은 음수가 될 수 없다")
            bucket.tokens = min(bucket.capacity, remaining)
            bucket.updated_at = now
        if reset_text is not None and bucket.tokens < 1:
            reset_after_seconds = float(reset_text)
            if reset_after_seconds < 0:
                raise ValueError("X-RateLimit-Reset은 음수가 될 수 없다")
            bucket.blocked_until = now + reset_after_seconds


@dataclass(frozen=True)
class PreparedRequest:
    operation: Operation
    method: str
    path: str
    params: Mapping[str, str]
    rate_limit_group: RateLimitGroup


class TossReadonlyClient:
    """실 HTTP 연결 전에도 안전 계약을 검증할 수 있는 요청 준비 계층."""

    def __init__(self, *, rate_limiter: RateLimiter | None = None) -> None:
        self.rate_limiter = rate_limiter or RateLimiter()

    def prepare_get(
        self,
        operation: Operation,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> PreparedRequest:
        if "?" in path or "://" in path:
            raise ReadonlyPathError("path와 query params를 분리해야 한다")
        contract = OPERATION_CONTRACTS[operation]
        if contract.method != "GET" or contract.path_pattern.fullmatch(path) is None:
            raise ReadonlyPathError(f"허용되지 않은 읽기 경로: GET {path}")
        return PreparedRequest(
            operation=operation,
            method="GET",
            path=path,
            params=dict(params or {}),
            rate_limit_group=contract.rate_limit_group,
        )

    def wait_for_slot(self, request: PreparedRequest) -> None:
        self.rate_limiter.acquire(request.rate_limit_group)

    def apply_rate_limit_headers(
        self, request: PreparedRequest, headers: Mapping[str, str]
    ) -> None:
        self.rate_limiter.update_from_headers(request.rate_limit_group, headers)


class CandleRangeError(StrictModelError):
    """A page sequence cannot prove a complete, non-overlapping candle range."""


@dataclass(frozen=True)
class CandlePage:
    candles: tuple[Candle, ...]
    next_before: str | None


@dataclass(frozen=True)
class ComputedField:
    value: str
    currency: str
    formula: str
    as_of: str
    source: str = "computed"
    is_provisional: bool = False


@dataclass(frozen=True)
class PriceWithChange:
    price: PriceResponse
    currency: str
    previous_close: ComputedField
    change: ComputedField
    change_rate: ComputedField
    volume: ComputedField


class JsonReader(Protocol):
    def __call__(self, request: PreparedRequest) -> Mapping[str, Any]: ...


class TossMarketData:
    """The thirteen allow-listed read operations, backed by a fixture-friendly reader.

    The reader can be ``TossLiveClient.get_json`` in production.  Keeping parsing in
    this layer makes offline tests exercise exactly the same checks.
    """

    def __init__(self, reader: JsonReader, *, readonly: TossReadonlyClient | None = None, now: Callable[[], datetime] | None = None) -> None:
        self._reader = reader
        self._readonly = readonly or TossReadonlyClient()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _result(self, operation: Operation, path: str, params: Mapping[str, str] | None = None) -> Any:
        request = self._readonly.prepare_get(operation, path, params=params)
        payload = self._reader(request)
        if not isinstance(payload, Mapping) or "result" not in payload:
            raise StrictModelError("Toss response must be an object with result")
        return payload["result"]

    @staticmethod
    def _symbols(symbols: Sequence[str]) -> str:
        if not symbols or any(not isinstance(symbol, str) or not symbol for symbol in symbols):
            raise ValueError("symbols must be a non-empty list of symbols")
        if len(symbols) > 200:
            raise ValueError("Toss accepts at most 200 symbols")
        if len(set(symbols)) != len(symbols):
            raise ValueError("symbols must not contain duplicates")
        return ",".join(symbols)

    @staticmethod
    def _verify_symbol_response(requested: Sequence[str], received: Sequence[str], *, kind: str) -> None:
        expected = set(requested)
        actual = set(received)
        if len(received) != len(actual):
            raise StrictModelError(f"{kind} response contains duplicate symbols")
        if actual != expected:
            missing, unexpected = sorted(expected - actual), sorted(actual - expected)
            raise StrictModelError(f"{kind} response symbols do not match request: missing={missing}, unexpected={unexpected}")

    @staticmethod
    def _symbol(symbol: str) -> str:
        if not isinstance(symbol, str) or not symbol or "/" in symbol:
            raise ValueError("symbol must be a non-empty path-safe string")
        return symbol

    def get_stock_info(self, symbols: Sequence[str]) -> tuple[StockInfo, ...]:
        result = self._result(Operation.GET_STOCKS, "/api/v1/stocks", {"symbols": self._symbols(symbols)})
        if not isinstance(result, list):
            raise StrictModelError("stock result must be a list")
        parsed = tuple(StockInfo.from_dict(self._mapping(item, "stock result")) for item in result)
        self._verify_symbol_response(symbols, [item.symbol for item in parsed], kind="stock")
        return parsed

    def lookup_stock_info(self, symbols: Sequence[str]) -> tuple[StockInfo, ...]:
        """Tolerant lookup for symbol resolution only; integrity failures still raise.

        Omit absent or unparsable requested items. All other callers must use the strict get_stock_info.
        """
        result = self._result(Operation.GET_STOCKS, "/api/v1/stocks", {"symbols": self._symbols(symbols)})
        if not isinstance(result, list):
            raise StrictModelError("stock result must be a list")
        requested = set(symbols)
        seen: set[str] = set()
        parsed: list[StockInfo] = []
        for item in result:
            payload = self._mapping(item, "stock result")
            symbol = payload.get("symbol")
            if not isinstance(symbol, str) or not symbol:
                raise StrictModelError("stock result symbol must be a non-empty string")
            if symbol not in requested:
                raise StrictModelError(f"stock response contains unexpected={symbol!r}")
            if symbol in seen:
                raise StrictModelError("stock response contains duplicate symbols")
            seen.add(symbol)
            try:
                stock = StockInfo.from_dict(payload)
            except StrictModelError:
                continue
            parsed.append(stock)
        return tuple(parsed)

    def get_price(self, symbols: Sequence[str]) -> tuple[PriceResponse, ...]:
        result = self._result(Operation.GET_PRICES, "/api/v1/prices", {"symbols": self._symbols(symbols)})
        if not isinstance(result, list):
            raise StrictModelError("price result must be a list")
        parsed = tuple(PriceResponse.from_dict(self._mapping(item, "price result")) for item in result)
        self._verify_symbol_response(symbols, [item.symbol for item in parsed], kind="price")
        return parsed

    @staticmethod
    def _mapping(value: object, name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise StrictModelError(f"{name} must be an object")
        return value

    def get_candles(self, symbol: str, *, interval: str, count: int = 200, before: str | None = None, adjusted: bool = True) -> CandlePage:
        if interval not in {"1m", "1d"} or not 1 <= count <= 200:
            raise ValueError("interval must be 1m/1d and count must be in 1..200")
        params = {"symbol": self._symbol(symbol), "interval": interval, "count": str(count), "adjusted": str(adjusted).lower()}
        if before is not None:
            params["before"] = before
        result = self._result(Operation.GET_CANDLES, "/api/v1/candles", params)
        body = self._mapping(result, "candle result")
        if set(body) != {"candles", "nextBefore"} or not isinstance(body["candles"], list):
            raise StrictModelError("candle page has an unexpected schema")
        next_before = body["nextBefore"]
        if next_before is not None and not isinstance(next_before, str):
            raise StrictModelError("nextBefore must be a timestamp or null")
        if next_before is not None:
            self._parse_timestamp(next_before)
        candles = tuple(Candle.from_dict(self._mapping(item, "candle")) for item in body["candles"])
        return CandlePage(candles, next_before)

    def get_candles_range(self, symbol: str, *, interval: str, from_: str, to: str, adjusted: bool = True) -> tuple[Candle, ...]:
        lower, upper = self._parse_timestamp(from_), self._parse_timestamp(to)
        if lower > upper:
            raise InvalidArgumentError("from must not be after to")
        before: str | None = None
        seen: set[datetime] = set()
        candles: list[Candle] = []
        previous_oldest: datetime | None = None
        while True:
            page = self.get_candles(symbol, interval=interval, count=200, before=before, adjusted=adjusted)
            if not page.candles:
                if page.next_before is not None:
                    raise CandleRangeError("empty candle page has a continuation cursor")
                break
            page_times = [self._parse_timestamp(c.timestamp) for c in page.candles]
            if page_times != sorted(page_times, reverse=True) or len(set(page_times)) != len(page_times):
                raise CandleRangeError("each candle page must be strictly newest-to-oldest")
            if previous_oldest is not None and page_times[0] >= previous_oldest:
                raise CandleRangeError("candle pages overlap or do not advance")
            for candle, timestamp in zip(page.candles, page_times):
                if timestamp in seen:
                    raise CandleRangeError("duplicate candle returned across pages")
                seen.add(timestamp)
                if lower <= timestamp <= upper:
                    candles.append(candle)
            oldest = page_times[-1]
            if oldest <= lower:
                break
            if page.next_before is None:
                raise CandleRangeError("candle pagination ended before reaching the requested lower boundary")
            cursor_time = self._parse_timestamp(page.next_before)
            if cursor_time >= oldest:
                raise CandleRangeError("nextBefore must be older than the oldest candle")
            previous_oldest = oldest
            before = page.next_before
        return tuple(sorted(candles, key=lambda candle: self._parse_timestamp(candle.timestamp)))

    def get_price_with_change(self, symbols: Sequence[str]) -> tuple[PriceWithChange, ...]:
        prices = self.get_price(symbols)
        result: list[PriceWithChange] = []
        now = self._now()
        if now.tzinfo is None:
            raise ValueError("injected clock must return an aware datetime")
        for price in prices:
            page = self.get_candles(price.symbol, interval="1d", count=2, adjusted=True)
            if len(page.candles) < 2:
                raise StrictModelError("two daily candles are required to compute price change")
            latest, previous = page.candles[0], page.candles[1]
            if len({price.currency, latest.currency, previous.currency}) != 1:
                raise StrictModelError("price and daily candles must use the same currency")
            source_time = datetime.fromisoformat(latest.timestamp[:-1] + "+00:00" if latest.timestamp.endswith("Z") else latest.timestamp)
            if source_time.tzinfo is None:
                raise StrictModelError("daily candle timestamp must carry a UTC offset")
            as_of = latest.timestamp
            previous_close = Decimal(previous.closePrice)
            current = Decimal(price.lastPrice)
            change = current - previous_close
            rate = (change / previous_close * Decimal("100")) if previous_close else None
            if rate is None:
                raise StrictModelError("cannot compute a change rate with a zero previous close")
            provisional = (
                now.astimezone(KST).date() == source_time.astimezone(KST).date()
                if price.currency == "KRW"
                else now.astimezone(source_time.tzinfo).date() == source_time.date()
            )
            result.append(PriceWithChange(
                price=price,
                currency=price.currency,
                previous_close=ComputedField(str(previous_close), price.currency, "previous daily candle closePrice", as_of),
                change=ComputedField(str(change), price.currency, "lastPrice - previous_close", as_of),
                change_rate=ComputedField(str(rate), price.currency, "(lastPrice - previous_close) / previous_close * 100", as_of),
                volume=ComputedField(latest.volume, price.currency, "latest daily candle volume", as_of, is_provisional=provisional),
            ))
        return tuple(result)

    # The remaining allow-listed read tools deliberately retain their documented,
    # source-shaped payloads; no model guesses fields absent from the OpenAPI contract.
    def get_orderbook(self, symbol: str) -> Mapping[str, Any]: return self._object(Operation.GET_ORDERBOOK, "/api/v1/orderbook", {"symbol": self._symbol(symbol)})
    def get_stock_warnings(self, symbol: str) -> Any: return self._result(Operation.GET_STOCK_WARNINGS, f"/api/v1/stocks/{self._symbol(symbol)}/warnings")
    def get_price_limits(self, symbol: str) -> Mapping[str, Any]: return self._object(Operation.GET_PRICE_LIMITS, "/api/v1/price-limits", {"symbol": self._symbol(symbol)})
    def get_rankings(self, *, type: str, market: str, duration: str) -> Mapping[str, Any]: return self._object(Operation.GET_RANKINGS, "/api/v1/rankings", {"type": type, "marketCountry": market, "duration": duration})
    def get_stock_investor_trading(self, symbol: str, *, count: int = 20, until: str | None = None) -> Mapping[str, Any]: return self._object(Operation.GET_STOCK_INVESTOR_TRADING, f"/api/v1/stocks/{self._symbol(symbol)}/investor-trading", self._count_until(count, until))
    def get_market_investor_trading(self, market: str, *, interval: str, count: int = 20, until: str | None = None) -> Mapping[str, Any]:
        if market not in {"KOSPI", "KOSDAQ"}: raise ValueError("market investor trading accepts KOSPI or KOSDAQ only")
        params = {"interval": interval, **self._count_until(count, until)}
        return self._object(Operation.GET_MARKET_INVESTOR_TRADING, f"/api/v1/market-indicators/{market}/investor-trading", params)
    def get_market_calendar(self, *, country: str = "KR", date: str | None = None) -> Mapping[str, Any]:
        if country != "KR": raise ValueError("only the Korean market calendar is in scope")
        return self._object(Operation.GET_MARKET_CALENDAR, "/api/v1/market-calendar/KR", {} if date is None else {"date": date})
    def get_exchange_rate(self, *, base_currency: str = "USD", quote_currency: str = "KRW") -> Mapping[str, Any]: return self._object(Operation.GET_EXCHANGE_RATE, "/api/v1/exchange-rate", {"baseCurrency": base_currency, "quoteCurrency": quote_currency})

    def _object(self, operation: Operation, path: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        return self._mapping(self._result(operation, path, params), "result")

    @staticmethod
    def _count_until(count: int, until: str | None) -> dict[str, str]:
        if not 1 <= count <= 200: raise ValueError("count must be in 1..200")
        params = {"count": str(count)}
        if until is not None: params["until"] = until
        return params

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        except (TypeError, ValueError) as exc:
            raise CandleRangeError("invalid candle timestamp") from exc
        if parsed.tzinfo is None: raise CandleRangeError("candle timestamps must include an offset")
        return parsed.astimezone(timezone.utc)


def retry_delay(
    attempt: int,
    *,
    retry_after: str | None,
    jitter: Callable[[], float] = lambda: 0.0,
    cap_seconds: float = 30.0,
) -> float:
    if attempt < 0:
        raise ValueError("attempt는 0 이상이어야 한다")
    if retry_after is not None:
        try:
            value = float(retry_after)
        except ValueError:
            value = -1
        if value >= 0:
            return min(cap_seconds, value)
    return min(cap_seconds, (2**attempt) + max(0.0, jitter()))
