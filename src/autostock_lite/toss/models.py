"""토스 응답을 엄격하게 검증하는 최소 모델."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping

from ..status_codes import ErrorCode


class StrictModelError(ValueError):
    """토스 응답이 기대한 계약과 다르다."""

    code = ErrorCode.TOSS_RESPONSE_INVALID


def _strict_keys(
    payload: Mapping[str, Any], *, required: set[str], allowed: set[str], model: str
) -> None:
    actual = set(payload)
    if not required <= actual or not actual <= allowed:
        missing = sorted(required - actual)
        extra = sorted(actual - allowed)
        raise StrictModelError(f"{model} 필드 불일치: missing={missing}, extra={extra}")


def _decimal_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise StrictModelError(f"{field}는 문자열 decimal이어야 한다")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise StrictModelError(f"{field}를 Decimal로 파싱할 수 없다") from exc
    if not parsed.is_finite():
        raise StrictModelError(f"{field}는 유한한 decimal이어야 한다")
    return value


def _timestamp(value: object, field: str, *, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise StrictModelError(f"{field}는 ISO 문자열이어야 한다")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise StrictModelError(f"{field}가 ISO 8601 형식이 아니다") from exc
    return value


def _date(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StrictModelError(f"{field} must be an ISO date or null")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise StrictModelError(f"{field} must be an ISO date") from exc
    return value


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StrictModelError(f"{field} must be an object")
    return value


class StockStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    DELISTED = "DELISTED"


class SecurityType(str, Enum):
    STOCK = "STOCK"
    FOREIGN_STOCK = "FOREIGN_STOCK"
    DEPOSITARY_RECEIPT = "DEPOSITARY_RECEIPT"
    INFRASTRUCTURE_FUND = "INFRASTRUCTURE_FUND"
    REIT = "REIT"
    ETF = "ETF"
    FOREIGN_ETF = "FOREIGN_ETF"
    ETN = "ETN"
    STOCK_WARRANTS = "STOCK_WARRANTS"


@dataclass(frozen=True)
class KoreanMarketDetail:
    liquidationTrading: bool
    krxTradingSuspended: bool
    nxtSupported: bool
    nxtTradingSuspended: bool | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "KoreanMarketDetail":
        required = {"liquidationTrading", "krxTradingSuspended", "nxtSupported", "nxtTradingSuspended"}
        _strict_keys(payload, required=required, allowed=required, model=cls.__name__)
        if not all(isinstance(payload[key], bool) for key in {"liquidationTrading", "krxTradingSuspended", "nxtSupported"}):
            raise StrictModelError("KoreanMarketDetail fields must be booleans")
        if payload["nxtTradingSuspended"] is not None and not isinstance(payload["nxtTradingSuspended"], bool):
            raise StrictModelError("nxtTradingSuspended must be a boolean or null")
        return cls(**{key: payload[key] for key in required})


@dataclass(frozen=True)
class StockInfo:
    symbol: str
    name: str
    englishName: str
    isinCode: str
    market: str
    securityType: SecurityType
    isCommonShare: bool
    status: StockStatus
    currency: str
    sharesOutstanding: str
    listDate: str | None = None
    delistDate: str | None = None
    koreanMarketDetail: KoreanMarketDetail | None = None

    REQUIRED_FIELDS = frozenset(
        {
            "symbol",
            "name",
            "englishName",
            "isinCode",
            "market",
            "securityType",
            "isCommonShare",
            "status",
            "currency",
            "sharesOutstanding",
        }
    )
    FIELDS = REQUIRED_FIELDS | frozenset(
        {"listDate", "delistDate", "leverageFactor", "koreanMarketDetail"}
    )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StockInfo":
        _strict_keys(
            payload,
            required=set(cls.REQUIRED_FIELDS),
            allowed=set(cls.FIELDS),
            model=cls.__name__,
        )
        symbol = payload["symbol"]
        name = payload["name"]
        isin = payload["isinCode"]
        common = payload["isCommonShare"]
        currency = payload["currency"]
        english_name = payload["englishName"]
        market = payload["market"]
        if not all(isinstance(value, str) and value for value in (symbol, name, isin, english_name, market)):
            raise StrictModelError("StockInfo 식별 필드는 비어 있지 않은 문자열이어야 한다")
        if not isinstance(common, bool):
            raise StrictModelError("isCommonShare는 boolean이어야 한다")
        if not isinstance(currency, str) or len(currency) != 3:
            raise StrictModelError("currency는 세 자리 문자열이어야 한다")
        shares = _decimal_string(payload["sharesOutstanding"], "sharesOutstanding")
        if Decimal(shares) <= 0 or Decimal(shares) != Decimal(shares).to_integral_value():
            raise StrictModelError("sharesOutstanding은 양의 정수 문자열이어야 한다")
        try:
            security_type = SecurityType(payload["securityType"])
            status = StockStatus(payload["status"])
        except (ValueError, TypeError) as exc:
            raise StrictModelError("StockInfo enum 값이 내부 계약에 없습니다") from exc
        return cls(
            symbol=symbol,
            name=name,
            englishName=english_name,
            isinCode=isin,
            market=market,
            securityType=security_type,
            isCommonShare=common,
            status=status,
            currency=currency,
            sharesOutstanding=shares,
            listDate=_date(payload.get("listDate"), "listDate"),
            delistDate=_date(payload.get("delistDate"), "delistDate"),
            koreanMarketDetail=(
                None if payload.get("koreanMarketDetail") is None
                else KoreanMarketDetail.from_dict(_mapping(payload["koreanMarketDetail"], "koreanMarketDetail"))
            ),
        )


@dataclass(frozen=True)
class PriceResponse:
    symbol: str
    timestamp: str | None
    lastPrice: str
    currency: str

    FIELDS = frozenset({"symbol", "timestamp", "lastPrice", "currency"})
    REQUIRED_FIELDS = frozenset({"symbol", "lastPrice", "currency"})

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PriceResponse":
        _strict_keys(
            payload,
            required=set(cls.REQUIRED_FIELDS),
            allowed=set(cls.FIELDS),
            model=cls.__name__,
        )
        symbol = payload["symbol"]
        currency = payload["currency"]
        if not isinstance(symbol, str) or not symbol:
            raise StrictModelError("symbol은 비어 있지 않은 문자열이어야 한다")
        if not isinstance(currency, str) or len(currency) != 3:
            raise StrictModelError("currency는 세 자리 문자열이어야 한다")
        return cls(
            symbol=symbol,
            timestamp=_timestamp(payload.get("timestamp"), "timestamp", nullable=True),
            lastPrice=_decimal_string(payload["lastPrice"], "lastPrice"),
            currency=currency,
        )


@dataclass(frozen=True)
class Candle:
    timestamp: str
    openPrice: str
    highPrice: str
    lowPrice: str
    closePrice: str
    volume: str
    currency: str

    FIELDS = frozenset(
        {
            "timestamp",
            "openPrice",
            "highPrice",
            "lowPrice",
            "closePrice",
            "volume",
            "currency",
        }
    )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Candle":
        _strict_keys(
            payload,
            required=set(cls.FIELDS),
            allowed=set(cls.FIELDS),
            model=cls.__name__,
        )
        currency = payload["currency"]
        if not isinstance(currency, str) or len(currency) != 3:
            raise StrictModelError("currency는 세 자리 문자열이어야 한다")
        return cls(
            timestamp=_timestamp(payload["timestamp"], "timestamp", nullable=False) or "",
            openPrice=_decimal_string(payload["openPrice"], "openPrice"),
            highPrice=_decimal_string(payload["highPrice"], "highPrice"),
            lowPrice=_decimal_string(payload["lowPrice"], "lowPrice"),
            closePrice=_decimal_string(payload["closePrice"], "closePrice"),
            volume=_decimal_string(payload["volume"], "volume"),
            currency=currency,
        )
