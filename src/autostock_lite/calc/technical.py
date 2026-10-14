"""Deterministic technical indicators over adjusted daily candles."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import re
from typing import Sequence

from ..toss.models import Candle
from ..status_codes import ErrorCode


TECHNICAL_FORMULA_VERSION = "1.0"
TECHNICAL_WINDOW = 252
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class TechnicalInputError(ValueError):
    """Candle input violates the adjusted-daily-series contract."""

    code = ErrorCode.TECHNICAL_INPUT_INVALID


class TechnicalInsufficientHistory(TechnicalInputError):
    code = ErrorCode.TECHNICAL_INSUFFICIENT_HISTORY


def _parse_timestamp(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise TechnicalInputError("candle timestamp must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise TechnicalInputError("candle timestamp must include an offset")
    return parsed


def _number(value: object, *, field: str) -> Decimal:
    if not isinstance(value, str):
        raise TechnicalInputError(f"{field} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise TechnicalInputError(f"{field} must be a decimal string") from exc
    if not parsed.is_finite():
        raise TechnicalInputError(f"{field} must be finite")
    return parsed


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _sma(values: Sequence[Decimal], days: int) -> Decimal:
    return sum(values[-days:], Decimal(0)) / Decimal(days)


def _wilder_rsi(closes: Sequence[Decimal], period: int = 14) -> Decimal:
    changes = [current - previous for previous, current in zip(closes, closes[1:])]
    gains = [max(change, Decimal(0)) for change in changes]
    losses = [max(-change, Decimal(0)) for change in changes]
    average_gain = sum(gains[:period], Decimal(0)) / Decimal(period)
    average_loss = sum(losses[:period], Decimal(0)) / Decimal(period)
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = (average_gain * Decimal(period - 1) + gain) / Decimal(period)
        average_loss = (average_loss * Decimal(period - 1) + loss) / Decimal(period)
    if average_gain == 0 and average_loss == 0:
        return Decimal(50)
    if average_loss == 0:
        return Decimal(100)
    relative_strength = average_gain / average_loss
    return Decimal(100) - Decimal(100) / (Decimal(1) + relative_strength)


def _volume_z_score(volumes: Sequence[Decimal]) -> Decimal | None:
    window = volumes[-60:]
    mean = sum(window, Decimal(0)) / Decimal(60)
    with localcontext() as context:
        context.prec = 50
        variance = sum(((value - mean) ** 2 for value in window), Decimal(0)) / Decimal(59)
        if variance == 0:
            return None
        return (window[-1] - mean) / variance.sqrt()


def technical_snapshot(
    candles: Sequence[Candle], *, adjusted: bool
) -> dict[str, object]:
    """Calculate formula 1.0 indicators from the trailing 252 adjusted candles.

    Inputs must be strictly oldest-to-newest, contain one timezone-aware candle
    per trading date, and share a currency.  More than 252 observations are
    accepted but only the trailing fixed window participates in any indicator.
    """

    if adjusted is not True:
        raise TechnicalInputError("technical indicators require adjusted=true candles")
    if len(candles) < TECHNICAL_WINDOW:
        raise TechnicalInsufficientHistory("252 adjusted daily candles are required")
    if any(not isinstance(candle, Candle) for candle in candles):
        raise TechnicalInputError("candles must be strict Candle models")

    timestamps = [_parse_timestamp(candle.timestamp) for candle in candles]
    if any(current <= previous for previous, current in zip(timestamps, timestamps[1:])):
        raise TechnicalInputError("candles must be unique and strictly chronological")
    if len({timestamp.date() for timestamp in timestamps}) != len(timestamps):
        raise TechnicalInputError("only one daily candle per date is allowed")
    currencies = {candle.currency for candle in candles}
    if len(currencies) != 1 or not _CURRENCY.fullmatch(next(iter(currencies), "")):
        raise TechnicalInputError("candle currencies must match")

    parsed: list[tuple[Decimal, Decimal, Decimal, Decimal, Decimal]] = []
    for candle in candles:
        open_price = _number(candle.openPrice, field="openPrice")
        high = _number(candle.highPrice, field="highPrice")
        low = _number(candle.lowPrice, field="lowPrice")
        close = _number(candle.closePrice, field="closePrice")
        volume = _number(candle.volume, field="volume")
        if min(open_price, high, low, close) <= 0:
            raise TechnicalInputError("OHLC prices must be positive")
        if volume < 0:
            raise TechnicalInputError("volume must be nonnegative")
        if high < max(open_price, low, close) or low > min(open_price, high, close):
            raise TechnicalInputError("candle OHLC bounds are inconsistent")
        parsed.append((open_price, high, low, close, volume))

    fixed = parsed[-TECHNICAL_WINDOW:]
    closes = [row[3] for row in fixed]
    highs = [row[1] for row in fixed]
    lows = [row[2] for row in fixed]
    volumes = [row[4] for row in fixed]
    with localcontext() as context:
        # Formula 1.0 fixes intermediate precision so results do not inherit an
        # embedding process's mutable Decimal context.
        context.prec = 50
        low_252, high_252 = min(lows), max(highs)
        position = (
            None
            if high_252 == low_252
            else (closes[-1] - low_252) / (high_252 - low_252) * Decimal(100)
        )
        volume_z = _volume_z_score(volumes)
        return {
            "formula_version": TECHNICAL_FORMULA_VERSION,
            "adjusted": True,
            "currency": next(iter(currencies)),
            "as_of": candles[-1].timestamp,
            "window_trading_days": TECHNICAL_WINDOW,
            "sma20": _decimal_text(_sma(closes, 20)),
            "sma60": _decimal_text(_sma(closes, 60)),
            "sma120": _decimal_text(_sma(closes, 120)),
            "rsi14": _decimal_text(_wilder_rsi(closes)),
            "position_252d_pct": None if position is None else _decimal_text(position),
            "volume_zscore_60": None if volume_z is None else _decimal_text(volume_z),
        }
