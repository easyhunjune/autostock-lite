"""DART 공시 원문을 안전하고 결정론적으로 읽는 계층.

이 모듈은 네트워크 클라이언트를 소유하지 않는다. 호출자는 ``FilingReader``
프로토콜을 구현해 공시목록과 ``document.xml`` 바이트를 주입한다. 따라서 ZIP
방어와 본문 파싱은 실제 HTTP 없이도 재현 가능하게 검증된다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
from html import unescape
from io import BytesIO
from pathlib import PurePosixPath
import re
from typing import Any, Mapping, Protocol, Sequence
from unicodedata import normalize
from zipfile import BadZipFile, ZipFile

from ..status_codes import ErrorCode


MAX_ARCHIVE_BYTES = 20_000_000
MAX_ARCHIVE_FILES = 100
MAX_UNCOMPRESSED_BYTES = 100_000_000
MAX_COMPRESSION_RATIO = 200

DOC_ANNUAL = "annual_report"
DOC_QUARTERLY = "quarterly_report"
DOC_SECURITIES = "securities_registration"
DOC_UNKNOWN = "unknown-doc-type"
FILING_TYPES = tuple("ABCDEFGHIJ")

REQUIRED_SECTIONS: dict[str, tuple[str, ...]] = {
    DOC_ANNUAL: ("사업의 내용", "이사의 경영진단 및 분석의견"),
    DOC_QUARTERLY: ("사업의 내용",),
    DOC_SECURITIES: ("투자위험요소", "사업위험", "회사위험", "기타위험"),
}
OPTIONAL_SECTIONS: dict[str, tuple[str, ...]] = {
    DOC_ANNUAL: ("주주에 관한 사항", "계열회사 등에 관한 사항"),
    DOC_QUARTERLY: ("이사의 경영진단 및 분석의견",),
    DOC_SECURITIES: (),
}


class FilingError(ValueError):
    """안전하게 공개 가능한 공시 처리 오류."""

    code = ErrorCode.FILING_ERROR


class FilingSchemaError(FilingError):
    code = ErrorCode.FILING_SCHEMA_INVALID


class FilingArgumentError(FilingError):
    """공시 조회 인수가 형식은 맞지만 처리할 수 없는 경우 (예: from > to)."""

    code = ErrorCode.INVALID_ARGUMENT


class FilingArchiveError(FilingError):
    code = ErrorCode.FILING_ARCHIVE_INVALID


class FilingEncodingError(FilingError):
    code = ErrorCode.FILING_ENCODING_INVALID


class FilingReader(Protocol):
    def list_disclosures(
        self,
        corp_code: str,
        *,
        begin_date: date,
        end_date: date,
        disclosure_type: str | None = None,
    ) -> Sequence[Mapping[str, Any]]: ...

    def get_document(self, rcept_no: str) -> bytes: ...


@dataclass(frozen=True)
class Filing:
    rcept_no: str
    rcept_dt: str
    report_nm: str
    doc_type: str
    original_rcept_no: str
    is_current: bool
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class FilingDocument:
    rcept_no: str
    doc_type: str
    text: str
    encoding: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class FilingSection:
    rcept_no: str
    doc_type: str
    section: str
    status: str
    text: str | None = None
    sentences: tuple[str, ...] = ()


@dataclass(frozen=True)
class FilingDiff:
    rcept_no_a: str
    rcept_no_b: str
    section: str
    doc_type: str
    blocks: tuple[Mapping[str, Any], ...]


_CORRECTION = re.compile(r"(?:\[?\s*(?:정정|첨부정정|기재정정)\s*\]?|\(\s*정정\s*\))", re.I)
_SPACE = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_TAG_NAME = re.compile(r"<\s*/?\s*([A-Za-z][\w:.-]*)")
# 문장 안에 놓이는 인라인 태그(대소문자 무시). 빈 문자열로 지워 "반도체</SPAN>를"이 "반도체를"로 붙는다.
# DART의 USERMARK 강조는 SPAN 속성이다. 이 목록 밖의 태그는 모두 줄바꿈이 된다.
_INLINE_TAGS = frozenset({"a", "b", "em", "font", "i", "span", "strong", "sub", "sup", "u"})
# str.splitlines가 줄 경계로 보는 문자 전부. 마크업에서는 줄 경계가 아니라 공백이다.
_LINE_BREAKS = re.compile(r"[\r\n\x0b\x0c\x1c-\x1e\x85\u2028\u2029]+")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TAG_NAME_END = r"(?![\w:.-])"
_TABLE_FAMILY = r"(?:table|tr|td|th|te|tu)"
_MAX_TABLE_PASSES = 32
_INNER_TABLE = re.compile(
    rf"<table{_TAG_NAME_END}[^>]*>((?:(?!<table{_TAG_NAME_END}).)*?)</table\s*>",
    re.I | re.S,
)
# 자기 닫는 태그가 없는 행·셀은 버린다. 닫힌 바깥 표가 소비되므로 산문으로 새지 않는다.
_TABLE_ROW = re.compile(
    rf"<tr{_TAG_NAME_END}[^>]*>((?:(?!<tr{_TAG_NAME_END}).)*?)</tr\s*>", re.I | re.S,
)
_TABLE_CELL = re.compile(
    rf"<(td|th|te|tu){_TAG_NAME_END}(?:[^>]*?/>|[^>]*>((?:(?!<(?:td|th|te|tu){_TAG_NAME_END}).)*?)</\1\s*>)",
    re.I | re.S,
)
# 가장 가까운 표 계열 닫는 태그·다음 시작 태그·표식 앞까지 잔여 텍스트를 버린다.
# 다음 시작 태그와 표식은 남겨, 반복 고아 태그의 재탐색과 표식 너머 삭제를 막는다.
_RESIDUAL_TABLE = re.compile(
    rf"<{_TABLE_FAMILY}{_TAG_NAME_END}[^>]*>(?:(?!\ue000|<{_TABLE_FAMILY}{_TAG_NAME_END}).)*?"
    rf"(?:</{_TABLE_FAMILY}\s*>|(?=\ue000|<{_TABLE_FAMILY}{_TAG_NAME_END}))", re.I | re.S,
)
_TABLE_SENTINEL = re.compile("\ue000([0-9]+)\ue001")
_XML_ENCODING = re.compile(br"<\?xml[^>]*encoding=[\"']([^\"']+)[\"']", re.I)
# 줄바꿈 경계는 normalize_sentences가 줄 단위로 먼저 나눠 처리한다. 이 식은 한 줄 안만 나눈다.
_SENTENCE = re.compile(r"(?<=[.!?。])\s+")
_HORIZONTAL_SPACE = re.compile(r"[^\S\n]+")
_NOTE_MARKER = re.compile(r"(?:주석|각주)[^\S\n]*\d+\)?")
# 줄 맨 앞 목록 표식(1. / 1.2. / 가. / II.)의 마침표는 문장 끝이 아니다. NFKC 뒤에 적용하므로
# Ⅰ·Ⅱ는 I·II로, ①은 1로 바뀌어 들어온다. (1)·①처럼 마침표가 없는 표식은 애초에 나뉘지 않는다.
_LIST_MARKER = re.compile(r"(?:[IVXLC]{1,8}|\d+(?:\.\d+)*|[가나다라마바사아자차카타파하])\.[^\S\n]+")
# fullmatch로 쓴다. _heading_identity가 줄을 strip하므로 끝 공백은 매치하지 않는다(긴 공백에서 되돌아가지 않게).
# 로마 숫자·한글 순번 표식은 공백 없이 한글 음절이 바로 와도 된다(인라인 태그가 지워져 "II.사업의 내용"처럼 붙는다).
# 그 밖의 문자("C.E.O.", "X.(주)", "IV.-")가 붙으면 표식이 아니다. 제N장/편은 공백이나 마침표+한글이 있어야 한다
# ("제2장에서 설명한" 같은 참조가 1레벨 제목이 되지 않게). 숫자 표식 뒤에는 공백이 꼭 있어야 한다("3.5% 늘었다").
_PREFIXED_HEADING = re.compile(
    r"^\s*(?:(?:(?P<roman>[IVXLC]{1,8})\.|(?P<korean>[가나다라마바사아자차카타파하])\.)(?:\s+|(?=[가-힣]))|"
    r"제\s*(?P<chapter>\d+)\s*(?:장|편)(?:\.?\s+|\.(?=[가-힣]))|"
    r"(?P<number>\d+(?:\.\d+)*)\.?\s+)(?P<title>\S.*)"
)
_LIST_DATE = re.compile(r"(?:\d{8}|\d{4}-\d{2}-\d{2})")


def classify_doc_type(report_nm: str) -> str:
    """목록의 원문 보고서명만으로 문서 계열을 판정한다."""
    compact = _SPACE.sub("", normalize("NFKC", report_nm))
    if "증권신고서" in compact:
        return DOC_SECURITIES
    if "사업보고서" in compact:
        return DOC_ANNUAL
    if "분기보고서" in compact or "반기보고서" in compact:
        return DOC_QUARTERLY
    return DOC_UNKNOWN


def _family_key(report_nm: str) -> str:
    return _SPACE.sub("", _CORRECTION.sub("", normalize("NFKC", report_nm))).strip()


def _filing_identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    rcept_no, rcept_dt, report_nm = row.get("rcept_no"), row.get("rcept_dt"), row.get("report_nm")
    if not (isinstance(rcept_no, str) and rcept_no.isdigit() and len(rcept_no) == 14):
        raise FilingSchemaError("공시 rcept_no는 숫자 14자리여야 합니다")
    if not (isinstance(rcept_dt, str) and rcept_dt.isdigit() and len(rcept_dt) == 8):
        raise FilingSchemaError("공시 rcept_dt는 YYYYMMDD여야 합니다")
    if not isinstance(report_nm, str) or not report_nm.strip():
        raise FilingSchemaError("공시 report_nm이 없습니다")
    return rcept_no, rcept_dt, report_nm


def resolve_current_filings(rows: Sequence[Mapping[str, Any]]) -> tuple[Filing, ...]:
    """원공시와 정정공시를 같은 보고서 가족으로 묶어 최신 유효본을 표시한다."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        _, _, report_nm = _filing_identity(row)
        grouped.setdefault(_family_key(report_nm), []).append(row)
    resolved: list[Filing] = []
    for family_rows in grouped.values():
        ordered = sorted(family_rows, key=lambda item: (_filing_identity(item)[1], _filing_identity(item)[0]))
        original = _filing_identity(ordered[0])[0]
        current = _filing_identity(ordered[-1])[0]
        for row in ordered:
            rcept_no, rcept_dt, report_nm = _filing_identity(row)
            resolved.append(Filing(rcept_no, rcept_dt, report_nm, classify_doc_type(report_nm), original, rcept_no == current, row))
    return tuple(sorted(resolved, key=lambda item: (item.rcept_dt, item.rcept_no)))


