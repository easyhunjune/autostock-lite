"""DART OpenAPI의 최소 읽기 전용 실환경 클라이언트."""

from __future__ import annotations

from datetime import date
import re
from typing import Any, Mapping

import httpx

from ..status_codes import ErrorCode


DART_API_BASE = "https://opendart.fss.or.kr/api"


class DartLiveError(RuntimeError):
    """요청 URL과 인증키를 노출하지 않는 DART 오류. 연결·HTTP·미정의 상태."""

    code = ErrorCode.DART_REQUEST_FAILED


class DartNoData(DartLiveError):
    """조회 조건에 맞는 데이터가 없다 (업무 상태 013, 014).

    "아직 공시되지 않은 보고서"를 인증 오류·서버 오류와 구분해야 한다.
    뭉뚱그리면 키가 틀린 것도 "없음"으로 처리돼 조용히 잘못된 결론이 난다.
    """

    code = ErrorCode.DART_NO_DATA


class DartAuthError(DartLiveError):
    """키 미등록·사용 중지·허용되지 않은 IP·보유기간 만료 (010, 011, 012, 901)."""

    code = ErrorCode.DART_AUTH_FAILED


class DartRateLimited(DartLiveError):
    """요청 제한 초과 (020)."""

    code = ErrorCode.DART_RATE_LIMITED


class DartRequestRejected(DartLiveError):
    """요청 값이나 접근 방식이 거부됨 (021, 100, 101)."""

    code = ErrorCode.DART_REQUEST_INVALID


class DartMaintenance(DartLiveError):
    """시스템 점검으로 서비스 중지 (800)."""

    code = ErrorCode.DART_MAINTENANCE


class DartResponseError(DartLiveError):
    """응답이 왔지만 형식이나 계약이 기대와 다르다."""

    code = ErrorCode.DART_RESPONSE_INVALID


_STATUS_ERRORS: dict[str, tuple[type[DartLiveError], str]] = {
    "010": (DartAuthError, "등록되지 않은 DART 키입니다"),
    "011": (DartAuthError, "사용할 수 없는 DART 키입니다"),
    "012": (DartAuthError, "DART에 접근할 수 없는 IP입니다"),
    "901": (DartAuthError, "보유기간이 만료되어 사용할 수 없는 DART 키입니다"),
    "013": (DartNoData, "DART 조회 조건에 맞는 데이터가 없습니다"),
    "014": (DartNoData, "DART 파일이 존재하지 않습니다"),
    "020": (DartRateLimited, "DART 요청 제한을 초과했습니다"),
    "021": (DartRequestRejected, "DART 조회 가능한 회사 개수를 초과했습니다"),
    "100": (DartRequestRejected, "DART 요청 필드 값이 부적절합니다"),
    "101": (DartRequestRejected, "DART 접근 방식이 부적절합니다"),
    "800": (DartMaintenance, "DART 시스템 점검 중입니다"),
}


_BODY_STATUS = re.compile(rb'<status>\s*([0-9]{3})\s*</status>|"status"\s*:\s*"([0-9]{3})"')


def raise_for_status_body(body: bytes) -> None:
    """ZIP 대신 온 짧은 오류 본문(XML 또는 JSON)에서 DART 업무 상태를 읽어 해당 오류를 낸다."""

    match = _BODY_STATUS.search(body[:4096])
    if match is None:
        return
    status = (match.group(1) or match.group(2)).decode("ascii")
    if status in _STATUS_ERRORS:
        error, message = _STATUS_ERRORS[status]
        raise error(f"{message} (status={status})")
    if status != "000":
        raise DartLiveError(f"DART API 업무 오류: status={status}")


