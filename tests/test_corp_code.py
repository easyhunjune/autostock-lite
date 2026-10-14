"""corpCode.xml 내려받기 → 파싱 → SQLite 색인 → 주간 갱신을 합성 ZIP과 주입 시계로 검증한다.

회사 고유번호(99로 시작), 회사명, 종목코드는 모두 지어낸 값이다.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import httpx

from autostock_lite import paths
from autostock_lite.dart import corp_code
from autostock_lite.dart.client import DartAuthError
from autostock_lite.dart.corp_code import (
    CorpCodeDownloadError, CorpCodeError, CorpCodeIndex, CorpCodeIndexError,
    CorpCodeRecord, CorpCodeRefreshService, parse_corp_code_archive,
)
from autostock_lite.mcp_runtime import create_live_runtime
from test_smoke import DART_KEY, IsolatedCase


NOW = datetime(2099, 1, 1, tzinfo=timezone.utc)
LISTED = ("99000001", "가상전자", "000001", "20981201")
ALPHANUMERIC = ("99000002", "가상바이오", "9999A9", "20981202")
DELISTED = ("99000003", "가상폐지", "000003", "20100101")
UNLISTED = ("99000004", "가상비상장", " ", "20981203")


def corp_xml(entries):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<result>']
    for code, name, stock, modified in entries:
        parts.append(
            f"<list><corp_code>{code}</corp_code><corp_name>{name}</corp_name>"
            f"<corp_eng_name>Synthetic {code}</corp_eng_name><stock_code>{stock}</stock_code>"
            f"<modify_date>{modified}</modify_date></list>"
        )
    parts.append("</result>")
    return "\n".join(parts).encode("utf-8")


def corp_zip(entries=(LISTED, ALPHANUMERIC, DELISTED, UNLISTED), *, members=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in ({"CORPCODE.xml": corp_xml(entries)} if members is None else members).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def record(entry):
    code, name, stock, modified = entry
    return CorpCodeRecord(code, name, stock, modified)


class FakeHttp:
    """CorpCodeHttpClient 프로토콜: 준비한 응답을 차례로 돌려주고 호출을 기록한다."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, params, headers):
        self.calls.append((url, dict(params), dict(headers)))
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def ok(content):
    return SimpleNamespace(status_code=200, content=content)


class ParseTests(unittest.TestCase):
    def test_listed_records_are_kept_and_unlisted_dropped(self):
        records = parse_corp_code_archive(corp_zip())
        self.assertEqual(records, (record(LISTED), record(ALPHANUMERIC), record(DELISTED)))
        # 오래전에 바뀐(상장폐지된) 회사도 종목코드가 있으면 색인에 남는다. 상장 여부는 조회 때 판정한다.
        self.assertIn(record(DELISTED), records)

    def test_blank_modify_date_is_allowed(self):
        records = parse_corp_code_archive(corp_zip([("99000005", "가상무변경", "000005", "")]))
        self.assertEqual(records, (CorpCodeRecord("99000005", "가상무변경", "000005", ""),))

    def test_invalid_records_fail_closed(self):
        cases = {
            "lowercase stock code": [("99000001", "가상", "9999a9", "20981201")],
            "seven-character stock code": [("99000001", "가상", "0000001", "20981201")],
            "symbol in stock code": [("99000001", "가상", "0000-1", "20981201")],
            "short corp code": [("9900001", "가상", "000001", "20981201")],
            "letter in corp code": [("9900000A", "가상", "000001", "20981201")],
            "blank name": [("99000001", " ", "000001", "20981201")],
            "dashed modify date": [("99000001", "가상", "000001", "2098-12-01")],
            "duplicate corp code": [LISTED, ("99000001", "가상 둘", "000002", "20981201")],
            "duplicate unlisted corp code": [UNLISTED, UNLISTED],
        }
        for label, entries in cases.items():
            with self.subTest(label), self.assertRaises(CorpCodeError) as caught:
                parse_corp_code_archive(corp_zip(entries))
            self.assertIs(type(caught.exception), CorpCodeError)
            self.assertEqual(caught.exception.code.value, "dart-response-invalid")

    def test_archive_shape_is_fixed(self):
        cases = {
            "empty bytes": b"",
            "not a zip": b"not a zip archive",
            "wrong member name": corp_zip(members={"corpcode.xml": corp_xml([LISTED])}),
            "extra member": corp_zip(members={"CORPCODE.xml": corp_xml([LISTED]), "notes.txt": b"synthetic"}),
            "empty member": corp_zip(members={"CORPCODE.xml": b""}),
            "no members": corp_zip(members={}),
            "malformed xml": corp_zip(members={"CORPCODE.xml": b"<result><list><corp_code>99000001</list>"}),
            "highly compressed": corp_zip(members={"CORPCODE.xml": corp_xml([LISTED]) + b" " * 1_000_000}),
        }
        for label, payload in cases.items():
            with self.subTest(label), self.assertRaises(CorpCodeError):
                parse_corp_code_archive(payload)
        payload = corp_zip()
        with patch.object(corp_code, "MAX_ARCHIVE_BYTES", len(payload) - 1), self.assertRaises(CorpCodeError):
            parse_corp_code_archive(payload)
        with patch.object(corp_code, "MAX_ARCHIVE_BYTES", len(payload)):
            self.assertEqual(len(parse_corp_code_archive(payload)), 3)


