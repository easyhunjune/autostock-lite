"""Return DART full financial statements exactly as disclosed.

Rows come from ``fnlttSinglAcntAll`` unchanged: no account classification, no
sums, ratios or other derived values.  ``fnlttSinglAcntAll`` carries no
settlement date, so the balance-sheet ``thstrm_dt`` is joined from
``fnlttSinglAcnt``.

The join is trusted only when both responses describe the same report:
``rcept_no``, ``bsns_year`` and ``reprt_code`` are required on every row, must
be single-valued within each response, must agree across the two responses,
and must match the request.  ``corp_code`` and ``fs_div`` are checked against
the request wherever DART includes them.  ``stock_code`` is optional; when both
responses carry one, they must agree.  Anything else fails closed.
"""

from __future__ import annotations

from datetime import date
import re
from typing import Any, Mapping, Sequence

from .client import DartNoData
from ..provenance import dart_public_url
from ..status_codes import ErrorCode


REPORT_NAMES = {
    "11011": "사업보고서",
    "11012": "반기보고서",
    "11013": "1분기보고서",
    "11014": "3분기보고서",
}
FS_NAMES = {"CFS": "연결재무제표", "OFS": "별도재무제표"}
UNIT_NOTE = (
    "금액은 DART가 공시한 문자열 그대로이며 각 행 currency의 기본 단위입니다(KRW는 원). "
    "단위 환산이나 합계·비율 같은 파생 계산은 하지 않았습니다."
)

_DATE = re.compile(r"(?P<year>[0-9]{4})[.\-/](?P<month>[0-9]{1,2})[.\-/](?P<day>[0-9]{1,2})")
_ROW_KEYS = frozenset({"sj_div", "sj_nm", "account_id", "account_nm", "account_detail", "currency", "ord"})
_REQUIRED_ROW_KEYS = ("sj_div", "account_nm", "currency")
_PERIOD_PREFIXES = ("thstrm_", "frmtrm_", "bfefrmtrm_")


class StatementsError(ValueError):
    """A disclosed statement cannot be returned safely."""


class StatementsNoData(StatementsError):
    code = ErrorCode.FINANCIALS_NO_DATA


class StatementsIdentityError(StatementsError):
    code = ErrorCode.FINANCIALS_IDENTITY_MISMATCH


class StatementsSchemaError(StatementsError):
    code = ErrorCode.FINANCIALS_SCHEMA_INVALID


class StatementsDateUnavailable(StatementsError):
    code = ErrorCode.FINANCIALS_DATE_UNAVAILABLE


def _rows(value: object, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(row, Mapping) for row in value):
        raise StatementsSchemaError(f"DART {label} 응답 행 형식이 올바르지 않습니다")
    return list(value)


def _text(row: Mapping[str, Any], field: str, *, required: bool) -> str | None:
    if field not in row:
        if required:
            raise StatementsSchemaError(f"DART 행에 {field}가 없습니다")
        return None
    value = row[field]
    if not isinstance(value, str) or not value.strip():
        raise StatementsSchemaError(f"DART 행의 {field}가 유효한 문자열이 아닙니다")
    return value


def _one_value(rows: Sequence[Mapping[str, Any]], field: str, *, required: bool) -> str | None:
    values = {value for row in rows if (value := _text(row, field, required=required)) is not None}
    if len(values) > 1:
        raise StatementsIdentityError(f"DART 응답 안에서 {field} 값이 하나가 아닙니다")
    return next(iter(values), None)


def _stock_code(rows: Sequence[Mapping[str, Any]]) -> str | None:
    """Return the single disclosed stock_code, or None when DART left it out or blank."""

    values: set[str] = set()
    for row in rows:
        if "stock_code" not in row:
            continue
        value = row["stock_code"]
        if not isinstance(value, str):
            raise StatementsSchemaError("DART 행의 stock_code가 문자열이 아닙니다")
        if value.strip():
            values.add(value.strip())
    if len(values) > 1:
        raise StatementsIdentityError("DART 응답 안에서 stock_code 값이 하나가 아닙니다")
    return next(iter(values), None)


def _report_identity(
    rows: Sequence[Mapping[str, Any]], *, corp_code: str, bsns_year: str, reprt_code: str, fs_div: str | None
) -> tuple[str, str | None]:
    receipt = _one_value(rows, "rcept_no", required=True)
    if receipt is None or re.fullmatch(r"[0-9]{14}", receipt) is None:
        raise StatementsSchemaError("DART 접수번호 형식이 올바르지 않습니다")
    for field, expected in (("bsns_year", bsns_year), ("reprt_code", reprt_code)):
        if _one_value(rows, field, required=True) != expected:
            raise StatementsIdentityError(f"DART {field}가 요청과 다릅니다")
    optional = {"corp_code": corp_code}
    if fs_div is not None:
        optional["fs_div"] = fs_div
    for field, expected in optional.items():
        disclosed = _one_value(rows, field, required=False)
        if disclosed is not None and disclosed != expected:
            raise StatementsIdentityError(f"DART {field}가 요청과 다릅니다")
    return receipt, _stock_code(rows)


