"""DART 지배구조 응답을 문서 계열별 provenance와 함께 보존한다."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Mapping, Protocol, Sequence

from ..provenance import dart_public_url
from ..status_codes import ErrorCode, WarningCode


ANNUAL_REPORT_CODE = "11011"


class OwnershipError(ValueError):
    code = ErrorCode.OWNERSHIP_ERROR


class OwnershipSchemaError(OwnershipError):
    code = ErrorCode.OWNERSHIP_SCHEMA_INVALID


class OwnershipClient(Protocol):
    def get_stock_totals(self, corp_code: str, *, business_year: str, report_code: str) -> Sequence[Mapping[str, Any]]: ...
    def get_major_shareholders(self, corp_code: str, *, business_year: str, report_code: str) -> Sequence[Mapping[str, Any]]: ...
    def get_major_shareholder_changes(self, corp_code: str, *, business_year: str, report_code: str) -> Sequence[Mapping[str, Any]]: ...
    def get_large_holdings(self, corp_code: str) -> Sequence[Mapping[str, Any]]: ...
    def get_insider_holdings(self, corp_code: str) -> Sequence[Mapping[str, Any]]: ...


def _valid_corp_code(corp_code: str) -> None:
    if not isinstance(corp_code, str) or re.fullmatch(r"[0-9]{8}", corp_code) is None:
        raise OwnershipSchemaError("corp_code는 숫자 8자리여야 합니다")


def _receipt_number(value: object, *, tool: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{14}", value) is None:
        raise OwnershipSchemaError(f"{tool} rcept_no는 숫자 14자리여야 합니다")
    return value


def _settlement_date(value: object, *, tool: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise OwnershipSchemaError(f"{tool} stlm_dt는 YYYY-MM-DD여야 합니다")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise OwnershipSchemaError(f"{tool} stlm_dt가 유효한 날짜가 아닙니다") from exc
    return value


def _periodic(rows: Sequence[Mapping[str, Any]], *, corp_code: str, year: str, report_code: str, tool: str) -> Mapping[str, Any]:
    out: list[Mapping[str, Any]] = []
    has_unreadable = False
    for row in rows:
        for key in ("provenance", "unreadable_fields"):
            if key in row:
                raise OwnershipSchemaError(f"{tool} 응답에 서버 예약 필드 {key}가 있습니다")
        if row.get("corp_code") != corp_code:
            raise OwnershipSchemaError(f"{tool} 응답 corp_code가 요청과 다릅니다")
        for key, expected in (("bsns_year", year), ("reprt_code", report_code)):
            if row.get(key) is not None and str(row[key]) != expected:
                raise OwnershipSchemaError(f"{tool} 응답 {key}가 요청과 다릅니다")
        rcept_no = _receipt_number(row.get("rcept_no"), tool=tool)
        stlm_dt = _settlement_date(row.get("stlm_dt"), tool=tool)
        unreadable = tuple(sorted(key for key, value in row.items() if isinstance(value, str) and "\ufffd" in value))
        item = {**row, "provenance": {"document_basis": "periodic_report", "rcept_no": rcept_no, "stlm_dt": stlm_dt, "bsns_year": year, "reprt_code": report_code, "public_url": dart_public_url(rcept_no)}}
        if unreadable:
            has_unreadable = True
            item.update({**dict.fromkeys(unreadable), "unreadable_fields": unreadable})
        out.append(item)
    return {"tool": tool, "document_basis": "periodic_report", "corp_code": corp_code, "bsns_year": year, "reprt_code": report_code, "items": tuple(out), "warnings": (WarningCode.SOURCE_TEXT_UNREADABLE.value,) if has_unreadable else ()}


def get_shares_outstanding(client: OwnershipClient, *, corp_code: str, year: str, reprt_code: str = ANNUAL_REPORT_CODE) -> Mapping[str, Any]:
    _valid_corp_code(corp_code)
    result = _periodic(client.get_stock_totals(corp_code, business_year=year, report_code=reprt_code), corp_code=corp_code, year=year, report_code=reprt_code, tool="get_shares_outstanding")
    for item in result["items"]:
        if not isinstance(item.get("se"), str) and "se" not in tuple(item.get("unreadable_fields") or ()):
            raise OwnershipSchemaError("stockTotqySttus 행에 se가 없습니다")
    return result


def get_major_shareholders(client: OwnershipClient, *, corp_code: str, year: str, reprt_code: str = ANNUAL_REPORT_CODE) -> Mapping[str, Any]:
    _valid_corp_code(corp_code)
    return _periodic(client.get_major_shareholders(corp_code, business_year=year, report_code=reprt_code), corp_code=corp_code, year=year, report_code=reprt_code, tool="get_major_shareholders")


def get_major_shareholder_changes(client: OwnershipClient, *, corp_code: str, year: str, reprt_code: str = ANNUAL_REPORT_CODE) -> Mapping[str, Any]:
    _valid_corp_code(corp_code)
    return _periodic(client.get_major_shareholder_changes(corp_code, business_year=year, report_code=reprt_code), corp_code=corp_code, year=year, report_code=reprt_code, tool="get_major_shareholder_changes")


def _ownership(rows: Sequence[Mapping[str, Any]], *, corp_code: str, tool: str, required: tuple[str, ...]) -> Mapping[str, Any]:
    items: list[Mapping[str, Any]] = []
    has_unreadable = False
    for row in rows:
        for key in ("provenance", "unreadable_fields", "rcept_dt_raw"):
            if key in row:
                raise OwnershipSchemaError(f"{tool} 응답에 서버 예약 필드 {key}가 있습니다")
        rcept_no, raw_date, repror = row.get("rcept_no"), row.get("rcept_dt"), row.get("repror")
        if row.get("corp_code") != corp_code:
            raise OwnershipSchemaError(f"{tool} 응답 corp_code가 요청과 다릅니다")
        rcept_no = _receipt_number(rcept_no, tool=tool)
        if not isinstance(raw_date, str) or re.fullmatch(r"[0-9]{8}|[0-9]{4}-[0-9]{2}-[0-9]{2}", raw_date) is None or not isinstance(repror, str) or not repror.strip():
            raise OwnershipSchemaError(f"{tool} 응답에 ownership_filing provenance가 없습니다")
        try:
            rcept_dt = datetime.strptime(raw_date, "%Y%m%d" if len(raw_date) == 8 else "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise OwnershipSchemaError(f"{tool} rcept_dt 형식이 올바르지 않습니다") from exc
        missing = [key for key in required if key not in row]
        if missing:
            raise OwnershipSchemaError(f"{tool} 전용 필드 누락: {missing}")
        unreadable = tuple(sorted(key for key, value in row.items() if isinstance(value, str) and "\ufffd" in value))
        item = {**row, "rcept_dt_raw": raw_date, "rcept_dt": rcept_dt, "provenance": {"document_basis": "ownership_filing", "rcept_no": rcept_no, "rcept_dt": rcept_dt, "rcept_dt_raw": raw_date, "repror": None if "repror" in unreadable else repror, "public_url": dart_public_url(rcept_no)}}
        if unreadable:
            has_unreadable = True
            item.update({**dict.fromkeys(unreadable), "unreadable_fields": unreadable})
        items.append(item)
    return {"tool": tool, "document_basis": "ownership_filing", "corp_code": corp_code, "items": tuple(items), "warnings": (WarningCode.SOURCE_TEXT_UNREADABLE.value,) if has_unreadable else ()}


def get_large_holdings(client: OwnershipClient, *, corp_code: str) -> Mapping[str, Any]:
    _valid_corp_code(corp_code)
    return _ownership(client.get_large_holdings(corp_code), corp_code=corp_code, tool="get_large_holdings", required=("report_tp", "stkqy", "stkqy_irds", "stkrt", "stkrt_irds", "ctr_stkqy", "ctr_stkrt", "report_resn"))


def get_insider_holdings(client: OwnershipClient, *, corp_code: str) -> Mapping[str, Any]:
    _valid_corp_code(corp_code)
    return _ownership(client.get_insider_holdings(corp_code), corp_code=corp_code, tool="get_insider_holdings", required=("isu_exctv_rgist_at", "isu_exctv_ofcps", "isu_main_shrholdr", "sp_stock_lmp_cnt", "sp_stock_lmp_irds_cnt", "sp_stock_lmp_rate", "sp_stock_lmp_irds_rate"))
