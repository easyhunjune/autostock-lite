"""Compose DART research tools and optional Toss market data for read-only MCP."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Callable, ContextManager, Mapping, TextIO

import httpx

from .calc.growth import growth_metrics
from .calc.technical import TechnicalInputError, technical_snapshot
from .config import ConfigError, ResearchSettings, load_settings
from .dart.statements import get_financial_statements
from .dart.client import DartReadonlyClient
from .dart.corp_code import CorpCodeIndex, CorpCodeRefreshService
from .dart.filings import (
    Filing,
    FilingDocument,
    diff_filing_sections,
    get_filing_document,
    get_filing_section,
    list_filings,
)
from .dart.ownership import (
    get_insider_holdings,
    get_large_holdings,
    get_major_shareholder_changes,
    get_major_shareholders,
    get_shares_outstanding,
)
from .mcp_server import ToolService, run_stdio
from . import paths
from .status_codes import ErrorCode
from .symbols import DartListingClassifier, SymbolResolver
from .toss.auth import TossTokenProvider
from .toss.client import TossMarketData
from .toss.live import TossLiveClient
from .toss.models import Candle, StrictModelError
from .toss.session_lock import TossSessionBusy, TossSessionLock, TossSessionLockError


class UtcClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FilingContextError(ValueError):
    code = ErrorCode.FILING_CONTEXT_UNAVAILABLE


class McpRuntime:
    """Own live dependencies, registries, and all safe tool adapters."""

    def __init__(
        self,
        *,
        http_client: httpx.Client,
        dart: Any,
        toss: TossMarketData | None,
        corp_index: CorpCodeIndex,
        corp_refresh: CorpCodeRefreshService | None,
        clock: Any,
        secret_values: tuple[str, ...] = (),
        close_http_client: bool = True,
    ) -> None:
        self.http_client = http_client
        self.dart = dart
        self.toss = toss
        self.corp_index = corp_index
        self.corp_refresh = corp_refresh
        self.clock = clock
        self.listing = DartListingClassifier(self.dart.get_company, clock=clock.now)
        self._filings: dict[str, Filing] = {}
        self._documents: dict[str, FilingDocument] = {}
        self._closed = False
        self._close_http_client = close_http_client
        self.handlers = self._build_handlers()
        self.service = ToolService(
            self.handlers,
            clock=clock.now,
            secret_values=secret_values,
        )

    @property
    def toss_enabled(self) -> bool:
        return self.toss is not None

    def close(self) -> None:
        if not self._closed and self._close_http_client:
            self.http_client.close()
        self._closed = True

    def _resolve_symbol(self, *, query: str) -> Any:
        if self.corp_refresh is not None:
            self.corp_refresh.refresh_if_due()
        if self.toss is not None:
            resolver = SymbolResolver(self.corp_index, self.toss.lookup_stock_info)
        else:
            resolver = SymbolResolver(self.corp_index, listing_classifier=self.listing)
        return resolver.resolve(query)

    def _list_filings(self, **arguments: Any) -> Any:
        filings = list_filings(
            self.dart,
            corp_code=arguments["corp_code"],
            from_date=arguments["from"],
            to_date=arguments["to"],
            filing_type=arguments.get("type"),
        )
        for filing in filings:
            self._filings[filing.rcept_no] = filing
        return filings

    def _filing(self, rcept_no: str) -> Filing:
        try:
            return self._filings[rcept_no]
        except KeyError as exc:
            raise FilingContextError(
                "receipt must first be resolved by list_filings"
            ) from exc

    def _document(self, rcept_no: str) -> FilingDocument:
        cached = self._documents.get(rcept_no)
        if cached is not None:
            return cached
        document = get_filing_document(self.dart, filing=self._filing(rcept_no))
        self._documents[rcept_no] = document
        return document

    @staticmethod
    def _technical_snapshot(**arguments: Any) -> Any:
        # 캔들은 호출자가 넘긴 값이므로 모델 오류를 토스 응답 오류가 아닌 입력 오류로 보고한다.
        try:
            candles = tuple(Candle.from_dict(item) for item in arguments["candles"])
        except StrictModelError as exc:
            raise TechnicalInputError("candles do not match the candle contract") from exc
        return technical_snapshot(candles, adjusted=arguments.get("adjusted", True))

    def _build_handlers(self) -> dict[str, Callable[..., Any]]:
        handlers = {
            "resolve_symbol": self._resolve_symbol,
            "get_company_profile": self.dart.get_company,
            "get_financial_statements": lambda **a: get_financial_statements(self.dart, **a),
            "get_shares_outstanding": lambda **a: get_shares_outstanding(self.dart, **a),
            "get_major_shareholders": lambda **a: get_major_shareholders(self.dart, **a),
            "get_major_shareholder_changes": lambda **a: get_major_shareholder_changes(self.dart, **a),
            "get_large_holdings": lambda **a: get_large_holdings(self.dart, **a),
            "get_insider_holdings": lambda **a: get_insider_holdings(self.dart, **a),
            "list_filings": self._list_filings,
            "get_filing_document": lambda **a: self._document(a["rcept_no"]),
            "get_filing_section": lambda **a: get_filing_section(
                self._document(a["rcept_no"]), section=a["section"]
            ),
            "diff_filing_sections": lambda **a: diff_filing_sections(
                self._document(a["rcept_no_a"]),
                self._document(a["rcept_no_b"]),
                section=a["section"],
            ),
            "growth_metrics": lambda **a: growth_metrics(a["series"], a["periods"]),
        }
        if self.toss is not None:
            handlers.update({
                "get_stock_info": self.toss.get_stock_info,
                "get_price": self.toss.get_price,
                "get_price_with_change": self.toss.get_price_with_change,
                "get_candles": self.toss.get_candles,
                "get_candles_range": lambda **a: self.toss.get_candles_range(
                    a["symbol"], interval=a["interval"], from_=a["from"], to=a["to"], adjusted=True
                ),
                "get_orderbook": self.toss.get_orderbook,
                "get_stock_warnings": self.toss.get_stock_warnings,
                "get_price_limits": self.toss.get_price_limits,
                "get_rankings": self.toss.get_rankings,
                "get_stock_investor_trading": self.toss.get_stock_investor_trading,
                "get_market_investor_trading": self.toss.get_market_investor_trading,
                "get_market_calendar": self.toss.get_market_calendar,
                "get_exchange_rate": self.toss.get_exchange_rate,
                "technical_snapshot": self._technical_snapshot,
            })
        return handlers


def create_live_runtime(
    settings: ResearchSettings,
    *,
    http_client: httpx.Client | None = None,
    state_dir: str | Path | None = None,
) -> McpRuntime:
    owned_http = http_client is None
    http = http_client if http_client is not None else httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        clock = UtcClock()
        dart = DartReadonlyClient(settings.dart_api_key or "", http_client=http)
        corp_index = CorpCodeIndex(paths.corp_code_db_path(state_dir))
        corp_refresh = CorpCodeRefreshService(
            corp_index,
            api_key=settings.dart_api_key or "",
            http_client=http,
            now=clock.now,
        )
        toss = None
        if settings.toss_enabled:
            tokens = TossTokenProvider(
                settings.toss_client_id or "",
                settings.toss_client_secret or "",
                http_client=http,
            )
            toss_live = TossLiveClient(http_client=http, token_provider=tokens)
            toss = TossMarketData(toss_live.get_json, readonly=toss_live.readonly, now=clock.now)
        return McpRuntime(
            http_client=http,
            dart=dart,
            toss=toss,
            corp_index=corp_index,
            corp_refresh=corp_refresh,
            clock=clock,
            secret_values=settings.secret_values(),
            close_http_client=owned_http,
        )
    except Exception:
        if owned_http:
            http.close()
        raise


def run_live_stdio(
    *,
    env_file: str | Path | None = None,
    state_dir: str | Path | None = None,
    session_lock: ContextManager[object] | None = None,
    lock_path: str | Path | None = None,
    output: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    http_client: httpx.Client | None = None,
    serve: Callable[[ToolService], Any] | None = None,
) -> int:
    output = sys.stderr if output is None else output

    def _emit(message: str) -> None:
        if output is not None:
            print(message, file=output)

    try:
        settings = load_settings(env_file, environ=environ)
    except ConfigError as exc:
        _emit(f"FAIL    : {exc}")
        return 1

    lock = None
    if settings.toss_enabled:
        if session_lock is not None and lock_path is not None:
            raise ValueError("session_lock과 lock_path는 동시에 지정할 수 없습니다")
        try:
            lock = session_lock if session_lock is not None else TossSessionLock(
                paths.toss_session_lock_path(state_dir) if lock_path is None else lock_path,
                timeout=0,
            )
            lock.__enter__()
        except TossSessionBusy:
            _emit(
                "FAIL    : 다른 프로세스가 토스 세션을 사용 중입니다. "
                "잠시 후 다시 시도하세요"
            )
            return 1
        except TossSessionLockError:
            _emit("FAIL    : 토스 세션 잠금을 획득하지 못했습니다")
            return 1
        except RuntimeError as exc:
            detail = exc.__class__.__name__
            _emit(f"FAIL    : 토스 세션 잠금을 준비하지 못했습니다 ({detail})")
            return 1
        except OSError as exc:
            detail = exc.strerror or exc.__class__.__name__
            _emit(f"FAIL    : 토스 세션 잠금을 준비하지 못했습니다 ({detail})")
            return 1

    try:
        runtime = create_live_runtime(settings, http_client=http_client, state_dir=state_dir)
        try:
            if serve is None:
                asyncio.run(run_stdio(runtime.service))
            else:
                serve(runtime.service)
        finally:
            runtime.close()
    except BaseException:
        if lock is not None:
            lock.__exit__(*sys.exc_info())
        raise

    try:
        if lock is not None:
            lock.__exit__(None, None, None)
    except OSError as exc:
        detail = exc.strerror or exc.__class__.__name__
        _emit(f"FAIL    : 토스 세션 잠금을 해제하지 못했습니다 ({detail})")
        return 1
    return 0


__all__ = [
    "McpRuntime",
    "UtcClock",
    "create_live_runtime",
    "run_live_stdio",
]
