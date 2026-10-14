"""get_financial_statements: 손으로 만든 합성 DART 응답으로만 검증한다.

회사 고유번호, 접수번호, 계정, 금액은 모두 지어낸 값이다.
"""

import asyncio
import copy
import unittest

import httpx

from autostock_lite.dart.client import DartNoData
from autostock_lite.dart.statements import (
    UNIT_NOTE,
    StatementsDateUnavailable,
    StatementsIdentityError,
    StatementsNoData,
    StatementsSchemaError,
    get_financial_statements,
)
from autostock_lite.mcp_runtime import create_live_runtime
from autostock_lite.mcp_server import TOOL_REGISTRY, ToolArgumentsError, ToolService
from test_smoke import IsolatedCase


CORP = "99000001"
YEAR = "2025"
RCEPT = "20990331000001"
OTHER_RCEPT = "20990331000002"


def all_row(sj_div, sj_nm, account_id, account_nm, ord_, thstrm, frmtrm, **extra):
    row = {
        "rcept_no": RCEPT, "reprt_code": "11011", "bsns_year": YEAR, "corp_code": CORP,
        "sj_div": sj_div, "sj_nm": sj_nm, "account_id": account_id, "account_nm": account_nm,
        "account_detail": "-", "thstrm_nm": "제 10 기", "thstrm_amount": thstrm,
        "frmtrm_nm": "제 9 기", "frmtrm_amount": frmtrm, "ord": ord_, "currency": "KRW",
    }
    row.update(extra)
    return row


def all_rows():
    return [
        all_row("BS", "재무상태표", "ifrs-full_Assets", "자산총계", "1", "1000000", "900000"),
        all_row("BS", "재무상태표", "ifrs-full_Liabilities", "부채총계", "2", "400000", "300000"),
        all_row("BS", "재무상태표", "ifrs-full_Equity", "자본총계", "3", "600000", "600000"),
        all_row("IS", "손익계산서", "ifrs-full_Revenue", "매출액", "4", "500000", "-100000",
                thstrm_add_amount="500000", bfefrmtrm_nm="제 8 기", bfefrmtrm_amount="200000"),
    ]


def date_row(fs_div, sj_div, thstrm_dt, account_nm="자산총계", **extra):
    row = {
        "rcept_no": RCEPT, "bsns_year": YEAR, "reprt_code": "11011", "account_nm": account_nm,
        "fs_div": fs_div, "fs_nm": "연결재무제표" if fs_div == "CFS" else "재무제표",
        "sj_div": sj_div, "sj_nm": "재무상태표" if sj_div == "BS" else "손익계산서",
        "thstrm_nm": "제 10 기", "thstrm_dt": thstrm_dt, "thstrm_amount": "1000000",
        "ord": "1", "currency": "KRW",
    }
    row.update(extra)
    return row


def date_rows():
    return [
        date_row("CFS", "BS", "2025.12.31 현재"),
        date_row("CFS", "BS", "2025.12.31 현재", account_nm="부채총계"),
        date_row("CFS", "IS", "2025.01.01 ~ 2025.12.31", account_nm="매출액"),
        date_row("OFS", "BS", "2025.12.31 현재"),
    ]


class FakeDart:
    """DartReadonlyClient의 두 재무제표 메서드만 흉내 낸다."""

    def __init__(self, all_result=None, single_result=None):
        self.all_result = all_rows() if all_result is None else all_result
        self.single_result = date_rows() if single_result is None else single_result
        self.calls = []

    def get_financials_all(self, corp_code, *, business_year, report_code, fs_div="CFS"):
        self.calls.append(("all", corp_code, business_year, report_code, fs_div))
        if isinstance(self.all_result, Exception):
            raise self.all_result
        return copy.deepcopy(self.all_result)

    def get_financials_single(self, corp_code, *, business_year, report_code):
        self.calls.append(("single", corp_code, business_year, report_code))
        if isinstance(self.single_result, Exception):
            raise self.single_result
        return copy.deepcopy(self.single_result)


ALLOWED_ROW_KEYS = {"sj_div", "sj_nm", "account_id", "account_nm", "account_detail", "currency", "ord"}
TOP_LEVEL_KEYS = {
    "corp_code", "bsns_year", "reprt_code", "report_name", "fs_div", "fs_name", "rcept_no",
    "statement_date", "statement_date_raw", "unit_note", "rows", "provenance",
}


