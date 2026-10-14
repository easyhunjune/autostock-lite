"""응답 마스킹, 오류 코드 등록, 성장률 기준값, 로깅 경계를 합성 데이터로 검증한다."""

import ast
import asyncio
from datetime import date
import importlib
import inspect
import logging
from pathlib import Path
import pkgutil
import unittest
from unittest.mock import patch

import httpx

import autostock_lite
from autostock_lite import cli
from autostock_lite.calc.growth import GROWTH_FORMULA_VERSION, growth_metrics
from autostock_lite.dart.client import (
    DartAuthError, DartLiveError, DartMaintenance, DartNoData, DartRateLimited,
    DartReadonlyClient, DartRequestRejected, DartResponseError, raise_for_dart_status,
)
from autostock_lite.dart.corp_code import (
    CorpCodeDownloadError, CorpCodeError, CorpCodeIndexError, CorpCodeRecord,
)
from autostock_lite.dart.filings import (
    FilingArchiveError, FilingArgumentError, FilingEncodingError, FilingError,
    FilingSchemaError, FilingSection,
)
from autostock_lite.dart.ownership import OwnershipError, OwnershipSchemaError
from autostock_lite.mcp_runtime import FilingContextError
from autostock_lite.mcp_server import TOOL_REGISTRY, ToolArgumentsError, ToolService
from autostock_lite.provenance import MASK
from autostock_lite.status_codes import (
    ERROR_CODES, UNAVAILABLE_CODES, ErrorCode, InvalidArgumentError,
)
from autostock_lite.toss.auth import TossAuthError, TossIpNotAllowed
from autostock_lite.toss.live import TossLiveError, TossLiveIpNotAllowed, TossRateLimited
from autostock_lite.toss.models import StrictModelError
from test_smoke import DART_KEY, ROOT, TOSS_SECRET, IsolatedCase


SRC = ROOT / "src" / "autostock_lite"
RCEPT = "20990331000001"
CORP = "99000001"

# 공시 원문에 실제로 나올 법한 모양을 손으로 만든 합성 문서.
SYNTHETIC_DOCUMENT = (
    "가상회사 주식회사 사업의 내용\n"
    "\t회사 홈페이지: https://ir.example.test/report?year=2025&lang=ko&q=a+b#top 참고\n"
    "| 구분 | 금액 |\n"
    "| 매출 | 1,000 |\n"
    "The bearer of this note may redeem it at par.\r\n"
    "질문?&답 50% 100 http://plain.example.test/?x=%20y&z\n"
    "끝\t"
)


def section(text):
    return FilingSection(RCEPT, "annual_report", "사업의 내용", "ok", text, (text, "| a | b |", "bearer bonds"))


def call(service, name, arguments):
    return asyncio.run(service.call(name, arguments))