def _single_date(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    matches = list(_DATE.finditer(raw))
    if len(matches) != 1:
        return None
    match = matches[0]
    try:
        return date(int(match.group("year")), int(match.group("month")), int(match.group("day"))).isoformat()
    except ValueError:
        return None


def get_financial_statements(
    client: Any, *, corp_code: str, bsns_year: str, reprt_code: str = "11011", fs_div: str = "CFS"
) -> dict[str, Any]:
    if not isinstance(corp_code, str) or re.fullmatch(r"[0-9]{8}", corp_code) is None:
        raise ValueError("corp_code는 숫자 8자리여야 합니다")
    if not isinstance(bsns_year, str) or re.fullmatch(r"[0-9]{4}", bsns_year) is None:
        raise ValueError("bsns_year는 숫자 4자리여야 합니다")
    if reprt_code not in REPORT_NAMES:
        raise ValueError("reprt_code가 지원되는 보고서 코드가 아닙니다")
    if fs_div not in FS_NAMES:
        raise ValueError("fs_div는 CFS 또는 OFS여야 합니다")
    request = {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code}

    try:
        raw_rows = client.get_financials_all(corp_code, business_year=bsns_year, report_code=reprt_code, fs_div=fs_div)
    except DartNoData:
        raise StatementsNoData("DART 전체 재무제표가 없습니다") from None
    all_rows = _rows(raw_rows, "전체 재무제표")
    if not all_rows:
        raise StatementsNoData("DART 전체 재무제표가 없습니다")
    receipt, all_stock = _report_identity(all_rows, **request, fs_div=fs_div)

    try:
        raw_dates = client.get_financials_single(corp_code, business_year=bsns_year, report_code=reprt_code)
    except DartNoData:
        raise StatementsDateUnavailable("DART 결산일 응답이 없습니다") from None
    date_rows = _rows(raw_dates, "주요계정")
    if not date_rows:
        raise StatementsDateUnavailable("DART 결산일 응답이 비어 있습니다")
    date_receipt, date_stock = _report_identity(date_rows, **request, fs_div=None)
    if date_receipt != receipt:
        raise StatementsIdentityError("두 DART 응답의 접수번호가 다릅니다")
    if all_stock is not None and date_stock is not None and all_stock != date_stock:
        raise StatementsIdentityError("두 DART 응답의 종목코드가 다릅니다")

    raw_dates_seen: set[str] = set()
    iso_dates: set[str] = set()
    for row in date_rows:
        if _text(row, "fs_div", required=True) != fs_div or row.get("sj_div") != "BS":
            continue
        raw = row.get("thstrm_dt")
        parsed = _single_date(raw)
        if parsed is None:
            raise StatementsDateUnavailable("DART 재무상태표 결산일을 해석할 수 없습니다")
        raw_dates_seen.add(raw)
        iso_dates.add(parsed)
    if len(iso_dates) != 1:
        raise StatementsDateUnavailable("요청한 재무제표의 재무상태표 결산일이 없거나 서로 다릅니다")
    statement_date = next(iter(iso_dates))
    raw_date = sorted(raw_dates_seen)[0]

    rows = []
    for row in all_rows:
        for field in _REQUIRED_ROW_KEYS:
            _text(row, field, required=True)
        disclosed: dict[str, str] = {}
        for key, value in row.items():
            if isinstance(key, str) and (key in _ROW_KEYS or key.startswith(_PERIOD_PREFIXES)):
                if not isinstance(value, str):
                    raise StatementsSchemaError(f"DART 공시 필드 {key}가 문자열이 아닙니다")
                disclosed[key] = value
        rows.append(disclosed)

    return {
        "corp_code": corp_code,
        "bsns_year": bsns_year,
        "reprt_code": reprt_code,
        "report_name": REPORT_NAMES[reprt_code],
        "fs_div": fs_div,
        "fs_name": FS_NAMES[fs_div],
        "rcept_no": receipt,
        "statement_date": statement_date,
        "statement_date_raw": raw_date,
        "unit_note": UNIT_NOTE,
        "rows": rows,
        "provenance": [
            {"endpoint": "fnlttSinglAcntAll.json", "rcept_no": receipt, "public_url": dart_public_url(receipt)},
            {"endpoint": "fnlttSinglAcnt.json", "rcept_no": receipt, "sj_div": "BS", "raw_field": "thstrm_dt", "raw_value": raw_date},
        ],
    }


__all__ = [
    "FS_NAMES",
    "REPORT_NAMES",
    "UNIT_NOTE",
    "get_financial_statements",
    "StatementsError",
    "StatementsNoData",
    "StatementsIdentityError",
    "StatementsSchemaError",
    "StatementsDateUnavailable",
]