def call(fake, **overrides):
    arguments = {"corp_code": CORP, "bsns_year": YEAR, **overrides}
    return get_financial_statements(fake, **arguments)


class HappyPathTests(unittest.TestCase):
    def test_rows_identity_and_date_as_disclosed(self):
        fake = FakeDart()
        result = call(fake)
        self.assertEqual(set(result), TOP_LEVEL_KEYS)
        self.assertEqual(
            {key: result[key] for key in ("corp_code", "bsns_year", "reprt_code", "fs_div", "rcept_no")},
            {"corp_code": CORP, "bsns_year": YEAR, "reprt_code": "11011", "fs_div": "CFS", "rcept_no": RCEPT},
        )
        self.assertEqual((result["report_name"], result["fs_name"]), ("사업보고서", "연결재무제표"))
        self.assertEqual(result["statement_date"], "2025-12-31")
        self.assertEqual(result["statement_date_raw"], "2025.12.31 현재")
        self.assertEqual(result["unit_note"], UNIT_NOTE)
        self.assertIn("원", UNIT_NOTE)
        self.assertEqual(fake.calls, [("all", CORP, YEAR, "11011", "CFS"), ("single", CORP, YEAR, "11011")])

        # 행은 공시 문자열 그대로이고, 신원 필드는 행에서 빠져 최상위에만 있다.
        expected_rows = []
        for source in all_rows():
            expected_rows.append({k: v for k, v in source.items() if k not in {"rcept_no", "reprt_code", "bsns_year", "corp_code"}})
        self.assertEqual(result["rows"], expected_rows)
        self.assertEqual(result["rows"][3]["frmtrm_amount"], "-100000")
        self.assertEqual(result["rows"][3]["bfefrmtrm_amount"], "200000")
        self.assertEqual(result["provenance"][0]["rcept_no"], RCEPT)
        self.assertEqual(result["provenance"][1]["raw_value"], "2025.12.31 현재")

    def test_no_derived_or_unknown_fields(self):
        rows = all_rows()
        rows[0].update(frmtrm_q_nm="제 9 기 3분기", frmtrm_q_amount="850000", unexpected_total="1", stock_code="000001")
        result = call(FakeDart(all_result=rows))
        self.assertEqual(set(result), TOP_LEVEL_KEYS)
        for row in result["rows"]:
            for key in row:
                self.assertTrue(key in ALLOWED_ROW_KEYS or key.startswith(("thstrm_", "frmtrm_", "bfefrmtrm_")), key)
            for value in row.values():
                self.assertIsInstance(value, str)
        self.assertEqual(result["rows"][0]["frmtrm_q_amount"], "850000")
        self.assertNotIn("unexpected_total", result["rows"][0])
        # 입력 행과 금액 개수가 같아야 한다: 합계 행이나 비율 행을 만들지 않는다.
        self.assertEqual(len(result["rows"]), len(rows))
        disclosed_amounts = sorted(v for row in rows for k, v in row.items() if k.endswith("_amount"))
        returned_amounts = sorted(v for row in result["rows"] for k, v in row.items() if k.endswith("_amount"))
        self.assertEqual(returned_amounts, disclosed_amounts)

    def test_separate_statements_select_ofs_date(self):
        singles = date_rows()
        singles[3]["thstrm_dt"] = "2025.06.30 현재"
        rows = all_rows()
        for row in rows:
            row["fs_div"] = "OFS"
        fake = FakeDart(all_result=rows, single_result=singles)
        result = call(fake, fs_div="OFS")
        self.assertEqual((result["fs_div"], result["fs_name"]), ("OFS", "별도재무제표"))
        self.assertEqual(result["statement_date"], "2025-06-30")
        self.assertEqual(fake.calls[0], ("all", CORP, YEAR, "11011", "OFS"))

    def test_quarter_report_codes(self):
        for code, name in (("11012", "반기보고서"), ("11013", "1분기보고서"), ("11014", "3분기보고서")):
            with self.subTest(code=code):
                rows = [dict(row, reprt_code=code) for row in all_rows()]
                singles = [dict(row, reprt_code=code) for row in date_rows()]
                result = call(FakeDart(all_result=rows, single_result=singles), reprt_code=code)
                self.assertEqual((result["reprt_code"], result["report_name"]), (code, name))