class RedactionRoundTripTests(unittest.TestCase):
    """마스킹은 응답 본문을 다시 조립하지 않는다 (URL·줄바꿈·탭·표 행 보존)."""

    arguments = {"rcept_no": RCEPT, "section": "사업의 내용"}

    def service(self, payload):
        return ToolService({"get_filing_section": lambda **_: payload}, secret_values=(DART_KEY, TOSS_SECRET))

    def test_filing_text_is_byte_identical(self):
        result = call(self.service(section(SYNTHETIC_DOCUMENT)), "get_filing_section", self.arguments)
        self.assertEqual(result["data"]["text"], SYNTHETIC_DOCUMENT)
        self.assertEqual(result["data"]["sentences"], [SYNTHETIC_DOCUMENT, "| a | b |", "bearer bonds"])
        self.assertEqual(result["data"]["text"].encode("utf-8"), SYNTHETIC_DOCUMENT.encode("utf-8"))

    def test_nested_values_are_byte_identical(self):
        payload = {"blocks": [{"before": [SYNTHETIC_DOCUMENT], "after": ["https://a.example.test/?k=v&m=1\n\t"]}]}
        result = call(self.service(payload), "get_filing_section", self.arguments)
        self.assertEqual(result["data"], payload)

    def test_configured_secrets_are_masked(self):
        text = f"앞 {DART_KEY} 중간\n{TOSS_SECRET}\t뒤"
        result = call(self.service(section(text)), "get_filing_section", self.arguments)
        self.assertEqual(result["data"]["text"], f"앞 {MASK} 중간\n{MASK}\t뒤")
        self.assertNotIn(DART_KEY, repr(result))

    def test_secret_query_params_and_authorization_tokens_only(self):
        # The header token is token-shaped (17 chars, letters and digits); a shorter
        # plain value is deliberately left alone (see RedactionTighteningTests).
        text = (
            "요청 https://opendart.example.test/api/list.json?corp_code=99000001&crtfc_key=unconfigured0001&page_no=1#p\n"
            "다른 표기 http://h.example.test/cb?Client-Secret=abc123&state=x+y\r\n"
            "헤더 Authorization: Bearer abc.DEF-123_456==\n"
            '{"authorization": "Bearer eyJzeW50aGV0aWMiOjF9.eyJ0ZXN0IjoiMjA5OSJ9.c3ludGhldGlj"}\n'
            "The bearer bond stays readable. Bearer 100 units.\n"
            "| crtfc_key | unconfigured0001 |"
        )
        expected = (
            f"요청 https://opendart.example.test/api/list.json?corp_code=99000001&crtfc_key={MASK}&page_no=1#p\n"
            f"다른 표기 http://h.example.test/cb?Client-Secret={MASK}&state=x+y\r\n"
            f"헤더 Authorization: Bearer {MASK}\n"
            f'{{"authorization": "Bearer {MASK}"}}\n'
            "The bearer bond stays readable. Bearer 100 units.\n"
            "| crtfc_key | unconfigured0001 |"
        )
        result = call(self.service(section(text)), "get_filing_section", self.arguments)
        self.assertEqual(result["data"]["text"], expected)

    def test_sensitive_keys_are_masked(self):
        payload = {"crtfc_key": "unconfigured0001", "Client-Secret": "x", "corp_code": CORP}
        result = call(self.service(payload), "get_filing_section", self.arguments)
        self.assertEqual(result["data"], {"crtfc_key": MASK, "Client-Secret": MASK, "corp_code": CORP})