class IndexTests(IsolatedCase):
    def test_index_lives_where_it_is_told_and_persists(self):
        path = self.root / "state" / "nested" / "corp_codes.sqlite3"
        index = CorpCodeIndex(path)
        self.assertTrue(path.is_file())
        self.assertEqual((index.all(), index.refreshed_at()), ((), None))
        index.replace([record(DELISTED), record(ALPHANUMERIC), record(LISTED)], refreshed_at=NOW.isoformat())
        reopened = CorpCodeIndex(path)
        self.assertEqual(reopened.all(), (record(LISTED), record(DELISTED), record(ALPHANUMERIC)))
        self.assertEqual(reopened.refreshed_at(), NOW.isoformat())

    def test_replace_is_a_full_swap(self):
        index = CorpCodeIndex(self.root / "index.sqlite3")
        index.replace([record(LISTED), record(DELISTED)], refreshed_at=NOW.isoformat())
        later = (NOW + timedelta(days=8)).isoformat()
        index.replace([record(ALPHANUMERIC)], refreshed_at=later)
        self.assertEqual((index.all(), index.refreshed_at()), ((record(ALPHANUMERIC),), later))

    def test_bad_snapshots_leave_the_old_index(self):
        index = CorpCodeIndex(self.root / "index.sqlite3")
        index.replace([record(LISTED)], refreshed_at=NOW.isoformat())
        for label, rows in (
            ("empty", []),
            ("duplicate stock code", [record(ALPHANUMERIC), CorpCodeRecord("99000009", "가상 복제", "9999A9", "20981201")]),
            ("duplicate corp code", [record(ALPHANUMERIC), CorpCodeRecord("99000002", "가상 복제", "000009", "20981201")]),
        ):
            with self.subTest(label), self.assertRaises(CorpCodeError):
                index.replace(rows, refreshed_at="2100-01-01T00:00:00+00:00")
            self.assertEqual((index.all(), index.refreshed_at()), ((record(LISTED),), NOW.isoformat()))

    def test_refresh_from_archive(self):
        index = CorpCodeIndex(self.root / "index.sqlite3")
        self.assertEqual(index.refresh_from_archive(corp_zip(), refreshed_at=NOW.isoformat()), 3)
        self.assertEqual([item.stock_code for item in index.all()], ["000001", "000003", "9999A9"])
        with self.assertRaises(CorpCodeError):
            index.refresh_from_archive(corp_zip([UNLISTED]), refreshed_at="2100-01-01T00:00:00+00:00")
        self.assertEqual(len(index.all()), 3)


class RefreshServiceTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.now = NOW
        self.index = CorpCodeIndex(self.root / "state" / "corp_codes.sqlite3")

    def service(self, *responses, api_key=DART_KEY):
        http = FakeHttp(*responses)
        return CorpCodeRefreshService(self.index, api_key=api_key, http_client=http, now=lambda: self.now), http

    def test_first_refresh_downloads_and_records_the_time(self):
        service, http = self.service(ok(corp_zip()))
        result = service.refresh_if_due()
        self.assertEqual((result.refreshed, result.record_count, result.refreshed_at), (True, 3, NOW))
        self.assertEqual(http.calls, [(CorpCodeRefreshService.URL, {"crtfc_key": DART_KEY}, {"Accept": "application/zip"})])
        self.assertEqual(self.index.refreshed_at(), NOW.isoformat())
        self.assertEqual([item.stock_code for item in self.index.all()], ["000001", "000003", "9999A9"])

    def test_weekly_staleness(self):
        service, http = self.service(ok(corp_zip()), ok(corp_zip([LISTED])))
        service.refresh_if_due()
        self.now = NOW + timedelta(days=7) - timedelta(seconds=1)
        result = service.refresh_if_due()
        self.assertEqual((result.refreshed, result.record_count, result.refreshed_at), (False, 3, NOW))
        self.assertEqual(len(http.calls), 1)
        self.now = NOW + timedelta(days=7)
        result = service.refresh_if_due()
        self.assertEqual((result.refreshed, result.record_count), (True, 1))
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(self.index.refreshed_at(), self.now.isoformat())

    def test_force_and_offset_clocks(self):
        service, http = self.service(ok(corp_zip()), ok(corp_zip([LISTED])))
        service.refresh_if_due()
        self.assertTrue(service.refresh_if_due(force=True).refreshed)
        self.assertEqual(len(http.calls), 2)
        # 다른 시간대의 같은 순간도 UTC로 비교한다.
        self.now = (NOW + timedelta(days=1)).astimezone(timezone(timedelta(hours=9)))
        self.assertFalse(service.refresh_if_due().refreshed)
        self.assertEqual(len(http.calls), 2)

    def test_z_suffix_timestamp_is_read(self):
        self.index.replace([record(LISTED)], refreshed_at="2099-01-01T00:00:00Z")
        service, http = self.service()
        self.now = NOW + timedelta(days=3)
        result = service.refresh_if_due()
        self.assertEqual((result.refreshed, result.record_count, result.refreshed_at), (False, 1, NOW))
        self.assertEqual(http.calls, [])

    def test_failures_keep_the_previous_snapshot(self):
        service, http = self.service(
            ok(corp_zip()),
            ok(corp_zip([UNLISTED])),
            ok(b"not a zip archive"),
            SimpleNamespace(status_code=500, content=b""),
            httpx.ConnectError("synthetic"),
            ok(b"<result><status>010</status><message>synthetic</message></result>"),
        )
        service.refresh_if_due()
        self.now = NOW + timedelta(days=8)
        for expected in (CorpCodeError, CorpCodeError, CorpCodeDownloadError, CorpCodeDownloadError, DartAuthError):
            with self.subTest(expected=expected.__name__), self.assertRaises(expected) as caught:
                service.refresh_if_due()
            self.assertNotIn(DART_KEY, str(caught.exception))
            self.assertEqual(self.index.refreshed_at(), NOW.isoformat())
            self.assertEqual(len(self.index.all()), 3)
        self.assertEqual(http.responses, [])

    def test_clock_and_key_contract(self):
        service, http = self.service()
        self.now = datetime(2099, 1, 1)
        with self.assertRaises(ValueError):
            service.refresh_if_due()
        self.assertEqual(http.calls, [])
        with self.assertRaises(ValueError):
            self.service(api_key="")
        self.index.replace([record(LISTED)], refreshed_at="last week")
        self.now = NOW
        with self.assertRaises(CorpCodeIndexError):
            service.refresh_if_due()
        self.assertEqual(http.calls, [])