def _parse_list_date(raw: str, *, field: str) -> date:
    if not isinstance(raw, str) or _LIST_DATE.fullmatch(raw) is None:
        raise FilingArgumentError(f"{field}은 YYYY-MM-DD 또는 YYYYMMDD여야 합니다")
    date_format = "%Y-%m-%d" if "-" in raw else "%Y%m%d"
    try:
        return datetime.strptime(raw, date_format).date()
    except ValueError as exc:
        raise FilingArgumentError(f"{field}이 유효한 날짜가 아닙니다") from exc


def list_filings(
    reader: FilingReader,
    *,
    corp_code: str,
    from_date: str,
    to_date: str,
    filing_type: str | None = None,
) -> tuple[Filing, ...]:
    """목록 reader의 결과를 최신 유효본 정보가 있는 공시 목록으로 정규화한다."""
    begin_date = _parse_list_date(from_date, field="from_date")
    end_date = _parse_list_date(to_date, field="to_date")
    if begin_date > end_date:
        raise FilingArgumentError("from_date는 to_date보다 늦을 수 없습니다")
    if filing_type is not None and filing_type not in FILING_TYPES:
        raise FilingArgumentError("공시유형은 DART 대분류 코드 A~J여야 합니다")
    return resolve_current_filings(
        reader.list_disclosures(
            corp_code,
            begin_date=begin_date,
            end_date=end_date,
            disclosure_type=filing_type,
        )
    )