class RedactionTighteningTests(unittest.TestCase):
    """Only the secret bytes change; prose, glued text and quotes stay put."""

    def redact(self, text):
        payload = FilingSection(RCEPT, "annual_report", "사업의 내용", "ok", text, (text,))
        service = ToolService({"get_filing_section": lambda **_: payload}, secret_values=(DART_KEY,))
        result = call(service, "get_filing_section", {"rcept_no": RCEPT, "section": "사업의 내용"})
        self.assertEqual(result["data"]["sentences"], [result["data"]["text"]])
        return result["data"]["text"]

    def test_bearer_prose_is_byte_identical(self):
        for prose in (
            "Board authorization: bearer bonds were issued",
            "Board authorization: bearer bonds.",
            "Authorization: Bearer of the note",
            "Bearer 100 units. The bearer clause applies.",
            "이사회 authorization: bearer 채권 발행",
            "The bearer 3.5% notes",
            "to bearer e.g. on demand",
            "the bearer U.S. persons",
            "a bearer non-interest-bearing-instrument",
            "a bearer non-interest-bearing-2099-instrument",
            "the bearer instruments.Furthermore",
            "Authorization: bearer of record 2099.",
            "무기명식(bearer) 사채 100% 상환\t| 구분 | bearer |\n다음 줄",
        ):
            with self.subTest(prose=prose):
                self.assertEqual(self.redact(prose).encode("utf-8"), prose.encode("utf-8"))

    def test_query_value_stops_at_key_shaped_characters(self):
        self.assertEqual(self.redact("https://x.example.test/?crtfc_key=K에서 오류"),
                         f"https://x.example.test/?crtfc_key={MASK}에서 오류")
        self.assertEqual(self.redact('링크 "https://x.example.test/?crtfc_key=abc123" 끝'),
                         f'링크 "https://x.example.test/?crtfc_key={MASK}" 끝')
        self.assertEqual(self.redact("(see https://x.example.test/a?b=1&client_secret=s3cr%2Bt).\n다음"),
                         f"(see https://x.example.test/a?b=1&client_secret={MASK}).\n다음")
        self.assertEqual(self.redact("요청 https://x.example.test/?crtfc_key=abc123."),
                         f"요청 https://x.example.test/?crtfc_key={MASK}.")
        self.assertEqual(self.redact("https://x.example.test/p;dart-api-key=abc123?x=1"),
                         f"https://x.example.test/p;dart-api-key={MASK}?x=1")
        # Outside an http(s) URL a "crtfc_key=" pair is ordinary text.
        self.assertEqual(self.redact("설정 예: ?crtfc_key=abc123 형식"), "설정 예: ?crtfc_key=abc123 형식")

    def test_token_shaped_bearer_values_are_masked_with_or_without_authorization(self):
        long_token = "abcdefghijklmnopqrstuvwxyz0123456789"
        jwt = "eyJzeW50aGV0aWMiOjF9.eyJ0ZXN0IjoiMjA5OSJ9.c3ludGhldGlj"
        base64_token = "c3ludGhldGljIHRva2VuIDIwOTk5OQ=="
        self.assertEqual(self.redact(f"call with Bearer {long_token} then"), f"call with Bearer {MASK} then")
        self.assertEqual(self.redact(f"bearer {jwt}"), f"bearer {MASK}")
        self.assertEqual(self.redact(f"Bearer {base64_token}"), f"Bearer {MASK}")
        self.assertEqual(self.redact(f"Authorization: Bearer {base64_token}"), f"Authorization: Bearer {MASK}")
        self.assertEqual(self.redact(f"Authorization: Bearer {jwt}."), f"Authorization: Bearer {MASK}.")
        self.assertEqual(self.redact("Bearer 0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"), f"Bearer {MASK}")
        # The header form is a little looser (16+ chars) than a bare "Bearer" (24+).
        self.assertEqual(self.redact("Authorization: Bearer abcd1234efgh5678"), f"Authorization: Bearer {MASK}")
        self.assertEqual(self.redact("Bearer abcd1234efgh5678"), "Bearer abcd1234efgh5678")

    def test_short_authorization_bearer_value_is_a_known_gap(self):
        """A short, plain value cannot be told from prose and stays readable.

        This is deliberate (same as the upstream project).  Configured secrets are
        still replaced whatever their shape, so the gap only covers tokens the
        server was never configured with.
        """

        self.assertEqual(self.redact("Authorization: Bearer abc123"), "Authorization: Bearer abc123")
        self.assertEqual(self.redact(f"Authorization: Bearer {DART_KEY}"), f"Authorization: Bearer {MASK}")

    def test_configured_secret_is_replaced_before_patterns(self):
        secret = "tossSecret!xyz*9"
        payload = FilingSection(RCEPT, "annual_report", "사업의 내용", "ok",
                                f"https://x.example.test/?client_secret={secret}&a=1", ())
        service = ToolService({"get_filing_section": lambda **_: payload}, secret_values=(secret,))
        result = call(service, "get_filing_section", {"rcept_no": RCEPT, "section": "사업의 내용"})
        self.assertEqual(result["data"]["text"], f"https://x.example.test/?client_secret={MASK}&a=1")
        rendered = repr(result)
        self.assertNotIn("xyz*9", rendered)
        self.assertNotIn("!xyz", rendered)


def _iter_modules():
    for info in pkgutil.walk_packages(autostock_lite.__path__, prefix="autostock_lite."):
        yield importlib.import_module(info.name)


