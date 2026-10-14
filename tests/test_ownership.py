"""지배구조 도구(주식 총수, 최대주주, 최대주주 변동, 대량보유, 임원·주요주주)를 합성 응답으로 검증한다.

회사 고유번호, 접수번호(2099년), 주주명, 수량은 모두 지어낸 값이다.
"""

import asyncio
import copy
import unittest

import httpx

from autostock_lite.dart.ownership import (
    OwnershipSchemaError,
    get_insider_holdings,
    get_large_holdings,
    get_major_shareholder_changes,
    get_major_shareholders,
    get_shares_outstanding,
)
from autostock_lite.mcp_runtime import create_live_runtime
from autostock_lite.mcp_server import ToolArgumentsError
from autostock_lite.provenance import dart_public_url
from test_smoke import DART_KEY, IsolatedCase


CORP = "99000001"
RCEPT = "20990331000001"
YEAR = "2098"


def call(service, name, arguments):
    return asyncio.run(service.call(name, arguments))


def periodic_row(**fields):
    return {"rcept_no": RCEPT, "corp_cls": "Y", "corp_code": CORP, "corp_name": "가상회사", "stlm_dt": "2098-12-31", **fields}


def totals_row(**changes):
    return {**periodic_row(se="보통주", isu_stock_totqy="1,000", istc_totqy="1,000", tesstk_co="0", distb_stock_co="1,000"), **changes}


def shareholder_row(**changes):
    return {**periodic_row(nm="가상주주", relate="본인", stock_knd="보통주", bsis_posesn_stock_co="1,000",
                           trmend_posesn_stock_co="1,100", trmend_posesn_stock_qota_rt="11.00", rm="-"), **changes}


def change_row(**changes):
    return {**periodic_row(change_on="2098.06.30", mxmm_shrholdr_nm="가상주주", posesn_stock_co="1,000",
                           qota_rt="10.00", change_cause="합성 사유", rm="-"), **changes}


def large_row(**changes):
    return {
        "rcept_no": RCEPT, "rcept_dt": "20990331", "corp_code": CORP, "corp_name": "가상회사",
        "report_tp": "일반", "repror": "가상투자자", "stkqy": "100", "stkqy_irds": "10", "stkrt": "5.10",
        "stkrt_irds": "0.10", "ctr_stkqy": "100", "ctr_stkrt": "5.10", "report_resn": "합성 사유", **changes,
    }


def insider_row(**changes):
    return {
        "rcept_no": RCEPT, "rcept_dt": "2099-03-31", "corp_code": CORP, "corp_name": "가상회사",
        "repror": "가상임원", "isu_exctv_rgist_at": "등기임원", "isu_exctv_ofcps": "이사", "isu_main_shrholdr": "-",
        "sp_stock_lmp_cnt": "1,000", "sp_stock_lmp_irds_cnt": "100", "sp_stock_lmp_rate": "0.10",
        "sp_stock_lmp_irds_rate": "0.01", **changes,
    }


