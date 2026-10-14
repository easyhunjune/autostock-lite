"""공시 목록·원문·절·비교 도구를 손으로 만든 합성 응답으로만 검증한다.

회사 고유번호, 접수번호(2099년), 보고서명, 본문은 모두 지어낸 값이다.
"""

import asyncio
from datetime import date
import io
import time
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
    get_filing_document, get_filing_section, list_filings, normalize_sentences, resolve_current_filings,
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

    def test_inline_tags_and_raw_line_breaks_stay_inside_the_line(self):
        """줄은 블록 태그에서만 나온다. 인라인 태그는 지워지고 원문 줄바꿈은 공백이 된다."""
        for body, expected in (
            ('<P>당사는 <SPAN USERMARK="B">반도체</SPAN>를 만든다. 이익도 늘었다.</P>', "당사는 반도체를 만든다. 이익도 늘었다."),
            ("<P>당사는 합성\n부품과\r\n   서비스를\r만든다.</P>", "당사는 합성 부품과 서비스를 만든다."),
            (
                '<p>A<b>굵게</b><I>기울임</I><u>밑줄</u><FONT COLOR="red">글꼴</FONT> H<sub>2</sub>O x<SUP>2</SUP> '
                '<strong>강</strong><Em>조</Em> <a href="https://x.example.test/">링크</a></p>',
                "A굵게기울임밑줄글꼴 H2O x2 강조 링크",
            ),
            # 인라인 목록 밖의 태그(BR, 이름이 비슷한 ABBR·BIG 포함)는 줄바꿈이다.
            ("<P>가<BR/>나<B>다</B><ABBR>라</ABBR><BIG>마</BIG></P>", "가\n나다\n라\n마"),
            ("<P>첫 문단</P><P>둘째 문단</P>\n<P>\n  셋째\n  문단\n</P>", "첫 문단\n둘째 문단\n셋째 문단"),
            ("<TITLE>1. 사업의 개요</TITLE>\n<P>당사는\n<SPAN>부품</SPAN>을 만든다.</P>", "1. 사업의 개요\n당사는 부품을 만든다."),
            (
                "<P>앞 문장.</P>\n<TABLE>\n<TR>\n<TD><SPAN>a</SPAN></TD>\n<TD>b\nc</TD>\n</TR>\n</TABLE>\n<P>뒤 문장.</P>",
                "앞 문장.\n| a | b c |\n뒤 문장.",
            ),
            ("앞<TABLE><TR><TD>a</TD><TD>b</TD></TR></TABLE>뒤", "앞\n| a | b |\n뒤"),
            ("앞<TABLE><TR><TD></TD></TR></TABLE>뒤", "앞\n뒤"),
        ):
            with self.subTest(body=body):
                self.assertEqual(self.text(body), expected)
        self.assertEqual(
            normalize_sentences(self.text('<P>당사는 <SPAN USERMARK="B">반도체</SPAN>를 만든다. 이익도 늘었다.</P>')),
            ("당사는 반도체를 만든다.", "이익도 늘었다."),
        )
        self.assertEqual(normalize_sentences(self.text("<P>당사는 합성\n부품과\n서비스를 만든다.</P>")), ("당사는 합성 부품과 서비스를 만든다.",))

    def test_plain_text_members_keep_their_line_breaks(self):
        # .txt 원문은 마크업이 아니다. 태그·엔티티·표 처리 없이 str.splitlines로 줄을 나누고,
        # 줄마다 공백을 접고 빈 줄을 버린다.
        for raw, expected in (
            ("첫 줄\n둘째 <B>줄</B>\n\n셋째 줄", "첫 줄\n둘째 <B>줄</B>\n셋째 줄"),
            ("첫 줄\r\n\r\n둘째 줄\r\n", "첫 줄\n둘째 줄"),
            ("가\r나\r\n  \r\n다  ", "가\n나\n다"),
            ("a < b and c > d\n&amp; &#10; &cr; <!-- 주석 --> <TABLE><TR><TD>x</TD></TR></TABLE>",
             "a < b and c > d\n&amp; &#10; &cr; <!-- 주석 --> <TABLE><TR><TD>x</TD></TR></TABLE>"),
            ("첫 줄" + chr(0x0C) + "둘째" + chr(0x2028) + "셋째" + chr(0x85) + "넷째", "첫 줄\n둘째\n셋째\n넷째"),
            ("  a \t  b\t", "a b"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(read(zipped({"d.txt": raw})).text, expected)

    def test_plain_text_long_whitespace_run_collapses(self):
        raw = "II. 사업의 내용\n본문" + " " * 200_000 + "끝.\n"
        payload = zipped({"d.txt": raw}, compression=zipfile.ZIP_STORED)
        started = time.perf_counter()
        document = read(payload)
        # 넉넉한 상한이다. 예전 정규식은 이 입력에서 수십 초가 걸렸다.
        self.assertLess(time.perf_counter() - started, 5.0)
        self.assertEqual(document.text, "II. 사업의 내용\n본문 끝.")

    def test_encoded_and_unicode_line_breaks_do_not_split_prose(self):
        """엔티티로 적었거나 유니코드 줄 구분 문자인 줄바꿈도 마크업 안에서는 공백이다."""
        self.assertEqual(self.text("<P>줄&#13;&#10;바꿈 엔티티.</P>"), "줄 바꿈 엔티티.")
        self.assertEqual(normalize_sentences(self.text("<P>줄&#13;&#10;바꿈 엔티티.</P>")), ("줄 바꿈 엔티티.",))
        self.assertEqual(self.text("<P>가 나" + chr(0x85) + "다 끝.</P>"), "가 나 다 끝.")
        self.assertEqual(normalize_sentences(self.text("<P>가 나" + chr(0x85) + "다 끝.</P>")), ("가 나 다 끝.",))
        encoded = ("&#10;", "&#xA;", "&#X0D;", "&#0010;", "&#x000a;", "&#12;", "&#8232;", "&#x2029;", "&NewLine;")
        raw = tuple(chr(code) for code in (0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029))
        for line_break in encoded + raw:
            with self.subTest(line_break=line_break):
                text = self.text(f"<P>앞{line_break}뒤 문장.</P>")
                self.assertEqual(text, "앞 뒤 문장.")
                self.assertEqual(normalize_sentences(text), ("앞 뒤 문장.",))
        # &#133;은 HTML5 규칙으로 줄임표가 된다(U+0085가 아니다). 줄바꿈이 아니므로 그대로 둔다.
        self.assertEqual(self.text("<P>잠깐&#133; 다음.</P>"), "잠깐… 다음.")
        # 표 처리가 넣는 줄 경계(빈 표, 닫히지 않은 표 태그)는 유지된다.
        self.assertEqual(self.text("앞<TABLE><TR><TD></TD></TR></TABLE>뒤"), "앞\n뒤")

    def test_section_text_and_sentences_see_the_same_lines(self):
        body = (
            "<SECTION-1><TITLE>II. 사업의 내용</TITLE>\n"
            "<P>첫째&#10;문장.</P><P>둘째" + chr(0x2028) + "문장.</P>\n"
            "<TABLE><TR><TD>a</TD><TD>b</TD></TR></TABLE>\n"
            "<P>셋째&#x0C;문장.</P></SECTION-1>\n"
            "<SECTION-1><TITLE>III. 재무에 관한 사항</TITLE><P>재무.</P></SECTION-1>"
        )
        section = get_filing_section(read(zipped({"d.xml": document_xml(body)})), section="사업의 내용")
        self.assertEqual(section.text, "첫째 문장.\n둘째 문장.\n| a | b |\n셋째 문장.")
        self.assertEqual(section.sentences, tuple(line for line in section.text.split("\n") if not line.startswith("| ")))
        # 밖에서 만든 FilingDocument도 같다. 줄 경계는 LF·CRLF·CR이고 U+2028은 줄 안에 남는다.
        external = annual_document(
            "II. 사업의 내용\r\n본문 가" + chr(0x2028) + "나.\r\nIII. 재무에 관한 사항\r\n재무."
        )
        section = get_filing_section(external, section="사업의 내용")
        self.assertEqual(section.text, "본문 가" + chr(0x2028) + "나.")
        self.assertEqual(section.sentences, ("본문 가 나.",))

    def test_bare_cr_external_text_still_splits_lines(self):
        document = annual_document("II. 사업의 내용\r본문.\rIII. 재무에 관한 사항\r재무.")
        section = get_filing_section(document, section="사업의 내용")
        self.assertEqual((section.status, section.text, section.sentences), ("ok", "본문.", ("본문.",)))
        self.assertEqual(normalize_sentences("가\r나\r\n다\n라"), ("가", "나", "다", "라"))

    def test_long_whitespace_inside_a_heading_line(self):
        gap = " " * 200_000
        started = time.perf_counter()
        identity = filings._heading_identity("1. 제목" + gap + "x")
        # 넉넉한 상한이다. 예전 정규식은 이 줄에서 분 단위가 걸렸다.
        self.assertLess(time.perf_counter() - started, 5.0)
        self.assertEqual(identity, ("제목" + gap + "x", 2))
        self.assertEqual(filings._heading_identity("III. 재무" + gap + "x"), ("재무" + gap + "x", 1))
        # 긴 공백이 든 III장 제목이 제목으로 인식돼 사업의 내용 절을 끝낸다.
        document = annual_document("II. 사업의 내용\n본문.\nIII. 재무" + gap + "x\n재무.")
        self.assertEqual(get_filing_section(document, section="사업의 내용").text, "본문.")
        # 마크업의 &nbsp; 반복도 NFKC 뒤 같은 경로를 탄다.
        body = "<P>II. 사업의 내용</P><P>본문.</P><P>III. 재무" + "&nbsp;" * 20_000 + "x</P><P>재무.</P>"
        marked_up = read(zipped({"d.xml": document_xml(body)}, compression=zipfile.ZIP_STORED))
        self.assertEqual(get_filing_section(marked_up, section="사업의 내용").text, "본문.")

    def test_section_sentences_with_inline_markup(self):
        body = (
            "<SECTION-1><TITLE>II. 사업의 내용</TITLE>\n<SECTION-2><TITLE>1. 사업의 개요</TITLE>\n"
            '<P>당사는 <SPAN USERMARK="B">반도체</SPAN>를\n만든다. 이익도 늘었다.</P>\n</SECTION-2></SECTION-1>\n'
            "<SECTION-1><TITLE>III. 재무에 관한 사항</TITLE><P>재무.</P></SECTION-1>"
        )
        section = get_filing_section(read(zipped({"d.xml": document_xml(body)})), section="사업의 내용")
        self.assertEqual(section.text, "1. 사업의 개요\n당사는 반도체를 만든다. 이익도 늘었다.")
        self.assertEqual(section.sentences, ("1. 사업의 개요", "당사는 반도체를 만든다.", "이익도 늘었다."))

    def test_section_text_and_sentences(self):
        document = read(zipped({"d.xml": document_xml(ANNUAL_BODY)}))
        section = get_filing_section(document, section="사업의 내용")
        self.assertEqual((section.rcept_no, section.doc_type, section.section, section.status), (ANNUAL, DOC_ANNUAL, "사업의 내용", "ok"))
        self.assertEqual(section.text, BUSINESS_TEXT)
        self.assertIn("주요 고객은 가상 고객사다.", section.sentences)
        self.assertIn("수출 비중은 절반이다.", section.sentences)
        self.assertFalse([sentence for sentence in section.sentences if "|" in sentence or "1,000" in sentence])
        self.assertNotIn("재무 내용.", section.text)
        # 제목 줄은 다음 문장에 붙지 않고, 목록 표식의 마침표에서 끊기지도 않는다.
        self.assertEqual(section.sentences, (
            "1. 사업의 개요", "당사는 합성 부품을 만든다.", "주요 고객은 가상 고객사다.",
            "수출 비중은 절반이다.", "2. 주요 제품", "제품은 하나다.",
        ))

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

    def test_glued_markers_from_inline_tags_are_headings(self):
        # 인라인 태그가 지워지면 표식과 제목이 공백 없이 붙는다.
        document = read(zipped({"d.xml": document_xml(
            "<P><SPAN>IV.</SPAN><SPAN>이사의 경영진단 및 분석의견</SPAN></P><P>본문.</P><TITLE>V. 감사인</TITLE>"
        )}))
        self.assertEqual(document.text, "IV.이사의 경영진단 및 분석의견\n본문.\nV. 감사인")
        section = get_filing_section(document, section="이사의 경영진단 및 분석의견")
        self.assertEqual((section.status, section.text, section.sentences), ("ok", "본문.", ("본문.",)))
        document = read(zipped({"d.xml": document_xml(
            "<P><B>II.</B>사업의 내용</P><P>본문.</P><P>III. 재무에 관한 사항</P><P>재무.</P>"
        )}))
        section = get_filing_section(document, section="사업의 내용")
        self.assertEqual((section.status, section.text), ("ok", "본문."))

    def test_heading_marker_spacing_rules(self):
        for line, title, level in (
            ("IV.이사의 경영진단 및 분석의견", "이사의 경영진단 및 분석의견", 1),
            ("II.사업의 내용", "사업의 내용", 1),
            ("가.주요 제품", "주요 제품", 3),
            ("제2장.사업의 내용", "사업의 내용", 1),
            ("제 2 장 사업의 내용", "사업의 내용", 1),
            ("제2장. 사업의 내용", "사업의 내용", 1),
            ("제 2 장.사업의 내용", "사업의 내용", 1),
            ("제3편 기타", "기타", 1),
            ("II. 사업의 내용", "사업의 내용", 1),
            ("가. 주요 제품", "주요 제품", 3),
            ("1. 사업의 개요", "사업의 개요", 2),
            ("1.1 세부", "세부", 3),
        ):
            with self.subTest(line=line):
                self.assertEqual(filings._heading_identity(line), (title, level))
        for line in (
            # 제N장/편은 공백이나 "마침표+한글"이 있어야 한다. 붙은 참조 문장은 제목이 아니다.
            "제2장사업의 내용", "제2장에서 설명한 바와 같이 당사는", "제3편의 내용", "제1장비 투자", "제 5 장」", "제1장.",
            # 붙은 로마 숫자·한글 순번 뒤에는 한글 음절만 올 수 있다.
            "X.(주)가 공급했다", "C.,", "I..", "V...", "L.「", "IV.-이사", "IV.:이사", "다.(주석 3 참조)", "다.」",
            "C.E.O.가 바뀌었다.", "I.R. 활동을 했다.", "X.509 인증서", "L.A.에서",
            # 숫자 표식 뒤에는 공백이 있어야 하고, 표식만 있는 줄은 제목이 아니다.
            "3.5% 늘었다", "1.사업의 내용", "가.",
        ):
            with self.subTest(line=line):
                self.assertEqual(filings._heading_identity(line), (line, None))

    def test_glued_korean_markers_do_not_swallow_prose(self):
        # 시작: 제목이 절 이름과 같아야 하므로 "다.그리고 …"나 "가."만 있는 줄은 절을 시작하지 않는다.
        document = annual_document("다.그리고 사업의 내용\n가.\n본문.")
        self.assertEqual(get_filing_section(document, section="사업의 내용").status, "section-not-found")
        # 끝: 붙은 표식 뒤가 한글이 아닌 줄은 1레벨 제목이 아니므로 절을 끝내지 않는다.
        document = annual_document(
            "II. 사업의 내용\n본문.\nC.E.O.가 바뀌었다.\nX.(주)가 공급했다.\nIV.-이사회 결의로 바꿨다.\n"
            "III. 재무에 관한 사항\n재무."
        )
        self.assertEqual(
            get_filing_section(document, section="사업의 내용").text,
            "본문.\nC.E.O.가 바뀌었다.\nX.(주)가 공급했다.\nIV.-이사회 결의로 바꿨다.",
        )

    def test_chapter_cross_references_do_not_end_a_section(self):
        document = annual_document(
            "II. 사업의 내용\n본문 1.\n제2장에서 설명한 바와 같이 수요가 늘었다.\n본문 2.\nIII. 재무에 관한 사항\n재무."
        )
        section = get_filing_section(document, section="사업의 내용")
        self.assertEqual((section.status, section.text), ("ok", "본문 1.\n제2장에서 설명한 바와 같이 수요가 늘었다.\n본문 2."))
        # 붙은 로마 숫자 제목은 절을 제대로 끝낸다.
        glued = annual_document("II. 사업의 내용\n본문.\nIII.재무에 관한 사항\n재무.")
        self.assertEqual(get_filing_section(glued, section="사업의 내용").text, "본문.")

    def test_securities_registration_sections(self):
        document = read(zipped({"d.xml": document_xml(REGISTRATION_BODY)}), REGISTRATION_ROW)
        self.assertEqual(document.doc_type, DOC_SECURITIES)
        self.assertEqual(get_filing_section(document, section="사업위험").text, "합성 수요가 줄 수 있다.")
        self.assertEqual(get_filing_section(document, section="회사위험").sentences, ("가상 공장이 하나뿐이다.",))
        self.assertEqual(get_filing_section(document, section="기타위험").status, "section-not-found")
        self.assertEqual(get_filing_section(document, section="사업의 내용").status, "section-not-applicable")


class SentenceTests(unittest.TestCase):
    """줄바꿈은 문장 경계다. 줄 맨 앞 목록 표식의 마침표는 문장 끝이 아니다."""

    def test_line_breaks_are_sentence_boundaries(self):
        for text, expected in (
            ("1. 사업의 개요\n당사는 부품을 만든다.", ("1. 사업의 개요", "당사는 부품을 만든다.")),
            ("가\n나", ("가", "나")),
            ("가. 주요 제품\n제품은 하나다. 수출 비중은 절반이다.", ("가. 주요 제품", "제품은 하나다.", "수출 비중은 절반이다.")),
            ("매출은 3.5% 늘었다. 이익도 늘었다.", ("매출은 3.5% 늘었다.", "이익도 늘었다.")),
            ("첫 줄\r\n\n\n  둘째\t\t줄  \n \t \n셋째 줄", ("첫 줄", "둘째 줄", "셋째 줄")),
            ("물음인가? 그렇다! 끝。 다음", ("물음인가?", "그렇다!", "끝。", "다음")),
        ):
            with self.subTest(text=text):
                self.assertEqual(normalize_sentences(text), expected)

    def test_list_markers_keep_the_heading_together(self):
        # NFKC가 먼저 적용되므로 Ⅰ·Ⅱ는 I·II로, ①은 1로 나온다.
        for text, expected in (
            ("1. 사업의 개요", ("1. 사업의 개요",)),
            ("12. 기타 사항\n본문.", ("12. 기타 사항", "본문.")),
            ("나. 판매 경로\n본문.", ("나. 판매 경로", "본문.")),
            ("Ⅰ. 회사의 개요\n본문.", ("I. 회사의 개요", "본문.")),
            ("II. 사업의 내용\n본문.", ("II. 사업의 내용", "본문.")),
            ("(1) 산업의 특성\n본문.", ("(1) 산업의 특성", "본문.")),
            ("① 주요 제품\n본문.", ("1 주요 제품", "본문.")),
            ("1.2. 세부 항목. 다음 문장.", ("1.2. 세부 항목.", "다음 문장.")),
            ("가. 제목이다. 본문이다.", ("가. 제목이다.", "본문이다.")),
        ):
            with self.subTest(text=text):
                self.assertEqual(normalize_sentences(text), expected)

    def test_tables_and_note_markers_are_still_removed(self):
        self.assertEqual(normalize_sentences("| a | b |\n본문이다.\n| 1,000 | 2,000 |"), ("본문이다.",))
        self.assertEqual(normalize_sentences("매출이 늘었다 주석 3) 이익도 늘었다."), ("매출이 늘었다 이익도 늘었다.",))
        # 표식 제거는 줄바꿈을 넘지 않는다.
        self.assertEqual(normalize_sentences("본문 각주\n12) 다음 줄"), ("본문 각주", "12) 다음 줄"))
        self.assertEqual(normalize_sentences(""), ())
        self.assertEqual(normalize_sentences("\n \n| a | b |\n"), ())


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

    def test_a_change_inside_marked_up_prose_replaces_the_whole_sentence(self):
        before = '당사는 <SPAN USERMARK="B">합성</SPAN> 부품을\n만든다. 주요 고객은 가상 고객사다.'
        after = '당사는 <SPAN USERMARK="B">합성</SPAN> 부품을\n판다. 주요 고객은 가상 고객사다.'
        diff = diff_filing_sections(self.document(before), self.document(after, ANNUAL_FIX), section="사업의 내용")
        self.assertEqual(diff.blocks, ({"kind": "replace", "before": ("당사는 합성 부품을 만든다.",), "after": ("당사는 합성 부품을 판다.",)},))

    def test_a_change_under_a_heading_is_its_own_sentence(self):
        before = annual_document("II. 사업의 내용\n1. 사업의 개요\n당사는 부품을 만든다.\n가. 주요 제품\n제품은 하나다.\nIII. 재무에 관한 사항\n재무.")
        after = annual_document("II. 사업의 내용\n1. 사업의 개요\n당사는 부품을 판다.\n가. 주요 제품\n제품은 하나다.\nIII. 재무에 관한 사항\n재무.")
        diff = diff_filing_sections(before, after, section="사업의 내용")
        self.assertEqual(diff.blocks, ({"kind": "replace", "before": ("당사는 부품을 만든다.",), "after": ("당사는 부품을 판다.",)},))
        renamed = annual_document("II. 사업의 내용\n1. 사업 개요\n당사는 부품을 만든다.\n가. 주요 제품\n제품은 하나다.\nIII. 재무에 관한 사항\n재무.")
        diff = diff_filing_sections(before, renamed, section="사업의 내용")
        self.assertEqual(diff.blocks, ({"kind": "replace", "before": ("1. 사업의 개요",), "after": ("1. 사업 개요",)},))


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