def _safe_archive_members(payload: bytes) -> tuple[ZipFile, tuple[str, ...]]:
    if not isinstance(payload, bytes) or not payload:
        raise FilingArchiveError("document.xml 응답이 비어 있습니다")
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise FilingArchiveError("document.xml ZIP이 허용 크기를 초과했습니다")
    try:
        archive = ZipFile(BytesIO(payload))
    except BadZipFile as exc:
        raise FilingArchiveError("document.xml 응답이 정상 ZIP이 아닙니다") from exc
    infos = archive.infolist()
    if not infos or len(infos) > MAX_ARCHIVE_FILES:
        archive.close()
        raise FilingArchiveError("document.xml ZIP 항목 수가 안전 범위를 벗어납니다")
    total = 0
    names: list[str] = []
    for info in infos:
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
            archive.close()
            raise FilingArchiveError("document.xml ZIP에 경로 탈출 항목이 있습니다")
        if info.flag_bits & 0x1:
            archive.close()
            raise FilingArchiveError("암호화된 document.xml ZIP은 지원하지 않습니다")
        total += info.file_size
        if total > MAX_UNCOMPRESSED_BYTES or (info.file_size and info.compress_size == 0) or (info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO):
            archive.close()
            raise FilingArchiveError("document.xml ZIP 압축 크기가 안전 범위를 벗어납니다")
        names.append(name)
    return archive, tuple(sorted(names))


