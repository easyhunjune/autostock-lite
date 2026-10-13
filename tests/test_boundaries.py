"""응답 마스킹, 오류 코드 등록, 성장률 기준값, 로깅 경계를 합성 데이터로 검증한다."""

import ast
import asyncio
import importlib
import inspect
import logging
from pathlib import Path
import pkgutil
import unittest
from unittest.mock import patch

import httpx

import autostock_research
from autostock_research import cli
from autostock_research.calc.growth import growth_metrics
from autostock_research.dart.client import (
    DartAuthError, DartLiveError, DartMaintenance, DartNoData, DartRateLimited,
    DartRequestRejected, DartResponseError,
)
from autostock_research.dart.filings import (
    FilingArchiveError, FilingArgumentError, FilingEncodingError, FilingError,
    FilingSchemaError, FilingSection,
)
from autostock_research.dart.ownership import OwnershipError, OwnershipSchemaError
from autostock_research.mcp_server import ToolArgumentsError, ToolService
from autostock_research.provenance import MASK
from autostock_research.status_codes import (
    ERROR_CODES, UNAVAILABLE_CODES, ErrorCode, InvalidArgumentError,
)
from autostock_research.toss.auth import TossAuthError, TossIpNotAllowed
from autostock_research.toss.live import TossLiveError, TossLiveIpNotAllowed, TossRateLimited
from autostock_research.toss.models import StrictModelError
from test_smoke import DART_KEY, ROOT, TOSS_SECRET, IsolatedCase


SRC = ROOT / "src" / "autostock_research"
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
        text = (
            "요청 https://opendart.example.test/api/list.json?corp_code=99000001&crtfc_key=unconfigured0001&page_no=1\n"
            "헤더 Authorization: Bearer abc.DEF-123_456\n"
            "The bearer bond stays readable."
        )
        expected = (
            f"요청 https://opendart.example.test/api/list.json?corp_code=99000001&crtfc_key={MASK}&page_no=1\n"
            f"헤더 Authorization: Bearer {MASK}\n"
            "The bearer bond stays readable."
        )
        result = call(self.service(section(text)), "get_filing_section", self.arguments)
        self.assertEqual(result["data"]["text"], expected)

    def test_sensitive_keys_are_masked(self):
        payload = {"crtfc_key": "unconfigured0001", "Client-Secret": "x", "corp_code": CORP}
        result = call(self.service(payload), "get_filing_section", self.arguments)
        self.assertEqual(result["data"], {"crtfc_key": MASK, "Client-Secret": MASK, "corp_code": CORP})


def _iter_modules():
    for info in pkgutil.walk_packages(autostock_research.__path__, prefix="autostock_research."):
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
        for where, code in found:
            with self.subTest(where=where):
                self.assertIn(code, ERROR_CODES)

    def test_every_exception_code_at_runtime_is_registered(self):
        """런타임 검사: 패키지의 모든 예외 클래스가 가진 code가 등록돼 있어야 한다."""

        checked = 0
        for module in _iter_modules():
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if issubclass(cls, BaseException) and cls.__module__.startswith("autostock_research") and hasattr(cls, "code"):
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
        from autostock_research.mcp_runtime import create_live_runtime

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
            ("020", ("unavailable", "dart-rate-limited")), ("100", ("blocked", "dart-request-invalid")),
            ("101", ("blocked", "dart-request-invalid")), ("800", ("unavailable", "dart-maintenance")),
            ("900", ("unavailable", "dart-request-failed")),
        ):
            with self.subTest(status=status):
                result = call(self.live(self.status(status)).service, "get_company_profile", {"corp_code": CORP})
                self.assertEqual(result["data"], {"status": expected[0], "error": expected[1]})
                self.assert_no_secrets(repr(result))

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

    def test_blank_symbol_query_is_invalid_argument(self):
        from datetime import datetime, timezone
        from autostock_research.dart.corp_code import CorpCodeRecord

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


class GrowthBaseTests(unittest.TestCase):
    """YoY는 양수 기준값에서만 백분율을 낸다."""

    def yoy(self, previous, current):
        entry, = growth_metrics([previous, current], ["2024", "2025"])["yoy"]
        return entry

    def test_negative_and_zero_previous_values_have_no_number(self):
        for previous, current in (("-100", "-200"), ("-100", "-50"), ("0", "100"), ("0", "-100"), ("-100", "0")):
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

    def test_positive_base_unchanged(self):
        self.assertEqual(self.yoy("100", "150"), {"from_period": "2024", "to_period": "2025", "status": "ok", "value": "0.5"})
        entry, = growth_metrics(["-100", "50"], ["2023", "2025"])["yoy"]
        self.assertEqual(entry["reason"], "non-consecutive-periods")
        self.assertNotIn("sign_change", entry)

    def test_through_service(self):
        result = call(ToolService({"growth_metrics": lambda **a: growth_metrics(a["series"], a["periods"])}),
                      "growth_metrics", {"series": ["-100", "-200"], "periods": ["2024", "2025"]})
        self.assertEqual(result["data"]["yoy"][0]["reason"], "nonpositive-previous-value")


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