class ErrorCodeRegistryTests(unittest.TestCase):
    def test_every_class_level_code_in_src_is_registered(self):
        """정적 검사: src의 모든 클래스 본문에 있는 code = ... 값이 등록돼 있어야 한다."""

        found = []
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for statement in node.body:
                    targets = statement.targets if isinstance(statement, ast.Assign) else (
                        [statement.target] if isinstance(statement, ast.AnnAssign) else []
                    )
                    if not any(isinstance(target, ast.Name) and target.id == "code" for target in targets):
                        continue
                    value = statement.value
                    where = f"{path.relative_to(SRC)}:{node.name}"
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        found.append((where, value.value))
                    elif (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
                          and value.value.id == "ErrorCode"):
                        self.assertTrue(hasattr(ErrorCode, value.attr), where)
                        found.append((where, getattr(ErrorCode, value.attr).value))
                    else:
                        self.fail(f"{where}: code must be a string or ErrorCode member")
        self.assertGreaterEqual(len(found), 25)
        names = {where.replace("\\", "/") for where, _ in found}
        # Guard the scan itself: these must be among the classes it sees.
        for expected in (
            "dart/client.py:DartNoData",
            "dart/filings.py:FilingArgumentError",
            "dart/ownership.py:OwnershipError",
            "dart/corp_code.py:CorpCodeError",
            "dart/corp_code.py:CorpCodeIndexError",
            "dart/statements.py:StatementsNoData",
            "mcp_runtime.py:FilingContextError",
        ):
            self.assertIn(expected, names)
        for where, code in found:
            with self.subTest(where=where):
                self.assertIn(code, ERROR_CODES)

    def test_every_exception_code_at_runtime_is_registered(self):
        """런타임 검사: 패키지의 모든 예외 클래스가 가진 code가 등록돼 있어야 한다."""

        checked = 0
        for module in _iter_modules():
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if issubclass(cls, BaseException) and cls.__module__.startswith("autostock_lite") and hasattr(cls, "code"):
                    code = cls.code.value if isinstance(cls.code, ErrorCode) else cls.code
                    with self.subTest(cls=f"{cls.__module__}.{cls.__name__}"):
                        self.assertIn(code, ERROR_CODES)
                    checked += 1
        self.assertGreaterEqual(checked, 25)
        self.assertLessEqual(UNAVAILABLE_CODES, ERROR_CODES)

    def test_each_failure_keeps_its_code_through_the_service(self):
        expected = {
            FilingError: ("blocked", "filing-error"),
            FilingSchemaError: ("blocked", "filing-schema-invalid"),
            FilingArchiveError: ("blocked", "filing-archive-invalid"),
            FilingEncodingError: ("blocked", "filing-encoding-invalid"),
            FilingArgumentError: ("blocked", "invalid-argument"),
            FilingContextError: ("blocked", "filing-context-unavailable"),
            InvalidArgumentError: ("blocked", "invalid-argument"),
            OwnershipError: ("blocked", "ownership-error"),
            OwnershipSchemaError: ("blocked", "ownership-schema-invalid"),
            DartNoData: ("blocked", "dart-no-data"),
            DartResponseError: ("blocked", "dart-response-invalid"),
            DartRequestRejected: ("blocked", "dart-request-invalid"),
            DartAuthError: ("unavailable", "dart-auth-failed"),
            DartRateLimited: ("unavailable", "dart-rate-limited"),
            DartMaintenance: ("unavailable", "dart-maintenance"),
            DartLiveError: ("unavailable", "dart-request-failed"),
            CorpCodeError: ("blocked", "dart-response-invalid"),
            CorpCodeDownloadError: ("unavailable", "dart-request-failed"),
            CorpCodeIndexError: ("unavailable", "internal-error"),
            StrictModelError: ("blocked", "toss-response-invalid"),
            TossAuthError: ("unavailable", "toss-auth-failed"),
            TossIpNotAllowed: ("unavailable", "toss-ip-not-allowed"),
            TossLiveIpNotAllowed: ("unavailable", "toss-ip-not-allowed"),
            TossRateLimited: ("unavailable", "toss-rate-limited"),
            TossLiveError: ("unavailable", "toss-request-failed"),
            RuntimeError: ("unavailable", "internal-error"),
        }
        for error, (status, code) in expected.items():
            with self.subTest(error=error.__name__):
                def handler(**_):
                    raise error(f"synthetic failure {DART_KEY}")

                result = call(ToolService({"get_company_profile": handler}, secret_values=(DART_KEY,)), "get_company_profile", {"corp_code": CORP})
                self.assertEqual(result["data"], {"status": status, "error": code})
                self.assertNotIn(DART_KEY, repr(result))


