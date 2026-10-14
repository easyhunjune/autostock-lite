"""Safe, versioned local index for OpenDART's ``corpCode.xml`` archive."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import re
import sqlite3
from typing import Callable, Iterable, Mapping, Protocol
from zipfile import BadZipFile, ZipFile

from .client import raise_for_status_body
from ..status_codes import ErrorCode


MAX_ARCHIVE_BYTES = 10_000_000
MAX_XML_BYTES = 100_000_000
MAX_COMPRESSION_RATIO = 200


class CorpCodeError(RuntimeError):
    """The upstream archive cannot safely be used as a symbol index."""

    code = ErrorCode.DART_RESPONSE_INVALID


class CorpCodeDownloadError(CorpCodeError):
    """The archive could not be downloaded (connection or HTTP failure)."""

    code = ErrorCode.DART_REQUEST_FAILED


class CorpCodeIndexError(CorpCodeError):
    """The local index metadata is unreadable; this is server state, not DART data."""

    code = ErrorCode.INTERNAL_ERROR


@dataclass(frozen=True)
class CorpCodeRecord:
    corp_code: str
    corp_name: str
    stock_code: str
    modify_date: str


class CorpCodeHttpResponse(Protocol):
    status_code: int
    content: bytes


class CorpCodeHttpClient(Protocol):
    def get(self, url: str, *, params: Mapping[str, str], headers: Mapping[str, str]) -> CorpCodeHttpResponse: ...


@dataclass(frozen=True)
class CorpCodeRefreshResult:
    refreshed: bool
    record_count: int
    refreshed_at: datetime


def parse_corp_code_archive(data: bytes) -> tuple[CorpCodeRecord, ...]:
    """Parse only the single file promised by DART, fail closed on bad ZIP/XML."""
    if not data or len(data) > MAX_ARCHIVE_BYTES:
        raise CorpCodeError("corpCode archive exceeds the configured size limit")
    try:
        with ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) != 1 or infos[0].filename != "CORPCODE.xml":
                raise CorpCodeError("corpCode archive must contain only CORPCODE.xml")
            info = infos[0]
            if (
                info.file_size <= 0
                or info.file_size > MAX_XML_BYTES
                or info.compress_size <= 0
                or info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
            ):
                raise CorpCodeError("corpCode archive has unsafe compression metadata")
            xml = archive.read(info)
    except BadZipFile as exc:
        raise CorpCodeError("corpCode response is not a valid ZIP archive") from exc

    try:
        # ElementTree is sufficient for this small fixed schema and keeps the
        # offline index usable before optional project dependencies are installed.
        from xml.etree import ElementTree as etree

        root = etree.fromstring(xml)
    except Exception as exc:
        raise CorpCodeError("corpCode XML could not be parsed") from exc

    records: list[CorpCodeRecord] = []
    seen_codes: set[str] = set()
    for node in root.findall("list"):
        corp_code = (node.findtext("corp_code") or "").strip()
        corp_name = (node.findtext("corp_name") or "").strip()
        stock_code = (node.findtext("stock_code") or "").strip()
        modify_date = (node.findtext("modify_date") or "").strip()
        if not (len(corp_code) == 8 and corp_code.isdigit() and corp_name):
            raise CorpCodeError("corpCode XML contains an invalid company record")
        if stock_code and not re.fullmatch(r"[0-9A-Z]{6}", stock_code):
            raise CorpCodeError("corpCode XML contains an invalid stock code")
        if modify_date and not (len(modify_date) == 8 and modify_date.isdigit()):
            raise CorpCodeError("corpCode XML contains an invalid modification date")
        if corp_code in seen_codes:
            raise CorpCodeError("corpCode XML contains a duplicate corporate code")
        seen_codes.add(corp_code)
        if stock_code:  # only listed companies enter the local symbol index
            records.append(CorpCodeRecord(corp_code, corp_name, stock_code, modify_date))
    return tuple(records)


class CorpCodeIndex:
    """SQLite-backed immutable snapshots; refresh replaces the current index atomically."""

    def __init__(self, database: str | Path) -> None:
        self.path = Path(database)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS corp_codes (
                  corp_code TEXT PRIMARY KEY, corp_name TEXT NOT NULL,
                  stock_code TEXT NOT NULL UNIQUE, modify_date TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS corp_codes_name ON corp_codes(corp_name);
                CREATE TABLE IF NOT EXISTS corp_code_meta (
                  key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def replace(self, records: Iterable[CorpCodeRecord], *, refreshed_at: str) -> None:
        rows = list(records)
        if not rows:
            raise CorpCodeError("refusing to replace the index with an empty snapshot")
        if len({row.corp_code for row in rows}) != len(rows) or len({row.stock_code for row in rows}) != len(rows):
            raise CorpCodeError("corpCode snapshot contains duplicate index keys")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM corp_codes")
            db.executemany(
                "INSERT INTO corp_codes(corp_code, corp_name, stock_code, modify_date) VALUES (?, ?, ?, ?)",
                [(r.corp_code, r.corp_name, r.stock_code, r.modify_date) for r in rows],
            )
            db.execute(
                "INSERT INTO corp_code_meta(key, value) VALUES ('refreshed_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (refreshed_at,),
            )

    def all(self) -> tuple[CorpCodeRecord, ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT corp_code, corp_name, stock_code, modify_date FROM corp_codes ORDER BY stock_code"
            ).fetchall()
        return tuple(CorpCodeRecord(**dict(row)) for row in rows)

    def refreshed_at(self) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM corp_code_meta WHERE key='refreshed_at'").fetchone()
        return None if row is None else str(row["value"])

    def refresh_from_archive(self, archive: bytes, *, refreshed_at: str) -> int:
        records = parse_corp_code_archive(archive)
        self.replace(records, refreshed_at=refreshed_at)
        return len(records)


class CorpCodeRefreshService:
    """Downloads the DART archive only when the local weekly snapshot is stale."""

    URL = "https://opendart.fss.or.kr/api/corpCode.xml"
    INTERVAL = timedelta(days=7)

    def __init__(
        self,
        index: CorpCodeIndex,
        *,
        api_key: str,
        http_client: CorpCodeHttpClient,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("DART API key is required for corpCode refresh")
        self._index = index
        self._api_key = api_key
        self._http = http_client
        self._now = now or (lambda: datetime.now(timezone.utc))

    def refresh_if_due(self, *, force: bool = False) -> CorpCodeRefreshResult:
        current = self._as_utc(self._now())
        existing = self._read_refreshed_at()
        if not force and existing is not None and current - existing < self.INTERVAL:
            return CorpCodeRefreshResult(False, len(self._index.all()), existing)
        try:
            response = self._http.get(
                self.URL,
                params={"crtfc_key": self._api_key},
                headers={"Accept": "application/zip"},
            )
        except Exception as exc:
            raise CorpCodeDownloadError("could not download corpCode archive") from exc
        if response.status_code != 200:
            raise CorpCodeDownloadError(f"corpCode download failed: HTTP {response.status_code}")
        if isinstance(response.content, bytes) and not response.content.startswith(b"PK"):
            # DART answers a bad key, rate limit, maintenance or missing file (014)
            # with a short status body instead of a ZIP; surface that status's own
            # code.  An unrecognizable body falls through to the archive checks,
            # which reject it as dart-response-invalid.
            raise_for_status_body(response.content)
        # Parse before opening the write transaction, preserving the old snapshot
        # for malformed data, HTTP proxies, and all archive-validation failures.
        records = parse_corp_code_archive(response.content)
        self._index.replace(records, refreshed_at=current.isoformat())
        return CorpCodeRefreshResult(True, len(records), current)

    def _read_refreshed_at(self) -> datetime | None:
        value = self._index.refreshed_at()
        if value is None:
            return None
        try:
            return self._as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError as exc:
            raise CorpCodeIndexError("corpCode index has an invalid refreshed_at value") from exc

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("corpCode refresh clock must return an aware datetime")
        return value.astimezone(timezone.utc)