class NoDataTests(unittest.TestCase):
    def test_013_and_empty_are_no_data(self):
        for result in (DartNoData("synthetic status 013"), []):
            with self.subTest(result=result):
                fake = FakeDart(all_result=result)
                with self.assertRaises(StatementsNoData):
                    call(fake)
                self.assertEqual([c[0] for c in fake.calls], ["all"])


class IdentityTests(unittest.TestCase):
    def assert_identity_error(self, **fake_kw):
        with self.assertRaises(StatementsIdentityError):
            call(FakeDart(**fake_kw))

    def test_all_rows_must_match_request(self):
        for field, value in (("bsns_year", "2024"), ("reprt_code", "11012"), ("corp_code", "99000002"), ("fs_div", "OFS")):
            with self.subTest(field=field):
                rows = all_rows()
                rows[1][field] = value
                self.assert_identity_error(all_result=rows)
                rows = [dict(row, **{field: value}) for row in all_rows()]
                self.assert_identity_error(all_result=rows)

    def test_receipt_must_be_single_and_shared(self):
        rows = all_rows()
        rows[2]["rcept_no"] = OTHER_RCEPT
        self.assert_identity_error(all_result=rows)
        singles = [dict(row, rcept_no=OTHER_RCEPT) for row in date_rows()]
        self.assert_identity_error(single_result=singles)
        singles = date_rows()
        singles[3]["rcept_no"] = OTHER_RCEPT
        self.assert_identity_error(single_result=singles)

    def test_single_rows_must_match_request(self):
        for field, value in (("bsns_year", "2024"), ("reprt_code", "11014"), ("corp_code", "99000002")):
            with self.subTest(field=field):
                singles = [dict(row, **{field: value}) for row in date_rows()]
                self.assert_identity_error(single_result=singles)

    def test_optional_corp_code_and_fs_div_may_be_absent(self):
        rows = [{k: v for k, v in row.items() if k != "corp_code"} for row in all_rows()]
        self.assertEqual(call(FakeDart(all_result=rows))["rcept_no"], RCEPT)
        rows = [dict(row, fs_div="CFS") for row in all_rows()]
        self.assertEqual(call(FakeDart(all_result=rows))["rcept_no"], RCEPT)
        singles = [dict(row, corp_code=CORP) for row in date_rows()]
        self.assertEqual(call(FakeDart(single_result=singles))["rcept_no"], RCEPT)


class StockCodeTests(unittest.TestCase):
    """stock_code는 선택 필드다. 양쪽에 모두 있을 때만 일치를 요구한다."""

    @staticmethod
    def with_stock(rows, value):
        return [dict(row, stock_code=value) for row in rows]

    def test_absent_or_one_sided_is_accepted(self):
        cases = {
            "both absent": (all_rows(), date_rows()),
            "single only": (all_rows(), self.with_stock(date_rows(), "000001")),
            "all only": (self.with_stock(all_rows(), "000001"), date_rows()),
            "equal": (self.with_stock(all_rows(), "000001"), self.with_stock(date_rows(), "000001")),
            "blank means absent": (self.with_stock(all_rows(), " "), self.with_stock(date_rows(), "000001")),
        }
        for label, (rows, singles) in cases.items():
            with self.subTest(label):
                self.assertEqual(call(FakeDart(all_result=rows, single_result=singles))["rcept_no"], RCEPT)

    def test_disagreement_is_identity_error(self):
        cases = {
            "across responses": (self.with_stock(all_rows(), "000001"), self.with_stock(date_rows(), "000002")),
            "within single": (all_rows(), self.with_stock(date_rows()[:3], "000001") + self.with_stock(date_rows()[3:], "000002")),
            "within all": (self.with_stock(all_rows()[:1], "000001") + self.with_stock(all_rows()[1:], "000002"), date_rows()),
        }
        for label, (rows, singles) in cases.items():
            with self.subTest(label), self.assertRaises(StatementsIdentityError):
                call(FakeDart(all_result=rows, single_result=singles))

    def test_non_string_stock_code_is_schema_error(self):
        with self.assertRaises(StatementsSchemaError):
            call(FakeDart(single_result=self.with_stock(date_rows(), 1)))