class DartAdapterCodeTests(IsolatedCase):
    """mock HTTP로 DART 업무 상태가 서로 다른 코드로 나오는지 확인한다."""

    def live(self, handler, *, toss=False):
        from autostock_lite.mcp_runtime import create_live_runtime

        runtime = create_live_runtime(self.settings(toss=toss), http_client=self.mock_client(handler), state_dir=self.root / "state")
        self.addCleanup(runtime.close)
        return runtime

    @staticmethod
    def status(value):
        return lambda request: httpx.Response(200, json={"status": value, "message": "합성 응답"})

    def test_no_data_in_profile_and_ownership_adapters(self):
        runtime = self.live(self.status("013"))
        calls = (
            ("get_company_profile", {"corp_code": CORP}),
            ("get_major_shareholders", {"corp_code": CORP, "year": "2025"}),
            ("get_shares_outstanding", {"corp_code": CORP, "year": "2025"}),
            ("get_large_holdings", {"corp_code": CORP}),
            ("get_insider_holdings", {"corp_code": CORP}),
        )
        for name, arguments in calls:
            with self.subTest(name=name):
                result = call(runtime.service, name, arguments)
                self.assertEqual(result["data"], {"status": "blocked", "error": "dart-no-data"})

    def test_business_statuses_map_to_distinct_codes(self):
        for status, expected in (
            ("010", ("unavailable", "dart-auth-failed")), ("011", ("unavailable", "dart-auth-failed")),
            ("012", ("unavailable", "dart-auth-failed")), ("901", ("unavailable", "dart-auth-failed")),
            ("013", ("blocked", "dart-no-data")),
            # 014 is a file-endpoint status; on a JSON endpoint it is a contract break, not "no data".
            ("014", ("blocked", "dart-response-invalid")),
            ("020", ("unavailable", "dart-rate-limited")), ("021", ("blocked", "dart-request-invalid")),
            ("100", ("blocked", "dart-request-invalid")),
            ("101", ("blocked", "dart-request-invalid")), ("800", ("unavailable", "dart-maintenance")),
            ("900", ("unavailable", "dart-request-failed")),
        ):
            with self.subTest(status=status):
                for name, arguments in (
                    ("get_company_profile", {"corp_code": CORP}),
                    ("get_major_shareholders", {"corp_code": CORP, "year": "2025"}),
                ):
                    result = call(self.live(self.status(status)).service, name, arguments)
                    self.assertEqual(result["data"], {"status": expected[0], "error": expected[1]}, name)
                    self.assert_no_secrets(repr(result))

    def test_json_014_is_never_read_as_no_data(self):
        runtime = self.live(self.status("014"))
        result = call(runtime.service, "list_filings", {"corp_code": CORP, "from": "2025-01-01", "to": "2025-12-31"})
        self.assertEqual(result["data"], {"status": "blocked", "error": "dart-response-invalid"})
        result = call(runtime.service, "get_financial_statements", {"corp_code": CORP, "bsns_year": "2025"})
        self.assertEqual(result["data"], {"status": "blocked", "error": "dart-response-invalid"})
        client = DartReadonlyClient(DART_KEY, http_client=self.mock_client(self.status("014")))
        with self.assertRaises(DartResponseError) as caught:
            client.list_disclosures(CORP, begin_date=date(2025, 1, 1), end_date=date(2025, 12, 31))
        self.assertEqual(caught.exception.status, "014")
        self.assertNotIsInstance(caught.exception, DartNoData)
        # 013 on page one is still the ordinary "no filings" / "no statements" answer.
        runtime = self.live(self.status("013"))
        result = call(runtime.service, "list_filings", {"corp_code": CORP, "from": "2025-01-01", "to": "2025-12-31"})
        self.assertEqual(result["data"], [])
        result = call(runtime.service, "get_financial_statements", {"corp_code": CORP, "bsns_year": "2025"})
        self.assertEqual(result["data"], {"status": "blocked", "error": "financials-no-data"})

    def test_transport_and_shape_failures(self):
        cases = (
            (lambda request: httpx.Response(500, text="synthetic"), ("unavailable", "dart-request-failed")),
            (lambda request: httpx.Response(200, text="not json"), ("blocked", "dart-response-invalid")),
            (lambda request: httpx.Response(200, json={"status": "000", "corp_code": "99000002"}), ("blocked", "dart-response-invalid")),
        )
        for handler, expected in cases:
            with self.subTest(expected=expected):
                result = call(self.live(handler).service, "get_company_profile", {"corp_code": CORP})
                self.assertEqual(result["data"], {"status": expected[0], "error": expected[1]})

    def test_list_filings_arguments(self):
        runtime = self.live(self.status("013"))
        result = call(runtime.service, "list_filings", {"corp_code": CORP, "from": "2025-12-31", "to": "2025-01-01"})
        self.assertEqual(result["data"], {"status": "blocked", "error": "invalid-argument"})
        self.assertEqual(self.requests, [])
        for bad_type in ("Z", "a", "AB", ""):
            with self.subTest(type=bad_type), self.assertRaises(ToolArgumentsError):
                call(runtime.service, "list_filings", {"corp_code": CORP, "from": "2025-01-01", "to": "2025-12-31", "type": bad_type})
        self.assertEqual(self.requests, [])
        result = call(runtime.service, "list_filings", {"corp_code": CORP, "from": "2025-01-01", "to": "2025-12-31", "type": "A"})
        self.assertEqual(result["data"], [])
        self.assertEqual(self.requests[0].url.params["pblntf_ty"], "A")

    def test_corp_code_download_failures_on_first_resolve(self):
        cases = (
            (httpx.Response(200, content=b"<?xml version='1.0'?><result><status>010</status><message>synthetic</message></result>"),
             ("unavailable", "dart-auth-failed")),
            (httpx.Response(200, json={"status": "020", "message": "synthetic"}), ("unavailable", "dart-rate-limited")),
            (httpx.Response(200, content=b"<result><status>014</status></result>"), ("blocked", "dart-no-data")),
            (httpx.Response(200, content=b"<result><status>800</status></result>"), ("unavailable", "dart-maintenance")),
            (httpx.Response(200, content=b"<result><status>000</status></result>"), ("blocked", "dart-response-invalid")),
            (httpx.Response(200, content=b"not a zip archive"), ("blocked", "dart-response-invalid")),
            (httpx.Response(503, content=b""), ("unavailable", "dart-request-failed")),
        )
        for response, expected in cases:
            with self.subTest(expected=expected):
                def handler(request, response=response):
                    self.assertEqual(request.url.path, "/api/corpCode.xml")
                    return response

                result = call(self.live(handler).service, "resolve_symbol", {"query": "가상회사"})
                self.assertEqual(result["data"], {"status": expected[0], "error": expected[1]})
                self.assert_no_secrets(repr(result))

    def test_unreadable_local_index_is_internal_error_without_a_request(self):
        runtime = self.live(None)
        runtime.corp_index.replace([CorpCodeRecord(CORP, "가상회사", "000001", "20990101")], refreshed_at="not-a-timestamp")
        result = call(runtime.service, "resolve_symbol", {"query": "가상회사"})
        self.assertEqual(result["data"], {"status": "unavailable", "error": "internal-error"})
        self.assertEqual(self.requests, [])

    def document_runtime(self, response):
        """A runtime whose list.json knows one synthetic annual report and whose document.xml answers ``response``."""

        def handler(request):
            if request.url.path == "/api/list.json":
                return httpx.Response(200, json={
                    "status": "000", "page_no": 1, "page_count": 100, "total_count": 1, "total_page": 1,
                    "list": [{"corp_code": CORP, "rcept_no": RCEPT, "rcept_dt": "20990331", "report_nm": "사업보고서 (2098.12)"}],
                })
            self.assertEqual(request.url.path, "/api/document.xml")
            return response

        runtime = self.live(handler)
        listed = call(runtime.service, "list_filings", {"corp_code": CORP, "from": "2099-01-01", "to": "2099-12-31"})
        self.assertEqual([item["rcept_no"] for item in listed["data"]], [RCEPT])
        return runtime

    def test_document_status_body_maps_to_its_dart_code(self):
        cases = (
            (httpx.Response(200, content=b"<?xml version='1.0' encoding='UTF-8'?><result><status>010</status><message>synthetic</message></result>"),
             ("unavailable", "dart-auth-failed")),
            (httpx.Response(200, json={"status": "020", "message": "synthetic"}), ("unavailable", "dart-rate-limited")),
            (httpx.Response(200, content=b"<result><status>013</status></result>"), ("blocked", "dart-no-data")),
            (httpx.Response(200, content=b"<result><status>014</status><message>synthetic</message></result>"), ("blocked", "dart-no-data")),
            (httpx.Response(200, content=b"<result><status>100</status></result>"), ("blocked", "dart-request-invalid")),
            (httpx.Response(200, content=b"<result><status>800</status></result>"), ("unavailable", "dart-maintenance")),
            (httpx.Response(200, content=b"<result><status>900</status></result>"), ("unavailable", "dart-request-failed")),
            # No readable error status: the archive checks reject it, as upstream does.
            (httpx.Response(200, content=b"<result><status>000</status></result>"), ("blocked", "filing-archive-invalid")),
            (httpx.Response(200, content=b"not a zip and no status"), ("blocked", "filing-archive-invalid")),
            (httpx.Response(503, content=b""), ("unavailable", "dart-request-failed")),
        )
        for response, (status, code) in cases:
            with self.subTest(code=code, body=response.content[:40]):
                runtime = self.document_runtime(response)
                for name, arguments in (
                    ("get_filing_document", {"rcept_no": RCEPT}),
                    ("get_filing_section", {"rcept_no": RCEPT, "section": "사업의 내용"}),
                ):
                    result = call(runtime.service, name, arguments)
                    self.assertEqual(result["data"], {"status": status, "error": code}, name)
                    self.assert_no_secrets(repr(result))

    def test_file_endpoint_014_is_no_data_and_keeps_its_status(self):
        body = b"<result><status>014</status><message>synthetic</message></result>"
        client = DartReadonlyClient(DART_KEY, http_client=self.mock_client(lambda request: httpx.Response(200, content=body)))
        with self.assertRaises(DartNoData) as caught:
            client.get_document(RCEPT)
        self.assertEqual(caught.exception.status, "014")
        self.assertNotIn(DART_KEY, str(caught.exception))
        client = DartReadonlyClient(DART_KEY, http_client=self.mock_client(
            lambda request: httpx.Response(200, json={"status": "020", "message": "synthetic"})))
        with self.assertRaises(DartRateLimited) as caught:
            client.get_document(RCEPT)
        self.assertEqual(caught.exception.status, "020")

    def test_blank_symbol_query_is_invalid_argument(self):
        from datetime import datetime, timezone
        from autostock_lite.dart.corp_code import CorpCodeRecord

        runtime = self.live(None)
        runtime.corp_index.replace([CorpCodeRecord(CORP, "가상회사", "000001", "20260101")],
                                   refreshed_at=datetime.now(timezone.utc).isoformat())
        result = call(runtime.service, "resolve_symbol", {"query": "   "})
        self.assertEqual(result["data"], {"status": "blocked", "error": "invalid-argument"})
        self.assertEqual(self.requests, [])

    def test_filing_document_needs_list_filings_first(self):
        result = call(self.live(self.status("013")).service, "get_filing_document", {"rcept_no": RCEPT})
        self.assertEqual(result["data"], {"status": "blocked", "error": "filing-context-unavailable"})
        self.assertEqual(self.requests, [])

    def test_toss_auth_and_ip_failures(self):
        for status_code, expected in ((401, "toss-auth-failed"), (403, "toss-ip-not-allowed")):
            with self.subTest(status_code=status_code):
                def handler(request, status_code=status_code):
                    self.assertEqual(request.url.path, "/oauth2/token")
                    return httpx.Response(status_code, json={"error": "synthetic"})

                result = call(self.live(handler, toss=True).service, "get_price", {"symbols": ["000001"]})
                self.assertEqual(result["data"], {"status": "unavailable", "error": expected})
                self.assert_no_secrets(repr(result))

    def test_toss_candle_range_order_and_caller_candles(self):
        runtime = self.live(None, toss=True)
        result = call(runtime.service, "get_candles_range", {
            "symbol": "000001", "interval": "1d", "from": "2025-12-31T00:00:00+09:00", "to": "2025-01-01T00:00:00+09:00",
        })
        self.assertEqual(result["data"], {"status": "blocked", "error": "invalid-argument"})
        candles = [
            {"timestamp": "2025-01-01T00:00:00+09:00", "openPrice": "abc", "highPrice": "1", "lowPrice": "1",
             "closePrice": "1", "volume": "1", "currency": "KRW"}
        ] * 252
        result = call(runtime.service, "technical_snapshot", {"candles": candles})
        self.assertEqual(result["data"], {"status": "blocked", "error": "technical-input-invalid"})
        self.assertEqual(self.requests, [])