class DartReadonlyClient:
    def __init__(self, api_key: str, *, http_client: httpx.Client) -> None:
        if not api_key:
            raise ValueError("DART API 키가 필요하다")
        self._api_key = api_key
        self._http = http_client

    def get_company(self, corp_code: str) -> Mapping[str, Any]:
        if len(corp_code) != 8 or not corp_code.isdigit():
            raise ValueError("corp_code는 숫자 8자리여야 한다")
        payload = self._get_json("company.json", {"corp_code": corp_code})
        if payload.get("corp_code") != corp_code:
            raise DartResponseError("DART 회사 응답의 corp_code가 요청과 다릅니다")
        return payload

    def get_financials_all(
        self, corp_code: str, *, business_year: str, report_code: str, fs_div: str = "CFS"
    ) -> list[Mapping[str, Any]]:
        payload = self._get_json(
            "fnlttSinglAcntAll.json",
            {
                "corp_code": corp_code,
                "bsns_year": business_year,
                "reprt_code": report_code,
                "fs_div": fs_div,
            },
        )
        rows = payload.get("list")
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            raise DartResponseError("DART 전체 재무제표 응답 list 형식이 올바르지 않습니다")
        return rows

    def get_financials_single(
        self, corp_code: str, *, business_year: str, report_code: str
    ) -> list[Mapping[str, Any]]:
        """Read the major-account response used only for its original dates.

        ``fnlttSinglAcntAll`` does not expose ``thstrm_dt``.  The financial
        pipeline joins this response by receipt/report identity and selects the
        requested ``fs_div`` itself; no settlement date is synthesized here.
        """

        payload = self._get_json(
            "fnlttSinglAcnt.json",
            {
                "corp_code": corp_code,
                "bsns_year": business_year,
                "reprt_code": report_code,
            },
        )
        rows = payload.get("list")
        if not isinstance(rows, list) or not all(
            isinstance(row, Mapping) for row in rows
        ):
            raise DartResponseError("DART 주요계정 응답 list 형식이 올바르지 않습니다")
        return rows

    def get_stock_totals(
        self, corp_code: str, *, business_year: str, report_code: str
    ) -> list[Mapping[str, Any]]:
        payload = self._get_json(
            "stockTotqySttus.json",
            {
                "corp_code": corp_code,
                "bsns_year": business_year,
                "reprt_code": report_code,
            },
        )
        rows = payload.get("list")
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            raise DartResponseError("DART 주식 총수 응답 list 형식이 올바르지 않습니다")
        return rows

    def get_major_shareholders(
        self, corp_code: str, *, business_year: str, report_code: str
    ) -> list[Mapping[str, Any]]:
        return self._get_list(
            "hyslrSttus.json",
            {
                "corp_code": corp_code,
                "bsns_year": business_year,
                "reprt_code": report_code,
            },
        )

    def get_major_shareholder_changes(
        self, corp_code: str, *, business_year: str, report_code: str
    ) -> list[Mapping[str, Any]]:
        return self._get_list(
            "hyslrChgSttus.json",
            {
                "corp_code": corp_code,
                "bsns_year": business_year,
                "reprt_code": report_code,
            },
        )

    def get_large_holdings(self, corp_code: str) -> list[Mapping[str, Any]]:
        return self._get_list("majorstock.json", {"corp_code": corp_code})

    def get_insider_holdings(self, corp_code: str) -> list[Mapping[str, Any]]:
        return self._get_list("elestock.json", {"corp_code": corp_code})

    def get_document(self, rcept_no: str) -> bytes:
        """Download the immutable original filing archive for one receipt."""

        if len(rcept_no) != 14 or not rcept_no.isdigit():
            raise ValueError("rcept_no must contain exactly 14 digits")
        try:
            response = self._http.get(
                f"{DART_API_BASE}/document.xml",
                params={"crtfc_key": self._api_key, "rcept_no": rcept_no},
                headers={"Accept": "application/zip"},
            )
        except httpx.RequestError as exc:
            raise DartLiveError("DART filing document connection failed") from exc
        if response.status_code != 200:
            raise DartLiveError(
                f"DART filing document request failed: HTTP {response.status_code}"
            )
        payload = response.content
        if not payload or len(payload) > 20_000_000:
            raise DartResponseError("DART filing document size is outside the safe range")
        return payload

    def list_disclosures(
        self,
        corp_code: str,
        *,
        begin_date: date,
        end_date: date,
        disclosure_type: str | None = None,
    ) -> list[Mapping[str, Any]]:
        """회사 한 곳의 공시를 기간 전체에 걸쳐 빠짐없이 읽는다.

        OpenDART ``list.json``은 한 페이지에 최대 100건만 반환한다. 첫 페이지만
        보고 "관련 공시 없음"으로 판정하면 거짓 음성이 되므로 모든 페이지와
        ``total_count``를 대조한다. 정정공시도 사건 추적에 필요해
        ``last_reprt_at=N``을 명시한다.
        """

        if len(corp_code) != 8 or not corp_code.isdigit():
            raise ValueError("corp_code는 숫자 8자리여야 한다")
        if disclosure_type is not None and disclosure_type not in set("ABCDEFGHIJ"):
            raise ValueError("DART 공시유형은 공식 대분류 코드 A~J여야 합니다")
        if begin_date > end_date:
            return []

        common = {
            "corp_code": corp_code,
            "bgn_de": begin_date.strftime("%Y%m%d"),
            "end_de": end_date.strftime("%Y%m%d"),
            "last_reprt_at": "N",
            "page_count": "100",
        }
        if disclosure_type is not None:
            common["pblntf_ty"] = disclosure_type
        rows: list[Mapping[str, Any]] = []
        expected_total: int | None = None
        expected_pages: int | None = None
        page_no = 1
        while True:
            try:
                payload = self._get_json(
                    "list.json", {**common, "page_no": str(page_no)}
                )
            except DartNoData:
                if page_no == 1:
                    return []
                raise DartResponseError("DART 공시검색 페이지 도중 데이터가 사라졌습니다")

            try:
                actual_page = int(payload["page_no"])
                actual_page_count = int(payload["page_count"])
                total_count = int(payload["total_count"])
                total_page = int(payload["total_page"])
            except (KeyError, TypeError, ValueError) as exc:
                raise DartResponseError("DART 공시검색 페이지 정보가 올바르지 않습니다") from exc
            if actual_page != page_no or actual_page_count != 100:
                raise DartResponseError("DART 공시검색 페이지 계약이 요청과 다릅니다")
            if total_count < 0 or total_page < 1 or total_page > 100:
                raise DartResponseError("DART 공시검색 전체 건수/페이지가 안전 범위를 벗어납니다")
            if expected_total is None:
                expected_total, expected_pages = total_count, total_page
            elif (total_count, total_page) != (expected_total, expected_pages):
                raise DartResponseError("DART 공시검색 중 전체 건수/페이지가 바뀌었습니다")

            page_rows = payload.get("list")
            if not isinstance(page_rows, list) or not all(
                isinstance(row, Mapping) for row in page_rows
            ):
                raise DartResponseError("DART 공시검색 응답 list 형식이 올바르지 않습니다")
            for row in page_rows:
                receipt_no = row.get("rcept_no")
                receipt_date = row.get("rcept_dt")
                report_name = row.get("report_nm")
                if not (
                    isinstance(receipt_no, str)
                    and len(receipt_no) == 14
                    and receipt_no.isdigit()
                    and isinstance(receipt_date, str)
                    and len(receipt_date) == 8
                    and receipt_date.isdigit()
                    and isinstance(report_name, str)
                    and report_name.strip()
                ):
                    raise DartResponseError("DART 공시검색 핵심 필드를 읽을 수 없습니다")
                try:
                    received = date(
                        int(receipt_date[:4]),
                        int(receipt_date[4:6]),
                        int(receipt_date[6:]),
                    )
                except ValueError as exc:
                    raise DartResponseError("DART 공시검색 접수일자가 유효하지 않습니다") from exc
                if not begin_date <= received <= end_date:
                    raise DartResponseError("DART 공시검색 응답 일자가 요청 범위를 벗어났습니다")
                rows.append(row)

            if page_no >= total_page:
                break
            page_no += 1

        if expected_total is None or len(rows) != expected_total:
            raise DartResponseError("DART 공시검색 전체 건수와 수집 행 수가 다릅니다")
        receipt_nos = [str(row["rcept_no"]) for row in rows]
        if len(receipt_nos) != len(set(receipt_nos)):
            raise DartResponseError("DART 공시검색에 중복 접수번호가 있습니다")
        return rows

    def _get_json(self, endpoint: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        allowed = {
            "company.json",
            "fnlttSinglAcntAll.json",
            "fnlttSinglAcnt.json",
            "stockTotqySttus.json",
            "hyslrSttus.json",
            "hyslrChgSttus.json",
            "majorstock.json",
            "elestock.json",
            "list.json",
        }
        if endpoint not in allowed:
            raise ValueError("허용되지 않은 DART 읽기 엔드포인트")
        query = {"crtfc_key": self._api_key, **params}
        try:
            response = self._http.get(
                f"{DART_API_BASE}/{endpoint}",
                params=query,
                headers={"Accept": "application/json"},
            )
        except httpx.RequestError as exc:
            raise DartLiveError("DART API에 연결하지 못했습니다") from exc
        if response.status_code != 200:
            raise DartLiveError(f"DART API 응답 실패: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DartResponseError("DART API 응답이 JSON이 아닙니다") from exc
        if not isinstance(payload, Mapping):
            raise DartResponseError("DART API 응답 최상위 값이 객체가 아닙니다")
        status = payload.get("status")
        if isinstance(status, str) and status in _STATUS_ERRORS:
            error, message = _STATUS_ERRORS[status]
            raise error(f"{message} (status={status})")
        if status != "000":
            safe_status = status if isinstance(status, str) else "unknown"
            raise DartLiveError(f"DART API 업무 오류: status={safe_status}")
        return payload

    def _get_list(
        self, endpoint: str, params: Mapping[str, str]
    ) -> list[Mapping[str, Any]]:
        payload = self._get_json(endpoint, params)
        rows = payload.get("list")
        if not isinstance(rows, list) or not all(
            isinstance(row, Mapping) for row in rows
        ):
            raise DartResponseError(f"DART {endpoint} response has an invalid list")
        return rows