def _decode(raw: bytes) -> tuple[str, str]:
    candidates: list[str] = []
    if raw.startswith(b"\xef\xbb\xbf"):
        candidates.append("utf-8-sig")
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates.append("utf-16")
    declaration = _XML_ENCODING.search(raw[:512])
    if declaration:
        try:
            candidates.append(declaration.group(1).decode("ascii"))
        except UnicodeDecodeError:
            pass
    candidates.extend(("utf-8", "euc-kr", "cp949"))
    tried: set[str] = set()
    for encoding in candidates:
        normalized_encoding = encoding.lower()
        if normalized_encoding in tried:
            continue
        tried.add(normalized_encoding)
        try:
            return raw.decode(encoding), encoding
        except (LookupError, UnicodeDecodeError):
            continue
    raise FilingEncodingError("공시 원문 인코딩을 판정할 수 없습니다")


def _table_cell_text(markup: str, tables: list[list[list[str]]]) -> str:
    """원문 조각만 이스케이프하고, 중첩 표는 빈 셀을 빼고 셀은 ';', 행은 '/'로 잇는다."""
    pieces = _TABLE_SENTINEL.split(markup)
    parts: list[str] = []
    for index, piece in enumerate(pieces):
        if index % 2:
            parts.append(" / ".join(" ; ".join(cell for cell in row if cell) for row in tables[int(piece)]))
        else:
            parts.append(unescape(_TAG.sub(" ", piece)).replace("\\", "\\\\").replace("|", "\\|"))
    return _SPACE.sub(" ", "".join(parts)).strip()


def _render_table(match: re.Match[str], tables: list[list[list[str]]]) -> str:
    rows: list[list[str]] = []
    for row in _TABLE_ROW.finditer(match.group(1)):
        cells = [_table_cell_text(cell.group(2) or "", tables) for cell in _TABLE_CELL.finditer(row.group(1))]
        if any(cells):
            rows.append(cells)
    if not rows:
        return "\n"
    tables.append(rows)
    return f"\n\ue000{len(tables) - 1}\ue001\n"


def _tag_break(match: re.Match[str]) -> str:
    name = _TAG_NAME.match(match.group(0))
    return "" if name is not None and name.group(1).lower() in _INLINE_TAGS else "\n"


def _text_run(run: str) -> str:
    """태그 사이 텍스트의 엔티티를 풀고, 풀어서 생긴 줄바꿈 문자는 공백으로 바꾼다.

    &#10; &#xD; &#12; &#8232; &NewLine; 등 표기·대소문자·0 채움과 상관없이 결과 문자로 판단한다.
    원문 줄바꿈은 _to_text 첫머리에서 이미 공백이 됐으므로 여기 남은 LF는 표 처리(빈 표, 남은 표 태그)가
    넣은 줄 경계다. 그 LF는 남긴다.
    """
    return "\n".join(_LINE_BREAKS.sub(" ", unescape(part)) for part in run.split("\n"))


def _markup_lines(piece: str) -> str:
    """표 밖 마크업 조각을 텍스트로 바꾼다. 인라인 태그는 지우고 나머지 태그는 줄바꿈으로 바꾼다."""
    parts: list[str] = []
    last = 0
    for match in _TAG.finditer(piece):
        parts.append(_text_run(piece[last:match.start()]))
        parts.append(_tag_break(match))
        last = match.end()
    parts.append(_text_run(piece[last:]))
    return "".join(parts)