class RuntimeIndexTests(IsolatedCase):
    """resolve_symbol은 처음 쓸 때 색인을 받아 상태 디렉터리에 두고, 일주일 동안 다시 받지 않는다."""

    CORP_CLS = {"99000001": "Y", "99000002": "K", "99000003": "E"}

    def handler(self, request):
        if request.url.path == "/api/corpCode.xml":
            self.assertEqual(request.url.params["crtfc_key"], DART_KEY)
            return httpx.Response(200, content=corp_zip())
        self.assertEqual(request.url.path, "/api/company.json")
        code = request.url.params["corp_code"]
        return httpx.Response(200, json={"status": "000", "message": "정상", "corp_code": code, "corp_cls": self.CORP_CLS[code]})

    def resolve(self, runtime, query):
        result = asyncio.run(runtime.service.call("resolve_symbol", {"query": query}))
        self.assert_no_secrets(repr(result))
        return result["data"]

    def test_first_resolve_builds_the_index_under_the_state_dir(self):
        state = self.root / "state"
        runtime = create_live_runtime(self.settings(), http_client=self.mock_client(self.handler), state_dir=state)
        self.addCleanup(runtime.close)
        self.assertEqual(runtime.corp_index.path, paths.corp_code_db_path(state))
        self.assertEqual(runtime.corp_index.all(), ())

        candidates = self.resolve(runtime, "가상")
        by_symbol = {item["symbol"]: item for item in candidates}
        self.assertEqual(set(by_symbol), {"000001", "9999A9", "000003"})
        self.assertEqual((by_symbol["000001"]["market"], by_symbol["000001"]["eligible"]), ("KOSPI", True))
        self.assertEqual((by_symbol["9999A9"]["market"], by_symbol["9999A9"]["eligible"]), ("KOSDAQ", True))
        self.assertEqual((by_symbol["000003"]["eligible"], by_symbol["000003"]["reason"]), (False, "not-listed"))
        self.assertTrue((state / "corp_codes.sqlite3").is_file())
        self.assertEqual(len(runtime.corp_index.all()), 3)
        downloads = [request for request in self.requests if request.url.path == "/api/corpCode.xml"]
        self.assertEqual(len(downloads), 1)

        before = len(self.requests)
        candidate, = self.resolve(runtime, "9999a9")
        self.assertEqual((candidate["symbol"], candidate["corp_code"], candidate["confidence"]), ("9999A9", "99000002", 100.0))
        self.assertEqual(len(self.requests), before)

    def test_fresh_index_from_an_earlier_session_is_reused(self):
        state = self.root / "state"
        CorpCodeIndex(paths.corp_code_db_path(state)).replace(
            [record(LISTED)], refreshed_at=datetime.now(timezone.utc).isoformat(),
        )
        runtime = create_live_runtime(self.settings(), http_client=self.mock_client(self.handler), state_dir=state)
        self.addCleanup(runtime.close)
        candidate, = self.resolve(runtime, "000001")
        self.assertEqual(candidate["name"], "가상전자")
        self.assertEqual([request.url.path for request in self.requests], ["/api/company.json"])


if __name__ == "__main__":
    unittest.main()
