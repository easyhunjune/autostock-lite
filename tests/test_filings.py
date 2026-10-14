"""공시 목록·원문·절·비교 도구를 손으로 만든 합성 응답으로만 검증한다.

회사 고유번호, 접수번호(2099년), 보고서명, 본문은 모두 지어낸 값이다.
"""

import asyncio
from datetime import date
import io
import unittest
from unittest.mock import patch
import zipfile

import httpx

from autostock_lite.dart import filings
from autostock_lite.dart.client import DartReadonlyClient, DartResponseError
from autostock_lite.dart.filings import (
    DOC_ANNUAL, DOC_QUARTERLY, DOC_SECURITIES, DOC_UNKNOWN,
    Filing, FilingArchiveError, FilingArgumentError, FilingDocument, FilingEncodingError,
    FilingError, FilingSchemaError, classify_doc_type, diff_filing_sections,
    get_filing_document, get_filing_section, list_filings, resolve_current_filings,
)
from autostock_lite.mcp_runtime import create_live_runtime
from autostock_lite.mcp_server import ToolArgumentsError
from autostock_lite.provenance import dart_public_url
from test_smoke import DART_KEY, IsolatedCase


CORP = "99000001"
ANNUAL = "20990331000001"
ANNUAL_FIX = "20990415000001"
HALF = "20990814000001"
OTHER = "20990901000001"
REGISTRATION = "20991001000001"


def call(service, name, arguments):
    return asyncio.run(service.call(name, arguments))


def row(rcept_no, rcept_dt, report_nm):
    return {"corp_code": CORP, "corp_name": "가상회사", "rcept_no": rcept_no, "rcept_dt": rcept_dt, "report_nm": report_nm}


ANNUAL_ROW = row(ANNUAL, "20990331", "사업보고서 (2098.12)")
ANNUAL_FIX_ROW = row(ANNUAL_FIX, "20990415", "[기재정정]사업보고서 (2098.12)")
HALF_ROW = row(HALF, "20990814", "반기보고서 (2099.06)")
OTHER_ROW = row(OTHER, "20990901", "주요사항보고서(자기주식취득결정)")
REGISTRATION_ROW = row(REGISTRATION, "20991001", "증권신고서(지분증권)")


def page(rows, *, page_no=1, total_count=None, total_page=1, page_count=100, status="000"):
    return {
        "status": status, "message": "합성 응답", "page_no": page_no, "page_count": page_count,
        "total_count": len(rows) if total_count is None else total_count, "total_page": total_page,
        "list": rows,
    }