def _to_text(markup: str, *, plain_text: bool = False) -> str:
    """원문을 줄 단위 텍스트로 바꾼다. 마크업에서 줄바꿈은 블록 태그에서만 나온다.

    마크업 원문의 줄바꿈 문자는 공백 하나가 된다. 표 셀은 원래 공백을 접으므로
    표 렌더링 결과는 같다. .txt 원문(plain_text)은 마크업이 아니므로 태그·표·엔티티 처리 없이
    str.splitlines 기준으로 줄을 나누고, 줄마다 공백을 접어 빈 줄을 버린다.
    """
    if plain_text:
        lines = (_HORIZONTAL_SPACE.sub(" ", line).strip() for line in markup.splitlines())
        return "\n".join(line for line in lines if line)
    markup = re.sub(r"&cr;", " ", markup, flags=re.I)
    markup = _LINE_BREAKS.sub(" ", markup)
    text = _COMMENT.sub(" ", markup.replace("\ue000", "&#xE000;").replace("\ue001", "&#xE001;"))
    tables: list[list[list[str]]] = []
    for _ in range(_MAX_TABLE_PASSES):
        rendered = _INNER_TABLE.sub(lambda match: _render_table(match, tables), text)
        if rendered == text:
            break
        text = rendered
    else:
        # 상한에 걸린 닫힌 표는 내부 표식까지 버리고, 고아 태그는 아래에서 별도로 정리한다.
        text = _INNER_TABLE.sub("\n", text)
    text = _RESIDUAL_TABLE.sub("\n", text)
    # 엔티티 복원 전에 표식을 분리해 원문의 사적 사용 문자를 표식으로 오인하지 않는다.
    pieces = _TABLE_SENTINEL.split(text)
    text = "".join(
        "\n" + "\n".join("| " + " | ".join(row) + " |" for row in tables[int(piece)]) + "\n" if index % 2
        else re.sub(r"[ \t\f\v]+", " ", _markup_lines(piece))
        for index, piece in enumerate(pieces)
    )
    # 줄 끝 공백과 빈 줄(공백만 있는 줄 포함)을 접는다.
    return re.sub(r"[ \t]*\n[ \t\n]*", "\n", text).strip()


def _validate_resolved_filing(filing: Filing) -> None:
    rcept_no, rcept_dt, report_nm = _filing_identity(filing.raw)
    if (rcept_no, rcept_dt, report_nm) != (filing.rcept_no, filing.rcept_dt, filing.report_nm):
        raise FilingSchemaError("Filing의 원본 identity와 정규화 필드가 다릅니다")
    classified = classify_doc_type(report_nm)
    if filing.doc_type not in REQUIRED_SECTIONS or classified != filing.doc_type:
        raise FilingSchemaError("검증된 문서 종류가 있는 Filing만 원문을 읽을 수 있습니다")


def get_filing_document(reader: FilingReader, *, filing: Filing) -> FilingDocument:
    """ZIP을 디스크에 해제하지 않고 안전 검증 뒤 텍스트로 정규화한다."""
    _validate_resolved_filing(filing)
    archive, names = _safe_archive_members(reader.get_document(filing.rcept_no))
    try:
        parts: list[str] = []
        encodings: list[str] = []
        for name in names:
            if not name.lower().endswith((".xml", ".html", ".htm", ".txt")):
                continue
            content, encoding = _decode(archive.read(name))
            parts.append(_to_text(content, plain_text=name.lower().endswith(".txt")))
            encodings.append(encoding)
    finally:
        archive.close()
    text = "\n".join(part for part in parts if part).strip()
    if not text:
        raise FilingArchiveError("document.xml ZIP에 읽을 수 있는 원문이 없습니다")
    return FilingDocument(
        filing.rcept_no,
        filing.doc_type,
        text,
        ",".join(dict.fromkeys(encodings)),
        names,
    )


def _is_table_row(line: str) -> bool:
    """양끝 공백 제거 후 길이 3 이상이며 '| '로 시작하고 ' |'로 끝나면 표 행이다.

    같은 모양의 산문도 표 행으로 취급한다. DART 산문에는 이 형태가 없다.
    """
    line = line.strip()
    return len(line) >= 3 and line.startswith("| ") and line.endswith(" |")


def _lines(text: str) -> list[str]:
    """텍스트를 줄로 나눈다. 줄 경계는 LF, CRLF, CR이다.

    get_filing_section과 normalize_sentences가 함께 써서 절 본문과 문장이 같은 줄을 본다.
    _to_text는 CR을 내보내지 않으므로 CR 경계는 밖에서 만든 FilingDocument 텍스트에만 해당한다.
    """
    return re.split(r"\r\n?|\n", text)