class DartStatusAttributeTests(unittest.TestCase):
    """Exceptions keep the raw DART status they saw; an explicit None stays None."""

    def test_class_defaults_and_explicit_values(self):
        self.assertEqual(DartNoData("x").status, "013")
        self.assertIsNone(DartNoData("x", status=None).status)
        self.assertEqual(DartNoData("x", status="014").status, "014")
        self.assertIsNone(DartLiveError("x").status)
        self.assertIsNone(DartResponseError("x").status)
        self.assertEqual(str(DartNoData("synthetic message", status="014")), "synthetic message")

    def test_raise_for_dart_status_keeps_the_status_it_read(self):
        raise_for_dart_status("000")
        for status, error in (("010", DartAuthError), ("013", DartNoData), ("014", DartResponseError),
                              ("020", DartRateLimited), ("021", DartRequestRejected), ("800", DartMaintenance)):
            with self.subTest(status=status), self.assertRaises(error) as caught:
                raise_for_dart_status(status)
            self.assertEqual(caught.exception.status, status)
        with self.assertRaises(DartLiveError) as caught:
            raise_for_dart_status("900")
        self.assertIs(type(caught.exception), DartLiveError)
        self.assertEqual(caught.exception.status, "900")

    def test_unreadable_status_is_reported_as_unknown(self):
        for raw in (None, 13, "x" * 20, "\uff10\uff11\uff13", ["013"]):
            with self.subTest(raw=raw), self.assertRaises(DartLiveError) as caught:
                raise_for_dart_status(raw)
            self.assertIs(type(caught.exception), DartLiveError)
            self.assertEqual(caught.exception.status, "unknown")
            self.assertIn("status=unknown", str(caught.exception))


