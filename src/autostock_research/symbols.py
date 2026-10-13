"""Local corporate-name resolution with explicit ambiguity and eligibility results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Callable, Iterable, Mapping

try:  # rapidfuzz is the production matcher; stdlib keeps offline tests portable.
    from rapidfuzz import fuzz
except ModuleNotFoundError:  # pragma: no cover - selected only in minimal environments
    from difflib import SequenceMatcher

    class _Fuzz:
        @staticmethod
        def ratio(left: str, right: str) -> float:
            return SequenceMatcher(None, left, right).ratio() * 100

    fuzz = _Fuzz()

from .dart.corp_code import CorpCodeIndex, CorpCodeRecord
from .status_codes import InvalidArgumentError
from .toss.models import SecurityType, StockInfo, StockStatus


KOREAN_MARKETS = frozenset({"KOSPI", "KOSDAQ", "KR_ETC"})
LISTING_SOURCE_TOSS = "toss"
LISTING_SOURCE_DART = "dart-corp-cls"
REASON_NOT_LISTED = "not-listed"
REASON_LISTING_UNAVAILABLE = "listing-status-unavailable"


class ListingStatusUnavailable(RuntimeError):
    pass


class DartListingClassifier:
    def __init__(self, company_lookup: Callable[[str], Mapping[str, Any]], *, clock: Callable[[], datetime] | None = None, ttl: timedelta = timedelta(hours=24)) -> None:
        self._company_lookup = company_lookup
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._ttl = ttl
        self._cache: dict[str, tuple[datetime, tuple[str, str | None]]] = {}

    def listing(self, corp_code: str) -> tuple[str, str | None]:
        """Return (corp_cls, disclosed stock_code or None when DART leaves it blank)."""
        now = self._clock()
        cached = self._cache.get(corp_code)
        if cached is not None and now < cached[0]:
            return cached[1]
        try:
            company = self._company_lookup(corp_code)
            if not isinstance(company, Mapping):
                raise ListingStatusUnavailable("DART listing status is unavailable")
            value = company.get("corp_cls")
            if not isinstance(value, str) or value not in {"Y", "K", "N", "E"}:
                raise ListingStatusUnavailable("DART listing status is unavailable")
            stock_code = company.get("stock_code")
            if stock_code is not None and not isinstance(stock_code, str):
                raise ListingStatusUnavailable("DART listing status is unavailable")
            disclosed = (stock_code.strip() or None) if isinstance(stock_code, str) else None
        except Exception:
            raise ListingStatusUnavailable("DART listing status is unavailable") from None
        result = (value, disclosed)
        self._cache[corp_code] = (now + self._ttl, result)
        return result

    def corp_cls(self, corp_code: str) -> str:
        return self.listing(corp_code)[0]


def normalize_company_name(value: str) -> str:
    """Normalize only presentation differences, never abbreviate a corporate name."""
    normalized = value.casefold().strip()
    normalized = re.sub(r"\(\s*주\s*\)", "", normalized)
    normalized = normalized.replace("주식회사", "")
    return re.sub(r"[\s\-]", "", normalized)


@dataclass(frozen=True)
class SymbolCandidate:
    symbol: str
    name: str
    corp_code: str
    market: str | None
    security_type: str | None
    is_common_share: bool | None
    status: str | None
    trading_suspended: bool | None
    liquidation_trading: bool | None
    confidence: float
    eligible: bool
    reason: str | None
    listing_source: str


class SymbolResolver:
    def __init__(self, index: CorpCodeIndex, stock_lookup: Callable[[list[str]], Iterable[StockInfo]] | None = None, *, listing_classifier: DartListingClassifier | None = None) -> None:
        if (stock_lookup is None) == (listing_classifier is None):
            raise ValueError("exactly one listing provider is required")
        self._index = index
        self._stock_lookup = stock_lookup
        self._listing_classifier = listing_classifier

    def resolve(self, query: str, *, limit: int = 5) -> tuple[SymbolCandidate, ...]:
        if not isinstance(query, str) or not query.strip():
            raise InvalidArgumentError("query must be a non-empty company name or symbol")
        if not 1 <= limit <= 5:
            raise ValueError("limit must be in 1..5")
        records = self._rank(query, self._index.all(), limit)
        if not records:
            return ()
        if self._stock_lookup is not None:
            stock_by_symbol = {stock.symbol: stock for stock in self._stock_lookup([record.stock_code for record, _ in records])}
            candidates = tuple(self._candidate(record, score, stock_by_symbol.get(record.stock_code)) for record, score in records)
        else:
            candidates = tuple(self._dart_candidate(record, score) for record, score in records)
        return tuple(sorted(candidates, key=lambda candidate: (-candidate.confidence, not candidate.eligible, candidate.symbol)))

    def _dart_candidate(self, record: CorpCodeRecord, confidence: float) -> SymbolCandidate:
        assert self._listing_classifier is not None
        try:
            corp_cls, disclosed_stock_code = self._listing_classifier.listing(record.corp_code)
            # 색인의 종목코드와 회사개황의 종목코드가 다르면 어느 쪽이 현재인지 알 수 없다.
            if disclosed_stock_code is not None and disclosed_stock_code != record.stock_code:
                raise ListingStatusUnavailable("DART stock_code differs from the corp code index")
        except ListingStatusUnavailable:
            market, eligible, reason = None, False, REASON_LISTING_UNAVAILABLE
        else:
            market = {"Y": "KOSPI", "K": "KOSDAQ", "N": "KONEX", "E": None}[corp_cls]
            eligible = corp_cls in {"Y", "K"}
            reason = None if eligible else "market-not-supported" if corp_cls == "N" else REASON_NOT_LISTED
        return SymbolCandidate(
            record.stock_code, record.corp_name, record.corp_code, market,
            None, None, None, None, None, confidence, eligible, reason, LISTING_SOURCE_DART,
        )

    @staticmethod
    def needs_user_choice(candidates: Iterable[SymbolCandidate]) -> bool:
        """Return True iff more than one candidate is eligible; never count ineligible candidates.

        False does not mean a candidate may be selected. With zero eligible
        candidates, the caller must stop and show each candidate's reason.
        Only an eligible candidate may ever be selected.
        """
        return sum(1 for candidate in candidates if candidate.eligible) > 1

    def _rank(self, query: str, records: Iterable[CorpCodeRecord], limit: int) -> list[tuple[CorpCodeRecord, float]]:
        normal = normalize_company_name(query)
        # Unlike pure-shape validators in dart/corp_code.py
        # and mcp_server.py, routing accepts lowercase input for user convenience
        # and upper-cases it, but still requires an ASCII digit: every real Korean exchange short code has one.
        # Without the digit requirement, names like LGCHEM route to exact stock_code lookup,
        # returning nothing. This difference from the strictly uppercase validators is intentional;
        # do not unify the rules.
        if re.fullmatch(r"[0-9A-Za-z]{6}", query) and re.search(r"[0-9]", query):
            exact = [(record, 100.0) for record in records if record.stock_code == query.upper()]
        else:
            materialized = list(records)
            exact = [(record, 100.0) for record in materialized if normalize_company_name(record.corp_name) == normal]
            # A short exact corporate name is still ambiguous when it is also a
            # prefix ("한화", "CJ", "SK"); never hide those choices.
            prefix = [(record, 90.0) for record in materialized if normalize_company_name(record.corp_name).startswith(normal) and normalize_company_name(record.corp_name) != normal]
            exact = exact + prefix or [(record, float(fuzz.ratio(normal, normalize_company_name(record.corp_name)))) for record in materialized]
        return sorted(exact, key=lambda item: (-item[1], item[0].stock_code))[:limit]

    @staticmethod
    def _candidate(record: CorpCodeRecord, confidence: float, stock: StockInfo | None) -> SymbolCandidate:
        if stock is None:
            return SymbolCandidate(record.stock_code, record.corp_name, record.corp_code, None, None, None, None, None, None, confidence, False, "stock-not-found", LISTING_SOURCE_TOSS)
        detail = stock.koreanMarketDetail
        liquidation = None if detail is None else detail.liquidationTrading
        suspended = None if detail is None else detail.krxTradingSuspended
        reason: str | None = None
        if stock.market not in KOREAN_MARKETS:
            reason = "market-not-supported"
        elif stock.securityType is not SecurityType.STOCK:
            reason = "asset-type-not-supported"
        elif stock.status is StockStatus.SCHEDULED:
            reason = "listing-scheduled"
        elif stock.status is StockStatus.DELISTED:
            reason = "delisted"
        elif detail is None or detail.liquidationTrading:
            reason = "liquidation-trading"
        return SymbolCandidate(
            record.stock_code, stock.name, record.corp_code, stock.market, stock.securityType.value,
            stock.isCommonShare, stock.status.value, suspended, liquidation, confidence, reason is None, reason,
            LISTING_SOURCE_TOSS,
        )