class DateJoinTests(unittest.TestCase):
    def assert_date_unavailable(self, singles, **kw):
        with self.assertRaises(StatementsDateUnavailable):
            call(FakeDart(single_result=singles), **kw)

    def test_single_response_missing(self):
        self.assert_date_unavailable(DartNoData("synthetic status 013"))
        self.assert_date_unavailable([])

    def test_no_bs_row_for_requested_fs_div(self):
        self.assert_date_unavailable([row for row in date_rows() if row["fs_div"] == "OFS"])
        self.assert_date_unavailable([row for row in date_rows() if row["sj_div"] != "BS"])
        rows = [dict(row, fs_div="OFS") for row in all_rows()]
        with self.assertRaises(StatementsDateUnavailable):
            call(FakeDart(all_result=rows, single_result=[r for r in date_rows() if r["fs_div"] == "CFS"]), fs_div="OFS")

    def test_unreadable_or_conflicting_dates(self):
        for label, value in (
            ("missing", None), ("words", "미정"), ("range in BS", "2025.01.01 ~ 2025.12.31"),
            ("impossible", "2025.02.30 현재"), ("number", 20251231),
        ):
            with self.subTest(label):
                singles = date_rows()
                if value is None:
                    del singles[0]["thstrm_dt"]
                else:
                    singles[0]["thstrm_dt"] = value
                self.assert_date_unavailable(singles)
        singles = date_rows()
        singles[1]["thstrm_dt"] = "2025.09.30 현재"
        self.assert_date_unavailable(singles)

    def test_equivalent_spellings_of_one_date_are_accepted(self):
        singles = date_rows()
        singles[1]["thstrm_dt"] = "2025-12-31"
        result = call(FakeDart(single_result=singles))
        self.assertEqual(result["statement_date"], "2025-12-31")
        self.assertEqual(result["statement_date_raw"], "2025-12-31")


class SchemaTests(unittest.TestCase):
    def test_malformed_rows_fail_closed(self):
        def drop(field):
            rows = all_rows()
            del rows[0][field]
            return rows

        cases = {
            "rcept_no missing": dict(all_result=drop("rcept_no")),
            "bsns_year missing": dict(all_result=drop("bsns_year")),
            "currency missing": dict(all_result=drop("currency")),
            "account_nm missing": dict(all_result=drop("account_nm")),
            "amount not string": dict(all_result=[dict(all_rows()[0], thstrm_amount=1000000)] + all_rows()[1:]),
            "receipt shape": dict(all_result=[dict(row, rcept_no="2099-0331") for row in all_rows()]),
            "rows not a list": dict(all_result={"list": []}),
            "row not an object": dict(all_result=all_rows() + ["row"]),
            "fs_div missing in single": dict(single_result=[{k: v for k, v in row.items() if k != "fs_div"} for row in date_rows()]),
            "blank corp_code": dict(all_result=[dict(row, corp_code="") for row in all_rows()]),
        }
        for label, kw in cases.items():
            with self.subTest(label), self.assertRaises(StatementsSchemaError):
                call(FakeDart(**kw))