class GrowthBaseTests(unittest.TestCase):
    """YoY는 양수 기준값에서만 백분율을 낸다."""

    def yoy(self, previous, current):
        entry, = growth_metrics([previous, current], ["2024", "2025"])["yoy"]
        return entry

    def test_negative_and_zero_previous_values_have_no_number(self):
        for previous, current in (("-100", "-200"), ("-100", "-50"), ("0", "100"), ("0", "-100"), ("-100", "0"), ("0", "0")):
            with self.subTest(previous=previous, current=current):
                entry = self.yoy(previous, current)
                self.assertEqual(entry["status"], "unavailable")
                self.assertEqual(entry["reason"], "nonpositive-previous-value")
                self.assertNotIn("value", entry)

    def test_sign_changes_are_labelled(self):
        entry = self.yoy("-100", "50")
        self.assertEqual((entry["status"], entry["sign_change"]), ("unavailable", "negative-to-positive"))
        self.assertNotIn("value", entry)
        entry = self.yoy("100", "-50")
        self.assertEqual((entry["status"], entry["value"], entry["sign_change"]), ("ok", "-1.5", "positive-to-negative"))
        for previous, current in (("100", "0"), ("0", "100"), ("-100", "-50"), ("100", "150")):
            with self.subTest(previous=previous, current=current):
                self.assertNotIn("sign_change", self.yoy(previous, current))

    def test_positive_base_unchanged(self):
        self.assertEqual(self.yoy("100", "150"), {"from_period": "2024", "to_period": "2025", "status": "ok", "value": "0.5"})
        entry, = growth_metrics(["-100", "50"], ["2023", "2025"])["yoy"]
        self.assertEqual(entry["reason"], "non-consecutive-periods")
        self.assertNotIn("sign_change", entry)

    def test_through_service(self):
        result = call(ToolService({"growth_metrics": lambda **a: growth_metrics(a["series"], a["periods"])}),
                      "growth_metrics", {"series": ["-100", "-200"], "periods": ["2024", "2025"]})
        self.assertEqual(result["data"]["yoy"][0]["reason"], "nonpositive-previous-value")

    def test_formula_version_matches_the_upstream_rule(self):
        # 1.1 is the upstream label for "no YoY on a non-positive base, plus sign_change".
        self.assertEqual(GROWTH_FORMULA_VERSION, "1.1")
        self.assertEqual(TOOL_REGISTRY["growth_metrics"].reference_value, f"growth-metrics:{GROWTH_FORMULA_VERSION}")
        result = call(ToolService({"growth_metrics": lambda **a: growth_metrics(a["series"], a["periods"])}),
                      "growth_metrics", {"series": ["-100", "-200", "50"], "periods": ["2023", "2024", "2025"]})
        self.assertEqual(result["data"]["formula_version"], GROWTH_FORMULA_VERSION)
        self.assertEqual(result["reference"]["value"], f"growth-metrics:{GROWTH_FORMULA_VERSION}")
        first, second = result["data"]["yoy"]
        self.assertEqual((first["status"], first["reason"]), ("unavailable", "nonpositive-previous-value"))
        self.assertEqual((second["status"], second["sign_change"]), ("unavailable", "negative-to-positive"))
        self.assertNotIn("value", second)


class HttpLoggingTests(unittest.TestCase):
    def test_main_quiets_http_client_loggers(self):
        loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
        saved = [logger.level for logger in loggers]
        self.addCleanup(lambda: [logger.setLevel(level) for logger, level in zip(loggers, saved)])
        for logger in loggers:
            logger.setLevel(logging.NOTSET)
        with patch.object(cli, "doctor", return_value=0) as fake_doctor:
            self.assertEqual(cli.main(["doctor"]), 0)
        fake_doctor.assert_called_once()
        for logger in loggers:
            self.assertEqual(logger.level, logging.WARNING)
            self.assertFalse(logger.isEnabledFor(logging.INFO))


if __name__ == "__main__":
    unittest.main()