def zipped(members, *, compression=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def document_xml(body, *, encoding="utf-8"):
    return f'<?xml version="1.0" encoding="{encoding}"?>\n<DOCUMENT>\n{body}\n</DOCUMENT>\n'.encode(encoding)


def business_section(prose, table_amount="1,000"):
    """사업보고서 한 부: II장에 산문과 표가 있고 III장에서 끝난다."""

    return document_xml(
        "<TITLE>사업보고서</TITLE>\n"
        "<SECTION-1><TITLE>I. 회사의 개요</TITLE><P>가상회사는 2099년에 설립되었다.</P></SECTION-1>\n"
        f"<SECTION-1><TITLE>II. 사업의 내용</TITLE><P>{prose}</P>\n"
        f"<TABLE><TR><TH>구분</TH><TH>매출</TH></TR><TR><TD>부품</TD><TD>{table_amount}</TD></TR></TABLE>\n"
        "</SECTION-1>\n"
        "<SECTION-1><TITLE>III. 재무에 관한 사항</TITLE><P>재무 내용.</P></SECTION-1>"
    )


ANNUAL_BODY = (
    "<TITLE>사업보고서</TITLE>\n"
    "<SECTION-1><TITLE>I. 회사의 개요</TITLE>\n<P>가상회사는 2099년에 설립되었다.</P>\n</SECTION-1>\n"
    "<SECTION-1><TITLE>II. 사업의 내용</TITLE>\n"
    "<SECTION-2><TITLE>1. 사업의 개요</TITLE>\n"
    "<P>당사는 합성 부품을 만든다. 주요 고객은 가상 고객사다.</P>\n"
    "<TABLE><TR><TH>구분</TH><TH>매출</TH></TR><TR><TD>부품|A</TD><TE>1,000</TE></TR><TR><TD></TD><TD></TD></TR></TABLE>\n"
    "<P>수출 비중은 절반이다.</P>\n"
    "</SECTION-2>\n"
    "<SECTION-2><TITLE>2. 주요 제품</TITLE>\n<P>제품은 하나다.</P>\n</SECTION-2>\n"
    "</SECTION-1>\n"
    "<SECTION-1><TITLE>III. 재무에 관한 사항</TITLE>\n<P>재무 내용.</P>\n</SECTION-1>"
)
ANNUAL_TEXT = (
    "사업보고서\nI. 회사의 개요\n가상회사는 2099년에 설립되었다.\nII. 사업의 내용\n1. 사업의 개요\n"
    "당사는 합성 부품을 만든다. 주요 고객은 가상 고객사다.\n| 구분 | 매출 |\n| 부품\\|A | 1,000 |\n"
    "수출 비중은 절반이다.\n2. 주요 제품\n제품은 하나다.\nIII. 재무에 관한 사항\n재무 내용."
)
BUSINESS_TEXT = (
    "1. 사업의 개요\n당사는 합성 부품을 만든다. 주요 고객은 가상 고객사다.\n| 구분 | 매출 |\n"
    "| 부품\\|A | 1,000 |\n수출 비중은 절반이다.\n2. 주요 제품\n제품은 하나다."
)
REGISTRATION_BODY = (
    "<TITLE>증권신고서</TITLE>\n"
    "<SECTION-1><TITLE>I. 투자위험요소</TITLE>\n"
    "<SECTION-2><TITLE>1. 사업위험</TITLE><P>합성 수요가 줄 수 있다.</P></SECTION-2>\n"
    "<SECTION-2><TITLE>2. 회사위험</TITLE><P>가상 공장이 하나뿐이다.</P></SECTION-2>\n"
    "</SECTION-1>"
)


class FakeReader:
    """FilingReader 프로토콜: 공시목록과 document.xml 바이트를 그대로 돌려준다."""

    def __init__(self, rows=(), payload=b""):
        self.rows = list(rows)
        self.payload = payload
        self.calls = []

    def list_disclosures(self, corp_code, *, begin_date, end_date, disclosure_type=None):
        self.calls.append(("list", corp_code, begin_date, end_date, disclosure_type))
        return list(self.rows)

    def get_document(self, rcept_no):
        self.calls.append(("document", rcept_no))
        return self.payload


def filing_for(report_row):
    filing, = resolve_current_filings([report_row])
    return filing


def read(payload, report_row=ANNUAL_ROW):
    return get_filing_document(FakeReader(payload=payload), filing=filing_for(report_row))


def annual_document(text):
    return FilingDocument(ANNUAL, DOC_ANNUAL, text, "utf-8", ("d.xml",))


class ClassificationTests(unittest.TestCase):
    def test_report_names_map_to_document_families(self):
        for report_nm, expected in (
            ("사업보고서 (2098.12)", DOC_ANNUAL),
            ("[기재정정]사업보고서 (2098.12)", DOC_ANNUAL),
            ("반기보고서 (2099.06)", DOC_QUARTERLY),
            ("분기보고서 (2099.03)", DOC_QUARTERLY),
            ("증권신고서(지분증권)", DOC_SECURITIES),
            ("[정정] 증권신고서 (채무증권)", DOC_SECURITIES),
            ("주요사항보고서(자기주식취득결정)", DOC_UNKNOWN),
            ("임원ㆍ주요주주특정증권등소유상황보고서", DOC_UNKNOWN),
        ):
            with self.subTest(report_nm=report_nm):
                self.assertEqual(classify_doc_type(report_nm), expected)

    def test_corrections_share_a_family_and_only_the_latest_is_current(self):
        resolved = resolve_current_filings([HALF_ROW, ANNUAL_FIX_ROW, ANNUAL_ROW])
        self.assertEqual([item.rcept_no for item in resolved], [ANNUAL, ANNUAL_FIX, HALF])
        by_no = {item.rcept_no: item for item in resolved}
        self.assertEqual((by_no[ANNUAL].original_rcept_no, by_no[ANNUAL].is_current), (ANNUAL, False))
        self.assertEqual((by_no[ANNUAL_FIX].original_rcept_no, by_no[ANNUAL_FIX].is_current), (ANNUAL, True))
        self.assertEqual((by_no[HALF].original_rcept_no, by_no[HALF].is_current), (HALF, True))
        self.assertEqual(by_no[ANNUAL_FIX].doc_type, DOC_ANNUAL)
        self.assertEqual(by_no[HALF].doc_type, DOC_QUARTERLY)
        self.assertEqual(by_no[ANNUAL_FIX].raw, ANNUAL_FIX_ROW)

    def test_unreadable_list_rows_are_schema_errors(self):
        for label, bad in (
            ("short receipt", dict(ANNUAL_ROW, rcept_no="2099033100000")),
            ("receipt not digits", dict(ANNUAL_ROW, rcept_no="2099-0331-0001")),
            ("dashed date", dict(ANNUAL_ROW, rcept_dt="2099-03-31")),
            ("blank name", dict(ANNUAL_ROW, report_nm="  ")),
            ("missing name", {k: v for k, v in ANNUAL_ROW.items() if k != "report_nm"}),
        ):
            with self.subTest(label), self.assertRaises(FilingSchemaError):
                resolve_current_filings([bad])

    def test_only_validated_filings_are_read(self):
        reader = FakeReader(payload=zipped({"d.xml": document_xml(ANNUAL_BODY)}))
        listed = filing_for(ANNUAL_ROW)
        for label, filing in (
            ("unknown family", filing_for(OTHER_ROW)),
            ("doc type relabelled", Filing(OTHER, OTHER_ROW["rcept_dt"], OTHER_ROW["report_nm"], DOC_ANNUAL, OTHER, True, OTHER_ROW)),
            ("identity differs from raw", Filing(OTHER, listed.rcept_dt, listed.report_nm, DOC_ANNUAL, OTHER, True, ANNUAL_ROW)),
        ):
            with self.subTest(label), self.assertRaises(FilingSchemaError):
                get_filing_document(reader, filing=filing)
        self.assertEqual(reader.calls, [])


class ListFilingsArgumentTests(unittest.TestCase):
    def test_both_date_spellings_reach_the_reader_as_dates(self):
        for from_date, to_date in (("2099-01-01", "2099-12-31"), ("20990101", "20991231"), ("2099-01-01", "20991231")):
            with self.subTest(from_date=from_date, to_date=to_date):
                reader = FakeReader([ANNUAL_ROW])
                result = list_filings(reader, corp_code=CORP, from_date=from_date, to_date=to_date)
                self.assertEqual([item.rcept_no for item in result], [ANNUAL])
                self.assertEqual(reader.calls, [("list", CORP, date(2099, 1, 1), date(2099, 12, 31), None)])

    def test_same_day_range_and_type_filter(self):
        reader = FakeReader()
        self.assertEqual(list_filings(reader, corp_code=CORP, from_date="2099-03-31", to_date="20990331", filing_type="A"), ())
        self.assertEqual(reader.calls, [("list", CORP, date(2099, 3, 31), date(2099, 3, 31), "A")])

    def test_invalid_arguments_never_reach_the_reader(self):
        cases = (
            {"from_date": "2099-12-31", "to_date": "2099-01-01"},
            {"from_date": "20991231", "to_date": "20990101"},
            {"from_date": "2099-02-30", "to_date": "2099-12-31"},
            {"from_date": "2099-01-01", "to_date": "20991301"},
            {"from_date": "2099-1-1", "to_date": "2099-12-31"},
            {"from_date": "2099.01.01", "to_date": "2099-12-31"},
            {"from_date": 20990101, "to_date": "2099-12-31"},
            {"from_date": "2099-01-01", "to_date": "2099-12-31", "filing_type": "Z"},
            {"from_date": "2099-01-01", "to_date": "2099-12-31", "filing_type": "a"},
            {"from_date": "2099-01-01", "to_date": "2099-12-31", "filing_type": ""},
        )
        for arguments in cases:
            reader = FakeReader([ANNUAL_ROW])
            with self.subTest(arguments=arguments), self.assertRaises(FilingArgumentError) as caught:
                list_filings(reader, corp_code=CORP, **arguments)
            self.assertEqual(caught.exception.code.value, "invalid-argument")
            self.assertEqual(reader.calls, [])


class ListDisclosuresPagingTests(IsolatedCase):
    """list.json은 한 페이지에 100건이다. 모든 페이지와 total_count를 대조해야 한다."""

    BEGIN, END = date(2099, 1, 1), date(2099, 12, 31)

    def client(self, pages):
        def handler(request):
            self.assertEqual(request.url.path, "/api/list.json")
            return httpx.Response(200, json=pages[int(request.url.params["page_no"])])

        return DartReadonlyClient(DART_KEY, http_client=self.mock_client(handler))

    @staticmethod
    def many(count, rcept_dt="20990331"):
        return [row(f"2099033100{number:04d}", rcept_dt, "기타 합성 공시") for number in range(count)]

    def test_two_pages_are_read_completely(self):
        rows = self.many(101)
        client = self.client({
            1: page(rows[:100], page_no=1, total_count=101, total_page=2),
            2: page(rows[100:], page_no=2, total_count=101, total_page=2),
        })
        result = client.list_disclosures(CORP, begin_date=self.BEGIN, end_date=self.END, disclosure_type="A")
        self.assertEqual([item["rcept_no"] for item in result], [item["rcept_no"] for item in rows])
        self.assertEqual([request.url.params["page_no"] for request in self.requests], ["1", "2"])
        for request in self.requests:
            params = request.url.params
            self.assertEqual(
                (params["corp_code"], params["bgn_de"], params["end_de"], params["last_reprt_at"], params["page_count"], params["pblntf_ty"]),
                (CORP, "20990101", "20991231", "N", "100", "A"),
            )

    def test_no_type_filter_is_not_sent(self):
        client = self.client({1: page([ANNUAL_ROW])})
        self.assertEqual(len(client.list_disclosures(CORP, begin_date=self.BEGIN, end_date=self.END)), 1)
        self.assertNotIn("pblntf_ty", self.requests[0].url.params)

    def test_no_data_only_on_the_first_page(self):
        client = self.client({1: {"status": "013", "message": "합성 응답"}})
        self.assertEqual(client.list_disclosures(CORP, begin_date=self.BEGIN, end_date=self.END), [])
        rows = self.many(101)
        client = self.client({
            1: page(rows[:100], page_no=1, total_count=101, total_page=2),
            2: {"status": "013", "message": "합성 응답"},
        })
        with self.assertRaises(DartResponseError):
            client.list_disclosures(CORP, begin_date=self.BEGIN, end_date=self.END)

    def test_reversed_range_returns_nothing_without_a_request(self):
        client = self.client({})
        self.assertEqual(client.list_disclosures(CORP, begin_date=self.END, end_date=self.BEGIN), [])
        self.assertEqual(self.requests, [])

    def test_inconsistent_pages_fail_closed(self):
        one, two = self.many(2)
        cases = {
            "fewer rows than total": {1: page([one], total_count=2)},
            "more rows than total": {1: page([one, two], total_count=1)},
            "duplicate receipt across pages": {
                1: page([one], page_no=1, total_count=2, total_page=2),
                2: page([one], page_no=2, total_count=2, total_page=2),
            },
            "total pages change": {
                1: page([one], page_no=1, total_count=2, total_page=2),
                2: page([two], page_no=2, total_count=2, total_page=3),
            },
            "page number echo differs": {1: page([one], page_no=2)},
            "page size echo differs": {1: page([one], page_count=10)},
            "too many pages": {1: page([one], total_count=1, total_page=101)},
            "missing paging fields": {1: {"status": "000", "list": [one]}},
            "list is not a list": {1: dict(page([one]), list={"0": one})},
            "date before range": {1: page([dict(one, rcept_dt="20981231")])},
            "impossible date": {1: page([dict(one, rcept_dt="20990230")])},
            "short receipt": {1: page([dict(one, rcept_no="2099033100000")])},
            "blank report name": {1: page([dict(one, report_nm=" ")])},
        }
        for label, pages in cases.items():
            with self.subTest(label), self.assertRaises(DartResponseError):
                self.client(pages).list_disclosures(CORP, begin_date=self.BEGIN, end_date=self.END)


class ArchiveSafetyTests(unittest.TestCase):
    """ZIP은 디스크에 풀지 않고, 위험한 모양이면 filing-archive-invalid로 막는다."""

    def assert_archive_invalid(self, payload):
        with self.assertRaises(FilingArchiveError) as caught:
            read(payload)
        self.assertEqual(caught.exception.code.value, "filing-archive-invalid")

    def test_not_an_archive(self):
        for payload in (b"", b"not a zip archive", b"PK\x03\x04truncated", zipped({})):
            with self.subTest(payload=payload[:12]):
                self.assert_archive_invalid(payload)

    def test_entry_count_limit(self):
        members = {f"part{number:03d}.xml": f"<P>합성 문단 {number}.</P>" for number in range(101)}
        self.assert_archive_invalid(zipped(members))
        del members["part100.xml"]
        document = read(zipped(members))
        self.assertEqual(len(document.files), 100)
        self.assertTrue(document.text.startswith("합성 문단 0.\n합성 문단 1."))

    def test_path_traversal_and_absolute_names(self):
        for name in ("../evil.xml", "a/../../evil.xml", "a\\..\\..\\evil.xml", "/abs.xml", "C:/abs.xml", "c:evil.xml"):
            with self.subTest(name=name):
                self.assert_archive_invalid(zipped({"ok.xml": "<P>정상.</P>", name: "<P>탈출.</P>"}))

    def test_encrypted_entry(self):
        data = bytearray(zipped({"d.xml": document_xml(ANNUAL_BODY)}))
        central = data.find(b"PK\x01\x02")
        data[central + 8] |= 0x01  # 중앙 디렉터리의 general purpose flag: 암호화 비트
        self.assert_archive_invalid(bytes(data))

    def test_compression_ratio_and_size_limits(self):
        self.assert_archive_invalid(zipped({"bomb.xml": b"0" * 1_000_000}))
        stored = zipped({"d.xml": "<P>" + "가" * 40 + "</P>"}, compression=zipfile.ZIP_STORED)
        self.assertEqual(read(stored).text, "가" * 40)
        with patch.object(filings, "MAX_UNCOMPRESSED_BYTES", 100):
            self.assert_archive_invalid(stored)
        with patch.object(filings, "MAX_ARCHIVE_BYTES", len(stored) - 1):
            self.assert_archive_invalid(stored)

    def test_no_readable_text(self):
        self.assert_archive_invalid(zipped({"image.png": b"\x89PNG synthetic", "style.css": "p {}"}))
        self.assert_archive_invalid(zipped({"blank.xml": "<DOCUMENT> <P>  </P> </DOCUMENT>"}))

    def test_non_text_members_are_listed_but_not_read(self):
        document = read(zipped({"z.xml": "<P>본문.</P>", "a.png": b"\x89PNG synthetic"}))
        self.assertEqual(document.files, ("a.png", "z.xml"))
        self.assertEqual(document.text, "본문.")


class EncodingTests(unittest.TestCase):
    def test_detected_encodings(self):
        korean = "<P>가상회사 똠방각하 공시.</P>"
        euc_kr = "<P>가상회사 공시.</P>"
        cases = (
            ("utf-8", korean.encode("utf-8"), "가상회사 똠방각하 공시."),
            ("utf-8-sig", b"\xef\xbb\xbf" + korean.encode("utf-8"), "가상회사 똠방각하 공시."),
            ("utf-16", korean.encode("utf-16"), "가상회사 똠방각하 공시."),
            ("euc-kr", b'<?xml version="1.0" encoding="euc-kr"?>' + euc_kr.encode("euc-kr"), "가상회사 공시."),
            # 선언은 euc-kr이지만 cp949 전용 글자(똠)가 있으면 cp949로 읽고 그렇게 보고한다.
            ("cp949", b'<?xml version="1.0" encoding="euc-kr"?>' + korean.encode("cp949"), "가상회사 똠방각하 공시."),
            ("utf-8", b'<?xml version="1.0" encoding="x-synthetic"?>' + korean.encode("utf-8"), "가상회사 똠방각하 공시."),
        )
        for expected_encoding, raw, expected_text in cases:
            with self.subTest(expected_encoding=expected_encoding, raw=raw[:24]):
                document = read(zipped({"d.xml": raw}))
                self.assertEqual(document.encoding, expected_encoding)
                self.assertEqual(document.text, expected_text)

    def test_mixed_member_encodings_are_reported_in_name_order(self):
        document = read(zipped({
            "a.xml": "<P>첫째 똠.</P>".encode("utf-8"),
            "b.xml": "<P>둘째 똠.</P>".encode("cp949"),
            "c.xml": "<P>셋째.</P>".encode("utf-8"),
        }))
        self.assertEqual(document.encoding, "utf-8,cp949")
        self.assertEqual(document.text, "첫째 똠.\n둘째 똠.\n셋째.")

    def test_undecodable_bytes(self):
        with self.assertRaises(FilingEncodingError) as caught:
            read(zipped({"d.xml": b"<P>\xff\xff\xff</P>"}))
        self.assertEqual(caught.exception.code.value, "filing-encoding-invalid")


class TextStructureTests(unittest.TestCase):
    """줄바꿈과 표 행은 보존하고, 표 행은 문장에서 뺀다."""

    def text(self, body):
        return read(zipped({"d.xml": document_xml(body)})).text

    def test_document_text_keeps_lines_and_table_rows(self):
        document = read(zipped({"d.xml": document_xml(ANNUAL_BODY)}))
        self.assertEqual(document.text, ANNUAL_TEXT)
        self.assertEqual((document.rcept_no, document.doc_type, document.encoding, document.files), (ANNUAL, DOC_ANNUAL, "utf-8", ("d.xml",)))

    def test_nested_tables_entities_comments_and_cr(self):
        self.assertEqual(
            self.text("<TABLE><TR><TD>바깥</TD><TD><TABLE><TR><TD>안1</TD><TD>안2</TD></TR><TR><TD>안3</TD><TD></TD></TR></TABLE></TD></TR></TABLE>"),
            "| 바깥 | 안1 ; 안2 / 안3 |",
        )
        self.assertEqual(self.text("<P>A&amp;B &lt;C&gt;<!-- 숨김 주석 --> 끝&cr;이어짐</P>"), "A&B <C> 끝 이어짐")
        self.assertEqual(self.text("<TABLE><TR><TD>a\\b</TD><TD>x|y</TD></TR></TABLE>"), "| a\\\\b | x\\|y |")

    def test_section_text_and_sentences(self):
        document = read(zipped({"d.xml": document_xml(ANNUAL_BODY)}))
        section = get_filing_section(document, section="사업의 내용")
        self.assertEqual((section.rcept_no, section.doc_type, section.section, section.status), (ANNUAL, DOC_ANNUAL, "사업의 내용", "ok"))
        self.assertEqual(section.text, BUSINESS_TEXT)
        self.assertIn("주요 고객은 가상 고객사다.", section.sentences)
        self.assertIn("수출 비중은 절반이다.", section.sentences)
        self.assertFalse([sentence for sentence in section.sentences if "|" in sentence or "1,000" in sentence])
        self.assertNotIn("재무 내용.", section.text)

    def test_section_statuses(self):
        document = read(zipped({"d.xml": document_xml(ANNUAL_BODY)}))
        missing = get_filing_section(document, section="이사의 경영진단 및 분석의견")
        self.assertEqual((missing.status, missing.text, missing.sentences), ("section-not-found", None, ()))
        for section in ("투자위험요소", "재무에 관한 사항", "사업"):
            with self.subTest(section=section):
                self.assertEqual(get_filing_section(document, section=section).status, "section-not-applicable")

    def test_section_boundaries_follow_heading_levels(self):
        document = annual_document(
            "I. 회사의 개요\n개요 문장.\nII. 사업의 내용\n1. 사업의 개요\n본문 문장.\n가. 세부\n세부 문장.\n"
            "| III. 표 안의 제목 |\n2. 주요 제품\n제품 문장.\nIII. 재무에 관한 사항\n재무 문장."
        )
        self.assertEqual(
            get_filing_section(document, section="사업의 내용").text,
            "1. 사업의 개요\n본문 문장.\n가. 세부\n세부 문장.\n| III. 표 안의 제목 |\n2. 주요 제품\n제품 문장.",
        )
        numbered = annual_document("1. 사업의 내용\n본문.\n1.1 세부\n세부.\n2. 주주에 관한 사항\n주주.")
        self.assertEqual(get_filing_section(numbered, section="사업의 내용").text, "본문.\n1.1 세부\n세부.")
        self.assertEqual(get_filing_section(numbered, section="주주에 관한 사항").text, "주주.")
        chapter = annual_document("제 2 장 사업의 내용\n본문.\n제3장. 주주에 관한 사항\n주주.")
        self.assertEqual(get_filing_section(chapter, section="사업의 내용").text, "본문.")

    def test_unnumbered_known_heading_ends_a_section(self):
        document = annual_document("사업의 내용\n본문 문장.\n이사의 경영진단 및 분석의견\n분석 문장.")
        self.assertEqual(get_filing_section(document, section="사업의 내용").text, "본문 문장.")
        self.assertEqual(get_filing_section(document, section="이사의 경영진단 및 분석의견").text, "분석 문장.")

    def test_table_rows_and_near_titles_are_not_headings(self):
        for text in (
            "| 사업의 내용 |\n본문.",
            "II. 사업의 내용 및 기타\n본문.",
            "II. 사업의 내용\nIII. 재무에 관한 사항\n재무.",
        ):
            with self.subTest(text=text):
                self.assertEqual(get_filing_section(annual_document(text), section="사업의 내용").status, "section-not-found")

    def test_securities_registration_sections(self):
        document = read(zipped({"d.xml": document_xml(REGISTRATION_BODY)}), REGISTRATION_ROW)
        self.assertEqual(document.doc_type, DOC_SECURITIES)
        self.assertEqual(get_filing_section(document, section="사업위험").text, "합성 수요가 줄 수 있다.")
        self.assertEqual(get_filing_section(document, section="회사위험").sentences, ("가상 공장이 하나뿐이다.",))
        self.assertEqual(get_filing_section(document, section="기타위험").status, "section-not-found")
        self.assertEqual(get_filing_section(document, section="사업의 내용").status, "section-not-applicable")


class DiffTests(unittest.TestCase):
    def document(self, prose, rcept_no=ANNUAL, table_amount="1,000", doc_type=DOC_ANNUAL):
        text = read(zipped({"d.xml": business_section(prose, table_amount)})).text
        return FilingDocument(rcept_no, doc_type, text, "utf-8", ("d.xml",))

    BEFORE = "당사는 합성 부품을 만든다. 주요 고객은 가상 고객사다. 수출 비중은 절반이다."
    AFTER = "당사는 합성 부품을 만든다. 주요 고객은 새 고객사다. 수출 비중은 절반이다. 신규 공장을 지었다."

    def test_sentence_changes(self):
        diff = diff_filing_sections(self.document(self.BEFORE), self.document(self.AFTER, ANNUAL_FIX), section="사업의 내용")
        self.assertEqual((diff.rcept_no_a, diff.rcept_no_b, diff.section, diff.doc_type), (ANNUAL, ANNUAL_FIX, "사업의 내용", DOC_ANNUAL))
        self.assertEqual(diff.blocks, (
            {"kind": "replace", "before": ("주요 고객은 가상 고객사다.",), "after": ("주요 고객은 새 고객사다.",)},
            {"kind": "insert", "before": (), "after": ("신규 공장을 지었다.",)},
        ))
        reverse = diff_filing_sections(self.document(self.AFTER, ANNUAL_FIX), self.document(self.BEFORE), section="사업의 내용")
        self.assertEqual(reverse.blocks[1], {"kind": "delete", "before": ("신규 공장을 지었다.",), "after": ()})

    def test_identical_and_table_only_changes_have_no_blocks(self):
        same = diff_filing_sections(self.document(self.BEFORE), self.document(self.BEFORE, ANNUAL_FIX), section="사업의 내용")
        self.assertEqual(same.blocks, ())
        table_only = diff_filing_sections(self.document(self.BEFORE), self.document(self.BEFORE, ANNUAL_FIX, "2,000"), section="사업의 내용")
        self.assertEqual(table_only.blocks, ())

    def test_incompatible_or_missing_sections(self):
        with self.assertRaises(FilingSchemaError):
            diff_filing_sections(self.document(self.BEFORE), self.document(self.BEFORE, HALF, doc_type=DOC_QUARTERLY), section="사업의 내용")
        empty = FilingDocument(ANNUAL_FIX, DOC_ANNUAL, "I. 회사의 개요\n개요.", "utf-8", ("d.xml",))
        for section, other in (("사업의 내용", empty), ("투자위험요소", self.document(self.AFTER, ANNUAL_FIX))):
            with self.subTest(section=section), self.assertRaises(FilingError) as caught:
                diff_filing_sections(self.document(self.BEFORE), other, section=section)
            self.assertIs(type(caught.exception), FilingError)
            self.assertEqual(caught.exception.code.value, "filing-error")


class FilingToolTests(IsolatedCase):
    """MCP 도구 경계: 세션 맥락 규칙, 캐시, 허용 보고서, 오류 코드."""

    def runtime(self, rows, documents=None):
        documents = documents or {}

        def handler(request):
            if request.url.path == "/api/list.json":
                return httpx.Response(200, json=page(rows))
            self.assertEqual(request.url.path, "/api/document.xml")
            answer = documents[request.url.params["rcept_no"]]
            payload = answer() if callable(answer) else answer
            return httpx.Response(200, content=payload)

        runtime = create_live_runtime(self.settings(), http_client=self.mock_client(handler), state_dir=self.root / "state")
        self.addCleanup(runtime.close)
        return runtime

    def list_all(self, runtime):
        return call(runtime.service, "list_filings", {"corp_code": CORP, "from": "2099-01-01", "to": "2099-12-31"})

    def document_requests(self):
        return [request.url.params["rcept_no"] for request in self.requests if request.url.path == "/api/document.xml"]

    def test_list_filings_envelope(self):
        runtime = self.runtime([ANNUAL_FIX_ROW, HALF_ROW, ANNUAL_ROW])
        result = self.list_all(runtime)
        self.assertEqual(result["source"], "dart")
        self.assertEqual(result["reference"]["value"], "list.json")
        self.assertEqual(len(result["reference"]["params_hash"]), 64)
        self.assertEqual([item["rcept_no"] for item in result["data"]], [ANNUAL, ANNUAL_FIX, HALF])
        first, fix, half = result["data"]
        self.assertEqual(set(first), {"rcept_no", "rcept_dt", "report_nm", "doc_type", "original_rcept_no", "is_current", "raw"})
        self.assertEqual((first["is_current"], fix["is_current"], fix["original_rcept_no"]), (False, True, ANNUAL))
        self.assertEqual((half["doc_type"], half["is_current"]), (DOC_QUARTERLY, True))
        params = self.requests[0].url.params
        self.assertEqual((params["crtfc_key"], params["bgn_de"], params["end_de"]), (DART_KEY, "20990101", "20991231"))
        self.assertNotIn("pblntf_ty", params)
        self.assert_no_secrets(repr(result))

    def test_service_dates_are_iso_and_checked_before_any_request(self):
        runtime = self.runtime([])
        for arguments in (
            {"from": "20990101", "to": "2099-12-31"},
            {"from": "2099-02-30", "to": "2099-12-31"},
            {"from": "2099-01-01"},
            {"from": "2099-01-01", "to": "2099-12-31", "extra": "x"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ToolArgumentsError):
                call(runtime.service, "list_filings", {"corp_code": CORP, **arguments})
        result = call(runtime.service, "list_filings", {"corp_code": CORP, "from": "2099-12-31", "to": "2099-01-01"})
        self.assertEqual(result["data"], {"status": "blocked", "error": "invalid-argument"})
        self.assertEqual(self.requests, [])
        result = call(runtime.service, "list_filings", {"corp_code": CORP, "from": "2099-03-31", "to": "2099-03-31", "type": "A"})
        self.assertEqual(result["data"], [])
        self.assertEqual(self.requests[0].url.params["pblntf_ty"], "A")

    def test_receipts_need_list_filings_in_the_same_session(self):
        documents = {ANNUAL: zipped({"d.xml": document_xml(ANNUAL_BODY)})}
        runtime = self.runtime([ANNUAL_ROW], documents)
        blocked = {"status": "blocked", "error": "filing-context-unavailable"}
        before_listing = (
            ("get_filing_document", {"rcept_no": ANNUAL}),
            ("get_filing_section", {"rcept_no": ANNUAL, "section": "사업의 내용"}),
            ("diff_filing_sections", {"rcept_no_a": ANNUAL, "rcept_no_b": ANNUAL, "section": "사업의 내용"}),
        )
        for name, arguments in before_listing:
            with self.subTest(name=name):
                self.assertEqual(call(runtime.service, name, arguments)["data"], blocked)
        self.assertEqual(self.requests, [])

        self.list_all(runtime)
        self.assertEqual(call(runtime.service, "get_filing_document", {"rcept_no": OTHER})["data"], blocked)
        diff = call(runtime.service, "diff_filing_sections", {"rcept_no_a": OTHER, "rcept_no_b": ANNUAL, "section": "사업의 내용"})
        self.assertEqual(diff["data"], blocked)
        self.assertEqual(self.document_requests(), [])
        diff = call(runtime.service, "diff_filing_sections", {"rcept_no_a": ANNUAL, "rcept_no_b": OTHER, "section": "사업의 내용"})
        self.assertEqual(diff["data"], blocked)

        # 맥락은 런타임(세션)마다 따로다.
        other_session = self.runtime([ANNUAL_ROW], documents)
        self.assertEqual(call(other_session.service, "get_filing_document", {"rcept_no": ANNUAL})["data"], blocked)

    def test_document_is_read_once_per_receipt(self):
        runtime = self.runtime([ANNUAL_ROW], {ANNUAL: zipped({"d.xml": document_xml(ANNUAL_BODY)})})
        self.list_all(runtime)
        document = call(runtime.service, "get_filing_document", {"rcept_no": ANNUAL})
        self.assertEqual(document["data"]["text"], ANNUAL_TEXT)
        self.assertEqual(document["data"]["files"], ["d.xml"])
        self.assertEqual(document["reference"]["public_url"], dart_public_url(ANNUAL))
        section = call(runtime.service, "get_filing_section", {"rcept_no": ANNUAL, "section": "사업의 내용"})
        self.assertEqual((section["data"]["status"], section["data"]["text"]), ("ok", BUSINESS_TEXT))
        missing = call(runtime.service, "get_filing_section", {"rcept_no": ANNUAL, "section": "이사의 경영진단 및 분석의견"})
        self.assertEqual(missing["data"]["status"], "section-not-found")
        not_applicable = call(runtime.service, "get_filing_section", {"rcept_no": ANNUAL, "section": "투자위험요소"})
        self.assertEqual(not_applicable["data"]["status"], "section-not-applicable")
        self.assertEqual(self.document_requests(), [ANNUAL])
        document_request = next(r for r in self.requests if r.url.path == "/api/document.xml")
        self.assertEqual(document_request.url.params["crtfc_key"], DART_KEY)
        for result in (document, section, missing):
            self.assert_no_secrets(repr(result))

    def test_failed_archive_is_not_cached(self):
        answers = iter([b"not a zip archive", zipped({"d.xml": document_xml(ANNUAL_BODY)})])
        runtime = self.runtime([ANNUAL_ROW], {ANNUAL: lambda: next(answers)})
        self.list_all(runtime)
        first = call(runtime.service, "get_filing_document", {"rcept_no": ANNUAL})
        self.assertEqual(first["data"], {"status": "blocked", "error": "filing-archive-invalid"})
        second = call(runtime.service, "get_filing_document", {"rcept_no": ANNUAL})
        self.assertEqual(second["data"]["text"], ANNUAL_TEXT)
        self.assertEqual(self.document_requests(), [ANNUAL, ANNUAL])

    def test_archive_and_encoding_codes_through_the_service(self):
        cases = {
            "filing-archive-invalid": zipped({"../evil.xml": "<P>탈출.</P>"}),
            "filing-encoding-invalid": zipped({"d.xml": b"<P>\xff\xff\xff</P>"}),
        }
        for code, payload in cases.items():
            with self.subTest(code=code):
                runtime = self.runtime([ANNUAL_ROW], {ANNUAL: payload})
                self.list_all(runtime)
                for name, arguments in (
                    ("get_filing_document", {"rcept_no": ANNUAL}),
                    ("get_filing_section", {"rcept_no": ANNUAL, "section": "사업의 내용"}),
                ):
                    self.assertEqual(call(runtime.service, name, arguments)["data"], {"status": "blocked", "error": code})

    def test_only_allowed_report_types_are_downloaded(self):
        runtime = self.runtime([OTHER_ROW, REGISTRATION_ROW], {REGISTRATION: zipped({"d.xml": document_xml(REGISTRATION_BODY)})})
        listed = self.list_all(runtime)
        self.assertEqual([item["doc_type"] for item in listed["data"]], [DOC_UNKNOWN, DOC_SECURITIES])
        for name, arguments in (
            ("get_filing_document", {"rcept_no": OTHER}),
            ("get_filing_section", {"rcept_no": OTHER, "section": "사업의 내용"}),
        ):
            with self.subTest(name=name):
                self.assertEqual(call(runtime.service, name, arguments)["data"], {"status": "blocked", "error": "filing-schema-invalid"})
        self.assertEqual(self.document_requests(), [])
        risk = call(runtime.service, "get_filing_section", {"rcept_no": REGISTRATION, "section": "사업위험"})
        self.assertEqual((risk["data"]["status"], risk["data"]["text"]), ("ok", "합성 수요가 줄 수 있다."))

    def test_diff_through_the_service(self):
        before = "당사는 합성 부품을 만든다. 주요 고객은 가상 고객사다."
        after = "당사는 합성 부품을 만든다. 주요 고객은 새 고객사다."
        runtime = self.runtime([ANNUAL_ROW, ANNUAL_FIX_ROW, HALF_ROW], {
            ANNUAL: zipped({"d.xml": business_section(before)}),
            ANNUAL_FIX: zipped({"d.xml": business_section(after, "2,000")}),
            HALF: zipped({"d.xml": document_xml("<SECTION-1><TITLE>II. 사업의 내용</TITLE><P>반기 본문.</P></SECTION-1>")}),
        })
        self.list_all(runtime)
        result = call(runtime.service, "diff_filing_sections", {"rcept_no_a": ANNUAL, "rcept_no_b": ANNUAL_FIX, "section": "사업의 내용"})
        self.assertEqual(result["source"], "dart")
        self.assertEqual(result["reference"]["value"], "filing-section-diff:1.0")
        self.assertEqual(result["data"]["blocks"], [{"kind": "replace", "before": ["주요 고객은 가상 고객사다."], "after": ["주요 고객은 새 고객사다."]}])
        mixed = call(runtime.service, "diff_filing_sections", {"rcept_no_a": ANNUAL, "rcept_no_b": HALF, "section": "사업의 내용"})
        self.assertEqual(mixed["data"], {"status": "blocked", "error": "filing-schema-invalid"})
        missing = call(runtime.service, "diff_filing_sections", {"rcept_no_a": ANNUAL, "rcept_no_b": ANNUAL_FIX, "section": "주주에 관한 사항"})
        self.assertEqual(missing["data"], {"status": "blocked", "error": "filing-error"})
        self.assertEqual(sorted(self.document_requests()), sorted([ANNUAL, ANNUAL_FIX, HALF]))
        self.assert_no_secrets(repr(result))


if __name__ == "__main__":
    unittest.main()