class FakeOwnership:
    """OwnershipClient 프로토콜의 다섯 메서드가 같은 합성 행을 돌려준다."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def _answer(self, *call_args):
        self.calls.append(call_args)
        return copy.deepcopy(self.rows)

    def get_stock_totals(self, corp_code, *, business_year, report_code):
        return self._answer("stock_totals", corp_code, business_year, report_code)

    def get_major_shareholders(self, corp_code, *, business_year, report_code):
        return self._answer("major_shareholders", corp_code, business_year, report_code)

    def get_major_shareholder_changes(self, corp_code, *, business_year, report_code):
        return self._answer("major_shareholder_changes", corp_code, business_year, report_code)

    def get_large_holdings(self, corp_code):
        return self._answer("large_holdings", corp_code)

    def get_insider_holdings(self, corp_code):
        return self._answer("insider_holdings", corp_code)


# (도구 이름, 함수, 행 생성기, 가짜 클라이언트 호출 이름)
PERIODIC = (
    ("get_shares_outstanding", get_shares_outstanding, totals_row, "stock_totals"),
    ("get_major_shareholders", get_major_shareholders, shareholder_row, "major_shareholders"),
    ("get_major_shareholder_changes", get_major_shareholder_changes, change_row, "major_shareholder_changes"),
)
FILINGS = (
    ("get_large_holdings", get_large_holdings, large_row, "large_holdings",
     ("report_tp", "stkqy", "stkqy_irds", "stkrt", "stkrt_irds", "ctr_stkqy", "ctr_stkrt", "report_resn")),
    ("get_insider_holdings", get_insider_holdings, insider_row, "insider_holdings",
     ("isu_exctv_rgist_at", "isu_exctv_ofcps", "isu_main_shrholdr", "sp_stock_lmp_cnt",
      "sp_stock_lmp_irds_cnt", "sp_stock_lmp_rate", "sp_stock_lmp_irds_rate")),
)
TOP_LEVEL_PERIODIC = {"tool", "document_basis", "corp_code", "bsns_year", "reprt_code", "items", "warnings"}
TOP_LEVEL_FILING = {"tool", "document_basis", "corp_code", "items", "warnings"}


class PeriodicReportTests(unittest.TestCase):
    def test_shape_and_provenance(self):
        for tool, function, make_row, method in PERIODIC:
            with self.subTest(tool=tool):
                source = make_row()
                fake = FakeOwnership([source])
                result = function(fake, corp_code=CORP, year=YEAR)
                self.assertEqual(set(result), TOP_LEVEL_PERIODIC)
                self.assertEqual(
                    (result["tool"], result["document_basis"], result["corp_code"], result["bsns_year"], result["reprt_code"], result["warnings"]),
                    (tool, "periodic_report", CORP, YEAR, "11011", ()),
                )
                item, = result["items"]
                self.assertEqual({key: value for key, value in item.items() if key != "provenance"}, source)
                self.assertEqual(item["provenance"], {
                    "document_basis": "periodic_report", "rcept_no": RCEPT, "stlm_dt": "2098-12-31",
                    "bsns_year": YEAR, "reprt_code": "11011", "public_url": dart_public_url(RCEPT),
                })
                self.assertEqual(fake.calls, [(method, CORP, YEAR, "11011")])
                quarter = function(FakeOwnership([make_row(bsns_year=YEAR, reprt_code="11014")]), corp_code=CORP, year=YEAR, reprt_code="11014")
                self.assertEqual(quarter["items"][0]["provenance"]["reprt_code"], "11014")

    def test_empty_list_is_an_empty_answer(self):
        for tool, function, _make_row, _method in PERIODIC:
            with self.subTest(tool=tool):
                result = function(FakeOwnership([]), corp_code=CORP, year=YEAR)
                self.assertEqual((result["items"], result["warnings"]), ((), ()))

    def test_identity_and_schema_violations(self):
        def without(row, key):
            return {k: v for k, v in row.items() if k != key}

        for tool, function, make_row, _method in PERIODIC:
            cases = {
                "other corp_code": make_row(corp_code="99000002"),
                "missing corp_code": without(make_row(), "corp_code"),
                "other bsns_year": make_row(bsns_year="2097"),
                "other reprt_code": make_row(reprt_code="11012"),
                "short rcept_no": make_row(rcept_no="2099033100000"),
                "missing rcept_no": without(make_row(), "rcept_no"),
                "dotted stlm_dt": make_row(stlm_dt="2098.12.31"),
                "impossible stlm_dt": make_row(stlm_dt="2098-02-30"),
                "missing stlm_dt": without(make_row(), "stlm_dt"),
                "reserved provenance": make_row(provenance={"rcept_no": RCEPT}),
                "reserved unreadable_fields": make_row(unreadable_fields=("nm",)),
            }
            for label, bad in cases.items():
                with self.subTest(tool=tool, case=label), self.assertRaises(OwnershipSchemaError) as caught:
                    function(FakeOwnership([make_row(), bad]), corp_code=CORP, year=YEAR)
                self.assertEqual(caught.exception.code.value, "ownership-schema-invalid")
            # 응답 행의 연도가 숫자로 와도 문자열로 비교한다.
            self.assertEqual(len(function(FakeOwnership([make_row(bsns_year=2098)]), corp_code=CORP, year=YEAR)["items"]), 1)

    def test_invalid_corp_code_never_reaches_the_client(self):
        for tool, function, make_row, _method in PERIODIC:
            for corp_code in ("9900001", "990000011", "9900000A", "９９000001", 99000001):
                fake = FakeOwnership([make_row()])
                with self.subTest(tool=tool, corp_code=corp_code), self.assertRaises(OwnershipSchemaError):
                    function(fake, corp_code=corp_code, year=YEAR)
                self.assertEqual(fake.calls, [])

    def test_unreadable_text_is_nulled_and_flagged(self):
        result = get_major_shareholders(FakeOwnership([shareholder_row(nm="가상�주주", rm="�"), shareholder_row()]), corp_code=CORP, year=YEAR)
        flagged, clean = result["items"]
        self.assertEqual((flagged["nm"], flagged["rm"], flagged["unreadable_fields"]), (None, None, ("nm", "rm")))
        self.assertEqual(flagged["relate"], "본인")
        self.assertNotIn("unreadable_fields", clean)
        self.assertEqual(result["warnings"], ("source-text-unreadable",))

    def test_shares_outstanding_needs_a_share_class(self):
        for bad in ({k: v for k, v in totals_row().items() if k != "se"}, totals_row(se=1), totals_row(se=None)):
            with self.subTest(se=bad.get("se", "missing")), self.assertRaises(OwnershipSchemaError):
                get_shares_outstanding(FakeOwnership([bad]), corp_code=CORP, year=YEAR)
        result = get_shares_outstanding(FakeOwnership([totals_row(se="�")]), corp_code=CORP, year=YEAR)
        self.assertEqual((result["items"][0]["se"], result["items"][0]["unreadable_fields"]), (None, ("se",)))
        # se는 주식 총수 도구만 요구한다.
        self.assertEqual(len(get_major_shareholders(FakeOwnership([shareholder_row()]), corp_code=CORP, year=YEAR)["items"]), 1)


class OwnershipFilingTests(unittest.TestCase):
    def test_shape_dates_and_provenance(self):
        for tool, function, make_row, method, _required in FILINGS:
            for raw_date in ("20990331", "2099-03-31"):
                with self.subTest(tool=tool, raw_date=raw_date):
                    source = make_row(rcept_dt=raw_date)
                    fake = FakeOwnership([source])
                    result = function(fake, corp_code=CORP)
                    self.assertEqual(set(result), TOP_LEVEL_FILING)
                    self.assertEqual((result["tool"], result["document_basis"], result["corp_code"], result["warnings"]),
                                     (tool, "ownership_filing", CORP, ()))
                    item, = result["items"]
                    self.assertEqual((item["rcept_dt"], item["rcept_dt_raw"]), ("2099-03-31", raw_date))
                    for key, value in source.items():
                        if key != "rcept_dt":
                            self.assertEqual(item[key], value, key)
                    self.assertEqual(item["provenance"], {
                        "document_basis": "ownership_filing", "rcept_no": RCEPT, "rcept_dt": "2099-03-31",
                        "rcept_dt_raw": raw_date, "repror": source["repror"], "public_url": dart_public_url(RCEPT),
                    })
                    self.assertEqual(fake.calls, [(method, CORP)])

    def test_empty_list_is_an_empty_answer(self):
        for tool, function, _make_row, _method, _required in FILINGS:
            with self.subTest(tool=tool):
                self.assertEqual(function(FakeOwnership([]), corp_code=CORP)["items"], ())

    def test_identity_and_schema_violations(self):
        for tool, function, make_row, _method, required in FILINGS:
            cases = {
                "other corp_code": make_row(corp_code="99000002"),
                "short rcept_no": make_row(rcept_no="2099033100000"),
                "slashed rcept_dt": make_row(rcept_dt="2099/03/31"),
                "impossible rcept_dt": make_row(rcept_dt="20990230"),
                "numeric rcept_dt": make_row(rcept_dt=20990331),
                "blank repror": make_row(repror="  "),
                "missing repror": {k: v for k, v in make_row().items() if k != "repror"},
                "reserved provenance": make_row(provenance={}),
                "reserved rcept_dt_raw": make_row(rcept_dt_raw="20990331"),
                "reserved unreadable_fields": make_row(unreadable_fields=()),
            }
            for field in required:
                cases[f"missing {field}"] = {k: v for k, v in make_row().items() if k != field}
            for label, bad in cases.items():
                with self.subTest(tool=tool, case=label), self.assertRaises(OwnershipSchemaError):
                    function(FakeOwnership([make_row(), bad]), corp_code=CORP)

    def test_required_fields_are_tool_specific(self):
        # 대량보유 행에는 임원 전용 필드가 없어도 되고, 그 반대도 같다.
        self.assertEqual(len(get_large_holdings(FakeOwnership([large_row()]), corp_code=CORP)["items"]), 1)
        with self.assertRaises(OwnershipSchemaError):
            get_insider_holdings(FakeOwnership([large_row()]), corp_code=CORP)
        with self.assertRaises(OwnershipSchemaError):
            get_large_holdings(FakeOwnership([insider_row()]), corp_code=CORP)

    def test_unreadable_reporter_is_dropped_from_provenance(self):
        result = get_large_holdings(FakeOwnership([large_row(repror="가상�투자자")]), corp_code=CORP)
        item, = result["items"]
        self.assertIsNone(item["repror"])
        self.assertIsNone(item["provenance"]["repror"])
        self.assertEqual(item["unreadable_fields"], ("repror",))
        self.assertEqual(result["warnings"], ("source-text-unreadable",))

    def test_invalid_corp_code_never_reaches_the_client(self):
        for tool, function, make_row, _method, _required in FILINGS:
            fake = FakeOwnership([make_row()])
            with self.subTest(tool=tool), self.assertRaises(OwnershipSchemaError):
                function(fake, corp_code="9900000")
            self.assertEqual(fake.calls, [])


ENDPOINTS = {
    "get_shares_outstanding": ("/api/stockTotqySttus.json", totals_row, {"corp_code": CORP, "year": YEAR}),
    "get_major_shareholders": ("/api/hyslrSttus.json", shareholder_row, {"corp_code": CORP, "year": YEAR}),
    "get_major_shareholder_changes": ("/api/hyslrChgSttus.json", change_row, {"corp_code": CORP, "year": YEAR}),
    "get_large_holdings": ("/api/majorstock.json", large_row, {"corp_code": CORP}),
    "get_insider_holdings": ("/api/elestock.json", insider_row, {"corp_code": CORP}),
}


class OwnershipToolTests(IsolatedCase):
    """실제 DartReadonlyClient와 런타임 배선을 mock HTTP로 통과시킨다."""

    def setUp(self):
        super().setUp()
        self.answer = None

        def handler(request):
            return self.answer(request)

        self.runtime = create_live_runtime(self.settings(), http_client=self.mock_client(handler), state_dir=self.root / "state")
        self.addCleanup(self.runtime.close)

    def test_happy_path_requests_and_envelopes(self):
        for name, (path, make_row, arguments) in ENDPOINTS.items():
            with self.subTest(name=name):
                self.requests.clear()
                self.answer = lambda request, make_row=make_row: httpx.Response(200, json={"status": "000", "message": "정상", "list": [make_row()]})
                result = call(self.runtime.service, name, arguments)
                self.assertEqual(result["source"], "dart")
                self.assertEqual(result["reference"]["value"], path.rsplit("/", 1)[1])
                self.assertEqual(result["data"]["tool"], name)
                self.assertEqual(result["data"]["items"][0]["provenance"]["public_url"], dart_public_url(RCEPT))
                request, = self.requests
                self.assertEqual(request.url.path, path)
                params = dict(request.url.params)
                self.assertEqual(params.pop("crtfc_key"), DART_KEY)
                expected = {"corp_code": CORP}
                if "year" in arguments:
                    expected.update(bsns_year=YEAR, reprt_code="11011")
                self.assertEqual(params, expected)
                self.assert_no_secrets(repr(result))

    def test_explicit_report_code_is_forwarded(self):
        self.answer = lambda request: httpx.Response(200, json={"status": "000", "list": [shareholder_row(reprt_code="11012")]})
        result = call(self.runtime.service, "get_major_shareholders", {"corp_code": CORP, "year": YEAR, "reprt_code": "11012"})
        self.assertEqual(result["data"]["reprt_code"], "11012")
        self.assertEqual(self.requests[0].url.params["reprt_code"], "11012")

    def test_dart_statuses_on_every_ownership_endpoint(self):
        expected_by_status = {
            "010": ("unavailable", "dart-auth-failed"),
            "013": ("blocked", "dart-no-data"),
            "014": ("blocked", "dart-response-invalid"),
            "020": ("unavailable", "dart-rate-limited"),
            "021": ("blocked", "dart-request-invalid"),
            "100": ("blocked", "dart-request-invalid"),
            "800": ("unavailable", "dart-maintenance"),
            "900": ("unavailable", "dart-request-failed"),
        }
        for status, (envelope_status, code) in expected_by_status.items():
            self.answer = lambda request, status=status: httpx.Response(200, json={"status": status, "message": "합성 응답"})
            for name, (_path, _make_row, arguments) in ENDPOINTS.items():
                with self.subTest(status=status, name=name):
                    result = call(self.runtime.service, name, arguments)
                    self.assertEqual(result["data"], {"status": envelope_status, "error": code})
                    self.assert_no_secrets(repr(result))

    def test_malformed_responses(self):
        cases = (
            ({"status": "000", "message": "정상"}, ("blocked", "dart-response-invalid")),
            ({"status": "000", "list": {"0": {}}}, ("blocked", "dart-response-invalid")),
            ({"status": "000", "list": ["row"]}, ("blocked", "dart-response-invalid")),
        )
        for payload, expected in cases:
            self.answer = lambda request, payload=payload: httpx.Response(200, json=payload)
            for name, (_path, _make_row, arguments) in ENDPOINTS.items():
                with self.subTest(payload=payload, name=name):
                    self.assertEqual(call(self.runtime.service, name, arguments)["data"], {"status": expected[0], "error": expected[1]})
        self.answer = lambda request: httpx.Response(503, text="synthetic")
        result = call(self.runtime.service, "get_insider_holdings", {"corp_code": CORP})
        self.assertEqual(result["data"], {"status": "unavailable", "error": "dart-request-failed"})

    def test_row_identity_failures_are_blocked(self):
        for name, (_path, make_row, arguments) in ENDPOINTS.items():
            with self.subTest(name=name):
                self.answer = lambda request, make_row=make_row: httpx.Response(200, json={"status": "000", "list": [make_row(corp_code="99000002")]})
                self.assertEqual(call(self.runtime.service, name, arguments)["data"], {"status": "blocked", "error": "ownership-schema-invalid"})

    def test_schema_rejects_bad_arguments_before_any_request(self):
        self.answer = lambda request: self.fail("no request expected")
        bad = (
            ("get_major_shareholders", {"corp_code": "9900001", "year": YEAR}),
            ("get_major_shareholders", {"corp_code": CORP, "year": "98"}),
            ("get_major_shareholders", {"corp_code": CORP}),
            ("get_shares_outstanding", {"corp_code": CORP, "year": YEAR, "reprt_code": "11015"}),
            ("get_major_shareholder_changes", {"corp_code": CORP, "year": "２０９８"}),
            ("get_large_holdings", {"corp_code": CORP, "year": YEAR}),
            ("get_insider_holdings", {}),
        )
        for name, arguments in bad:
            with self.subTest(name=name, arguments=arguments), self.assertRaises(ToolArgumentsError):
                call(self.runtime.service, name, arguments)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
