"""Deterministic growth metrics for caller-supplied series."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import re
from typing import Sequence

from ..status_codes import ErrorCode


GROWTH_FORMULA_VERSION = "1.1"
_YEAR = re.compile(r"^\d{4}$")


class GrowthInputError(ValueError):
    """The public growth-series boundary is malformed or ambiguous."""

    code = ErrorCode.GROWTH_INPUT_INVALID


def _decimal(value: object, *, field: str) -> Decimal:
    if not isinstance(value, str):
        raise GrowthInputError(f"{field} values must be decimal strings")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise GrowthInputError(f"{field} contains an invalid decimal") from exc
    if not parsed.is_finite():
        raise GrowthInputError(f"{field} values must be finite")
    return parsed


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _unavailable(reason: str) -> dict[str, str]:
    return {"status": "unavailable", "reason": reason}


def _cagr(
    values_by_year: dict[int, Decimal], *, latest_year: int, years: int
) -> dict[str, str]:
    first_year = latest_year - years
    required = set(range(first_year, latest_year + 1))
    if not required <= values_by_year.keys():
        return _unavailable("insufficient-contiguous-history")
    first = values_by_year[first_year]
    last = values_by_year[latest_year]
    if first <= 0:
        return _unavailable("nonpositive-start-value")
    if last < 0:
        return _unavailable("negative-end-value")
    with localcontext() as context:
        context.prec = 50
        rate = (last / first) ** (Decimal(1) / Decimal(years)) - Decimal(1)
    return {
        "status": "ok",
        "value": _decimal_text(rate),
        "from_period": str(first_year),
        "to_period": str(latest_year),
    }


def growth_metrics(
    series: Sequence[str], periods: Sequence[str]
) -> dict[str, object]:
    """Return YoY and latest 5/10-year CAGR as decimal *rates*.

    Periods are four-digit business years in strictly increasing order.  Gaps do
    not invalidate the whole response, but every metric whose required interval
    is incomplete is explicitly unavailable.  Values are decimal strings; no
    float conversion or implicit rounding occurs.

    YoY is a percentage change only against a positive previous value.  A zero
    or negative base gives no number (``nonpositive-previous-value``): dividing
    by a negative base flips the sign, so -100 -> -200 would read as +100%.
    A sign change between consecutive years is labelled in ``sign_change``
    (``negative-to-positive`` or ``positive-to-negative``); the number is kept
    only when the base is positive.
    """

    if len(series) != len(periods) or not series:
        raise GrowthInputError("series and periods must be non-empty and equal length")
    if any(not isinstance(period, str) or not _YEAR.fullmatch(period) for period in periods):
        raise GrowthInputError("periods must be four-digit year strings")
    years = [int(period) for period in periods]
    if any(current <= previous for previous, current in zip(years, years[1:])):
        raise GrowthInputError("periods must be unique and strictly increasing")
    values = [_decimal(value, field="series") for value in series]
    values_by_year = dict(zip(years, values))

    yoy: list[dict[str, str]] = []
    for index in range(1, len(values)):
        previous_year, current_year = years[index - 1], years[index]
        previous, current = values[index - 1], values[index]
        identity = {
            "from_period": str(previous_year),
            "to_period": str(current_year),
        }
        if current_year != previous_year + 1:
            entry = {**identity, **_unavailable("non-consecutive-periods")}
        elif previous <= 0:
            entry = {**identity, **_unavailable("nonpositive-previous-value")}
        else:
            with localcontext() as context:
                context.prec = 50
                rate = current / previous - Decimal(1)
            entry = {**identity, "status": "ok", "value": _decimal_text(rate)}
        if current_year == previous_year + 1:
            if previous < 0 < current:
                entry["sign_change"] = "negative-to-positive"
            elif current < 0 < previous:
                entry["sign_change"] = "positive-to-negative"
        yoy.append(entry)

    latest_year = years[-1]
    return {
        "formula_version": GROWTH_FORMULA_VERSION,
        "periods": list(periods),
        "yoy": yoy,
        "cagr_5y": _cagr(values_by_year, latest_year=latest_year, years=5),
        "cagr_10y": _cagr(values_by_year, latest_year=latest_year, years=10),
    }