class InputValidationTests(IsolatedCase):
    def service(self, fake):
        return ToolService({"get_financial_statements": lambda **a: get_financial_statements(fake, **a)})

    def test_schema_enums_and_ascii_digits(self):
        spec = TOOL_REGISTRY["get_financial_statements"].input_schema
        self.assertEqual(spec["properties"]["reprt_code"]["enum"], ["11011", "11012", "11013", "11014"])
        self.assertEqual(spec["properties"]["fs_div"]["enum"], ["CFS", "OFS"])
        self.assertEqual(spec["required"], ["corp_code", "bsns_year"])
        fake = FakeDart()
        service = self.service(fake)
        bad_arguments = (
            {"corp_code": CORP, "bsns_year": YEAR, "reprt_code": "11015"},
            {"corp_code": CORP, "bsns_year": YEAR, "reprt_code": 11011},
            {"corp_code": CORP, "bsns_year": YEAR, "fs_div": "cfs"},
            {"corp_code": CORP, "bsns_year": YEAR, "fs_div": "SFS"},
            {"corp_code": "９９０００００１", "bsns_year": YEAR},
            {"corp_code": "٩٩٠٠٠٠٠١", "bsns_year": YEAR},
            {"corp_code": CORP, "bsns_year": "25"},
            {"corp_code": CORP},
            {"corp_code": CORP, "bsns_year": YEAR, "stock_code": "000001"},
        )
        for arguments in bad_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(ToolArgumentsError):
                asyncio.run(service.call("get_financial_statements", arguments))
        self.assertEqual(fake.calls, [])
        for kwargs in ({"reprt_code": "11015"}, {"fs_div": "SFS"}, {"corp_code": "٩٩٠٠٠٠٠١"}):
            with self.subTest(direct=kwargs), self.assertRaises(ValueError):
                get_financial_statements(fake, **{"corp_code": CORP, "bsns_year": YEAR, **kwargs})
        self.assertEqual(fake.calls, [])

    def test_defaults_and_blocked_envelopes(self):
        fake = FakeDart()
        result = asyncio.run(self.service(fake).call("get_financial_statements", {"corp_code": CORP, "bsns_year": YEAR}))
        self.assertEqual(fake.calls[0], ("all", CORP, YEAR, "11011", "CFS"))
        self.assertEqual(result["data"]["rcept_no"], RCEPT)
        self.assertEqual(result["provenance"], result["data"]["provenance"])
        cases = {
            "financials-no-data": FakeDart(all_result=DartNoData("synthetic")),
            "financials-identity-mismatch": FakeDart(single_result=[dict(r, rcept_no=OTHER_RCEPT) for r in date_rows()]),
            "financials-date-unavailable": FakeDart(single_result=DartNoData("synthetic")),
            "financials-schema-invalid": FakeDart(all_result=[dict(r, thstrm_amount=1) for r in all_rows()]),
        }
        for code, fake in cases.items():
            with self.subTest(code):
                result = asyncio.run(self.service(fake).call("get_financial_statements", {"corp_code": CORP, "bsns_year": YEAR}))
                self.assertEqual(result["data"], {"status": "blocked", "error": code})


class RuntimeWiringTests(IsolatedCase):
    """실제 DartReadonlyClient와 런타임 배선을 mock HTTP로 끝까지 통과시킨다."""

    def test_live_client_path(self):
        def handler(request):
            params = dict(request.url.params)
            self.assertEqual(params["crtfc_key"], "test-dart-key-0001")
            self.assertEqual((params["corp_code"], params["bsns_year"], params["reprt_code"]), (CORP, YEAR, "11011"))
            if request.url.path == "/api/fnlttSinglAcntAll.json":
                self.assertEqual(params["fs_div"], "CFS")
                return httpx.Response(200, json={"status": "000", "message": "정상", "list": all_rows()})
            self.assertEqual(request.url.path, "/api/fnlttSinglAcnt.json")
            self.assertNotIn("fs_div", params)
            return httpx.Response(200, json={"status": "000", "message": "정상", "list": date_rows()})

        runtime = create_live_runtime(self.settings(), http_client=self.mock_client(handler), state_dir=self.root / "state")
        self.addCleanup(runtime.close)
        result = asyncio.run(runtime.service.call("get_financial_statements", {"corp_code": CORP, "bsns_year": YEAR}))
        self.assertEqual(result["source"], "dart")
        self.assertEqual(result["data"]["statement_date"], "2025-12-31")
        self.assertEqual(len(result["data"]["rows"]), 4)
        self.assert_no_secrets(repr(result))
        self.assertEqual([r.url.path for r in self.requests], ["/api/fnlttSinglAcntAll.json", "/api/fnlttSinglAcnt.json"])

    def test_live_status_013_is_blocked_no_data(self):
        def handler(request):
            return httpx.Response(200, json={"status": "013", "message": "조회된 데이타가 없습니다."})

        runtime = create_live_runtime(self.settings(), http_client=self.mock_client(handler), state_dir=self.root / "state")
        self.addCleanup(runtime.close)
        result = asyncio.run(runtime.service.call("get_financial_statements", {"corp_code": CORP, "bsns_year": YEAR, "fs_div": "OFS"}))
        self.assertEqual(result["data"], {"status": "blocked", "error": "financials-no-data"})
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