def normalize_sentences(text: str) -> tuple[str, ...]:
    """표 행을 뺀 산문을 문장으로 나눈다. 줄바꿈과 문장 끝 부호 뒤 공백이 경계다."""
    text = "\n".join(line for line in _lines(text) if not _is_table_row(line))
    text = normalize("NFKC", text)
    text = _NOTE_MARKER.sub(" ", text)
    sentences: list[str] = []
    for line in text.split("\n"):
        line = _HORIZONTAL_SPACE.sub(" ", line).strip()
        if not line:
            continue
        marker = _LIST_MARKER.match(line)
        start = marker.end() if marker is not None else 0
        pieces = _SENTENCE.split(line[start:])
        pieces[0] = line[:start] + pieces[0]
        sentences.extend(piece.strip() for piece in pieces if piece.strip())
    return tuple(sentences)


def _allowed_sections(doc_type: str) -> tuple[str, ...]:
    return REQUIRED_SECTIONS.get(doc_type, ()) + OPTIONAL_SECTIONS.get(doc_type, ())


def _heading_identity(line: str) -> tuple[str, int | None]:
    """제목 텍스트와 문서 계층 레벨을 반환한다.

    DART 정기보고서의 대절 로마 숫자는 1레벨, 그 아래 숫자 제목은 점으로
    나뉜 깊이에 따라 2레벨부터, 한글 순번은 3레벨로 취급한다. 접두사가 없는
    줄은 제목 후보일 수는 있지만 계층 증거는 갖지 않는다.
    """
    normalized = normalize("NFKC", line).strip()
    match = _PREFIXED_HEADING.fullmatch(normalized)
    if match is None:
        return normalized, None
    if match.group("roman") is not None or match.group("chapter") is not None:
        level = 1
    elif match.group("number") is not None:
        level = 1 + len(match.group("number").split("."))
    else:
        level = 3
    return match.group("title").strip(), level


def get_filing_section(document: FilingDocument, *, section: str) -> FilingSection:
    """문서 계열을 먼저 검사해 정상적인 부재와 파싱 실패를 구별한다.

    두 제목 탐색의 표 행 검사는 외부에서 직접 구성한 FilingDocument 텍스트도 방어한다.
    """
    if section not in _allowed_sections(document.doc_type):
        return FilingSection(document.rcept_no, document.doc_type, section, "section-not-applicable")
    lines = [line.strip() for line in _lines(document.text) if line.strip()]
    start: int | None = None
    section_level = 1
    for index, line in enumerate(lines):
        if _is_table_row(line):
            continue
        candidate, level = _heading_identity(line)
        if candidate == section:
            start = index + 1
            section_level = level or 1
            break
    if start is None:
        return FilingSection(document.rcept_no, document.doc_type, section, "section-not-found")
    stop = len(lines)
    known = set(sum((_allowed_sections(kind) for kind in REQUIRED_SECTIONS), ()))
    for index in range(start, len(lines)):
        if _is_table_row(lines[index]):
            continue
        candidate, level = _heading_identity(lines[index])
        same_or_higher_heading = level is not None and level <= section_level
        unnumbered_known_heading = level is None and candidate in known and candidate != section
        if same_or_higher_heading or unnumbered_known_heading:
            stop = index
            break
    body = "\n".join(lines[start:stop]).strip()
    if not body:
        return FilingSection(document.rcept_no, document.doc_type, section, "section-not-found")
    return FilingSection(document.rcept_no, document.doc_type, section, "ok", body, normalize_sentences(body))


def diff_filing_sections(a: FilingDocument, b: FilingDocument, *, section: str) -> FilingDiff:
    if a.doc_type != b.doc_type:
        raise FilingSchemaError("서로 다른 문서 종류의 절은 비교할 수 없습니다")
    left, right = get_filing_section(a, section=section), get_filing_section(b, section=section)
    if left.status != "ok" or right.status != "ok":
        raise FilingError(f"diff 대상 절을 읽을 수 없습니다: {left.status}, {right.status}")
    matcher = SequenceMatcher(a=left.sentences, b=right.sentences, autojunk=False)
    blocks: list[Mapping[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            blocks.append({"kind": tag, "before": left.sentences[i1:i2], "after": right.sentences[j1:j2]})
    return FilingDiff(a.rcept_no, b.rcept_no, section, a.doc_type, tuple(blocks))
