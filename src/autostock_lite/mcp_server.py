"""MCP 2.0 read-only research surface and injectable tool-service boundary.

The public boundary deliberately contains no account or order operations."""

from __future__ import annotations

import inspect
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from .provenance import dart_public_url, redact, safe_params_hash
from .status_codes import ERROR_CODES, UNAVAILABLE_CODES, ErrorCode


Handler = Callable[..., Any | Awaitable[Any]]
Clock = Callable[[], datetime]


# MCP 클라이언트 모델에 전달하는 서버 안내문.
SERVER_INSTRUCTIONS = """\
autostock-lite는 한국 상장기업을 읽기 전용으로 관찰하는 MCP 서버다. DART 공시 도구는 늘 쓸 수 있고, 시세·호가·순위 같은 시장 데이터 도구는 사용자가 토스 키를 설정했을 때만 목록에 나타난다. 투자 자문이 아니며 주문·계좌 기능은 없다.

[숫자]
- 도구가 돌려준 숫자만 보여 준다. 숫자마다 기준 시각(as_of 또는 fetched_at)과 출처 보고서나 기간을 함께 적는다.
- 합계·평균·비율·증감률·성장률을 직접 계산하지 않는다. growth_metrics처럼 도구가 계산해 돌려준 값만 그 도구의 결과로 인용한다.
- 값이 없으면 추정하지 말고 무엇이 없는지 그대로 말한다. 결과가 blocked나 unavailable이면 error 코드를 알리고 그 값은 비워 둔다.

[권유 금지]
- 매수·매도·보유를 권하지 않는다. 목표가나 진입가를 제시하지 않는다. 주가나 실적의 방향을 예측하지 않는다. 사용자가 요청해도 같다.

[종목 찾기: resolve_symbol]
- eligible: false인 후보는 절대 고르지 않는다.
- eligible 후보가 없으면 후보마다 reason을 보여 주고 거기서 멈춘다.
- eligible 후보가 둘 이상이면 임의로 고르지 말고 사용자에게 고르게 한다.
- listing_source가 dart-corp-cls인 후보는 거래정지·정리매매·관리종목·상장폐지 예정 여부를 알 수 없다고 함께 적는다.

[재무제표: get_financial_statements]
- 공시된 계정과 금액을 그대로 옮긴다. 사업연도, 보고서 종류(reprt_code), 연결·별도 구분(fs_div), 결산일, 금액 단위(unit_note)를 함께 밝힌다.

[공시 원문: get_filing_document, get_filing_section, diff_filing_sections]
- 같은 세션에서 list_filings로 그 접수번호를 먼저 조회해야 한다. 아니면 filing-context-unavailable로 막힌다.
- 읽을 수 있는 원문은 사업보고서, 반기·분기보고서, 증권신고서뿐이다.

[토스 키가 설정된 경우에만]
- 일봉은 사용자가 따로 말하지 않으면 to를 오늘 KST 00:00의 1초 전(전날 23:59:59+09:00)으로 두어 끝나지 않은 오늘 봉을 뺀다. get_candles 결과에 오늘 봉이 있으면 같은 기준으로 뺀다. 오늘 봉은 사용자가 명시적으로 요청할 때만 넣고 "장중 잠정"이라고 표시한다.
- get_rankings의 market은 국가 코드 KR 또는 US다. KOSPI·KOSDAQ을 넣지 않는다.
- 호가는 도구가 돌려준 단계별 가격과 잔량을 그대로 보여 준다. 잔량 합계나 매수·매도 불균형 수치를 만들지 않는다.

[마무리]
- 모든 답변의 마지막 줄에 "이 내용은 정보 제공용이며 투자 권유가 아닙니다."라고 한 줄로 적는다.
"""


class UnknownToolError(LookupError):
    """The requested tool is not part of this server's registry."""

    def __init__(self, name: object) -> None:
        shown = str(name)
        if len(shown) > 80:
            shown = shown[:80] + "..."
        super().__init__(f"Unknown tool: {shown}")
        self.tool_name = name


class ToolArgumentsError(ValueError):
    """The arguments do not satisfy the tool's input schema."""


_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "crtfc_key",
        "client_secret",
        "toss_client_id",
        "toss_client_secret",
        "dart_api_key",
    }
)


def _sensitive_key(key: object) -> bool:
    return str(key).strip().lower().replace("-", "_") in _SENSITIVE_KEYS


def _seal(value: Any, *, secret_values: tuple[str, ...]) -> Any:
    """Apply the shared redactor plus MCP-boundary credential masking."""

    value = redact(value, secret_values=secret_values)
    if isinstance(value, Mapping):
        return {
            str(key): "***REDACTED***"
            if _sensitive_key(key)
            else _seal(item, secret_values=secret_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_seal(item, secret_values=secret_values) for item in value]
    return value


def _jsonable(value: Any) -> Any:
    """Convert strict internal models to JSON values without guessing fields."""

    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    raise TypeError(f"MCP handler returned a non-JSON value: {type(value).__name__}")


def _object(
    properties: Mapping[str, Any],
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties),
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _string(**extra: Any) -> dict[str, Any]:
    return {"type": "string", **extra}


def _integer(**extra: Any) -> dict[str, Any]:
    return {"type": "integer", **extra}


def _strings(*, min_items: int = 1, **extra: Any) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string"},
        "minItems": min_items,
        **extra,
    }


def _string_array(item_schema: Mapping[str, Any], *, min_items: int = 1, **extra: Any) -> dict[str, Any]:
    return {
        "type": "array",
        "items": dict(item_schema),
        "minItems": min_items,
        **extra,
    }


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    source: str
    reference_kind: str
    reference_value: str

    def as_mcp_tool(self) -> types.Tool:
        return types.Tool(
            name=self.name,
            description=self.description,
            inputSchema=dict(self.input_schema),
            annotations=types.ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=self.source in {"toss", "dart"},
            ),
        )


def _spec(
    name: str,
    description: str,
    schema: Mapping[str, Any],
    source: str,
    reference_value: str,
    *,
    reference_kind: str = "endpoint",
) -> ToolSpec:
    return ToolSpec(name, description, schema, source, reference_kind, reference_value)


# Python's \d also matches non-ASCII digits, so identifiers spell out [0-9].
_SYMBOL = _string(pattern=r"^[0-9A-Z]{6}$")
_CORP = _string(pattern=r"^[0-9]{8}$")
_REPORT = _string(enum=["11011", "11012", "11013", "11014"], default="11011")
_FS_DIV = _string(enum=["CFS", "OFS"], default="CFS")
_YEAR = _string(pattern=r"^[0-9]{4}$")
_RCEPT = _string(pattern=r"^[0-9]{14}$")
_CANDLES = {
    "type": "array",
    "minItems": 252,
    "items": _object(
        {
            "timestamp": _string(format="date-time"),
            "openPrice": _string(),
            "highPrice": _string(),
            "lowPrice": _string(),
            "closePrice": _string(),
            "volume": _string(),
            "currency": _string(pattern=r"^[A-Z]{3}$"),
        },
        ("timestamp", "openPrice", "highPrice", "lowPrice", "closePrice", "volume", "currency"),
    ),
}


TOOL_SPECS: tuple[ToolSpec, ...] = (
    _spec("resolve_symbol", "Resolve a Korean company name or symbol to candidates. Each candidate has eligible, reason, market and listing_source (toss or dart-corp-cls); never select an ineligible candidate.", _object({"query": _string(minLength=1)}, ("query",)), "dart", "corpCode.xml"),
    _spec("get_stock_info", "Read stock master information.", _object({"symbols": _string_array(_SYMBOL, uniqueItems=True)}, ("symbols",)), "toss", "/api/v1/stocks"),
    _spec("get_price", "Read current prices without derived fields.", _object({"symbols": _string_array(_SYMBOL, uniqueItems=True)}, ("symbols",)), "toss", "/api/v1/prices"),
    _spec("get_price_with_change", "Read prices and deterministic daily change fields.", _object({"symbols": _string_array(_SYMBOL, uniqueItems=True)}, ("symbols",)), "toss", "price-change:1.0", reference_kind="formula"),
    _spec("get_candles", "Read one page of market candles.", _object({"symbol": _SYMBOL, "interval": _string(enum=["1m", "1d"]), "count": _integer(minimum=1, maximum=200), "before": _string(), "adjusted": {"type": "boolean", "default": True}}, ("symbol", "interval", "count")), "toss", "/api/v1/candles"),
    _spec("get_candles_range", "Read a complete candle range with server-side pagination.", _object({"symbol": _SYMBOL, "interval": _string(enum=["1m", "1d"]), "from": _string(format="date-time"), "to": _string(format="date-time")}, ("symbol", "interval", "from", "to")), "toss", "/api/v1/candles"),
    _spec("get_orderbook", "Read the ten-level order book.", _object({"symbol": _SYMBOL}, ("symbol",)), "toss", "/api/v1/orderbook"),
    _spec("get_stock_warnings", "Read active stock warnings.", _object({"symbol": _SYMBOL}, ("symbol",)), "toss", "/api/v1/stocks/{symbol}/warnings"),
    _spec("get_price_limits", "Read daily price limits.", _object({"symbol": _SYMBOL}, ("symbol",)), "toss", "/api/v1/price-limits"),
    _spec("get_rankings", "Read market rankings. market is the country code KR or US (not KOSPI/KOSDAQ); it maps to Toss marketCountry. TOP_GAINERS and TOP_LOSERS do not support duration=realtime (Toss returns 400 unsupported-ranking-duration).", _object({"type": _string(enum=["MARKET_TRADING_AMOUNT", "MARKET_TRADING_VOLUME", "TOP_GAINERS", "TOP_LOSERS", "TOSS_SECURITIES_TRADING_AMOUNT", "TOSS_SECURITIES_TRADING_VOLUME"]), "market": _string(enum=["KR", "US"]), "duration": _string(enum=["realtime", "1d", "1w", "1mo", "3mo", "6mo", "1y"])}, ("type", "market", "duration")), "toss", "/api/v1/rankings"),
    _spec("get_stock_investor_trading", "Read per-stock investor trading quantities.", _object({"symbol": _SYMBOL, "count": _integer(minimum=1, maximum=200), "until": _string()}, ("symbol", "count")), "toss", "/api/v1/stocks/{symbol}/investor-trading"),
    _spec("get_market_investor_trading", "Read market-wide investor trading values.", _object({"market": _string(enum=["KOSPI", "KOSDAQ"]), "interval": _string(), "count": _integer(minimum=1, maximum=200), "until": _string()}, ("market", "interval", "count")), "toss", "/api/v1/market-indicators/{market}/investor-trading"),
    _spec("get_market_calendar", "Read a market calendar day.", _object({"country": _string(enum=["KR"]), "date": _string(format="date")}, ("country", "date")), "toss", "/api/v1/market-calendar/KR"),
    _spec("get_exchange_rate", "Read the USD/KRW exchange rate.", _object({}), "toss", "/api/v1/exchange-rate"),
    _spec("get_company_profile", "Read a DART company profile.", _object({"corp_code": _CORP}, ("corp_code",)), "dart", "company.json"),
    _spec("get_financial_statements", "Read one DART single-company full financial statement set exactly as disclosed: every row with its period names and amounts, plus receipt number and balance-sheet settlement date. reprt_code: 11011 annual (default), 11012 half-year, 11013 Q1, 11014 Q3. fs_div: CFS consolidated (default), OFS separate. Amounts are the disclosed strings in each row's currency base unit (won for KRW); no account classification, sums, ratios or other derived values.", _object({"corp_code": _CORP, "bsns_year": _YEAR, "reprt_code": _REPORT, "fs_div": _FS_DIV}, ("corp_code", "bsns_year")), "dart", "fnlttSinglAcntAll.json"),
    _spec("get_shares_outstanding", "Read periodic-report share counts.", _object({"corp_code": _CORP, "year": _YEAR, "reprt_code": _REPORT}, ("corp_code", "year")), "dart", "stockTotqySttus.json"),
    _spec("get_major_shareholders", "Read periodic-report major shareholder status.", _object({"corp_code": _CORP, "year": _YEAR, "reprt_code": _REPORT}, ("corp_code", "year")), "dart", "hyslrSttus.json"),
    _spec("get_major_shareholder_changes", "Read periodic-report major shareholder changes.", _object({"corp_code": _CORP, "year": _YEAR, "reprt_code": _REPORT}, ("corp_code", "year")), "dart", "hyslrChgSttus.json"),
    _spec("get_large_holdings", "Read five-percent ownership filings.", _object({"corp_code": _CORP}, ("corp_code",)), "dart", "majorstock.json"),
    _spec("get_insider_holdings", "Read officer and principal shareholder filings.", _object({"corp_code": _CORP}, ("corp_code",)), "dart", "elestock.json"),
    _spec("list_filings", "List DART filings including corrections. type is the DART disclosure group A..J (A periodic reports, C issuance filings incl. securities registrations, ...); from must not be after to (invalid-argument).", _object({"corp_code": _CORP, "type": _string(enum=list("ABCDEFGHIJ")), "from": _string(format="date"), "to": _string(format="date")}, ("corp_code", "from", "to")), "dart", "list.json"),
    _spec("get_filing_document", "Read and safely extract a filing document. Call list_filings for the receipt first in this session (otherwise filing-context-unavailable). Only annual, half-year/quarterly reports and securities registrations are readable. Table rows appear as '| cell | cell |' lines.", _object({"rcept_no": _RCEPT}, ("rcept_no",)), "dart", "document.xml", reference_kind="filing"),
    _spec("get_filing_section", "Extract a document-type-aware filing section. Call list_filings for the receipt first in this session (otherwise filing-context-unavailable). Table rows appear as '| cell | cell |' lines; sentences exclude tables.", _object({"rcept_no": _RCEPT, "section": _string(minLength=1)}, ("rcept_no", "section")), "dart", "document.xml", reference_kind="filing"),
    _spec("diff_filing_sections", "Diff the same section across two compatible filings. Both receipts must come from list_filings earlier in this session. Sentence-level only; table-only changes do not appear.", _object({"rcept_no_a": _RCEPT, "rcept_no_b": _RCEPT, "section": _string(minLength=1)}, ("rcept_no_a", "rcept_no_b", "section")), "dart", "filing-section-diff:1.0", reference_kind="formula"),
    _spec("growth_metrics", "Calculate deterministic YoY and CAGR metrics. YoY is unavailable (nonpositive-previous-value) when the previous value is zero or negative; a sign change is labelled in sign_change.", _object({"series": _strings(), "periods": _string_array(_YEAR)}, ("series", "periods")), "computed", "growth-metrics:1.1", reference_kind="formula"),
    _spec("technical_snapshot", "Calculate deterministic indicators from adjusted daily candles.", _object({"candles": _CANDLES, "adjusted": {"type": "boolean", "const": True, "default": True}}, ("candles",)), "computed", "technical-snapshot:1.0", reference_kind="formula"),
)

TOOL_REGISTRY: Mapping[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}


class ToolService:
    """Validate public inputs, invoke trusted handlers, and seal responses."""

    def __init__(
        self,
        handlers: Mapping[str, Handler] | None = None,
        *,
        clock: Clock | None = None,
        secret_values: tuple[str, ...] = (),
    ) -> None:
        self._handlers = dict(handlers or {})
        unknown = set(self._handlers) - set(TOOL_REGISTRY)
        if unknown:
            raise ValueError(f"handlers registered for unknown tools: {sorted(unknown)}")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._secret_values = secret_values

    def list_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(spec for spec in TOOL_SPECS if spec.name in self._handlers)

    async def call(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        spec = TOOL_REGISTRY.get(name) if isinstance(name, str) else None
        if spec is None:
            raise UnknownToolError(name)
        handler = self._handlers.get(name)
        resolved = {}
        if handler is None:
            data: Any = {
                "status": "unavailable",
                "error": ErrorCode.HANDLER_NOT_CONFIGURED.value,
            }
        else:
            resolved = dict(arguments or {})
            try:
                Draft202012Validator(
                    spec.input_schema, format_checker=FormatChecker()
                ).validate(resolved)
            except ValidationError as exc:
                raise ToolArgumentsError(self._argument_problem(name, exc)) from None
            for key, property_schema in spec.input_schema.get("properties", {}).items():
                if key not in resolved and "default" in property_schema:
                    resolved[key] = property_schema["default"]
            try:
                data = handler(**resolved)
                if inspect.isawaitable(data):
                    data = await data
            except Exception as exc:
                raw_code = getattr(exc, "code", ErrorCode.INTERNAL_ERROR)
                code = raw_code.value if isinstance(raw_code, Enum) else str(raw_code)
                if code not in ERROR_CODES:
                    code = ErrorCode.INTERNAL_ERROR.value
                data = {
                    "status": "unavailable" if code in UNAVAILABLE_CODES else "blocked",
                    "error": code,
                }
        data = _jsonable(data)

        reference: dict[str, Any] = {
            "kind": spec.reference_kind,
            "value": spec.reference_value,
            "public_url": None,
        }
        if spec.source == "dart":
            reference["params_hash"] = safe_params_hash(resolved)
        if spec.reference_kind == "filing":
            rcept_no = resolved.get("rcept_no")
            if isinstance(rcept_no, str):
                reference["public_url"] = dart_public_url(rcept_no)
        provenance = data.get("provenance", []) if isinstance(data, Mapping) else []
        if not isinstance(provenance, list):
            provenance = []
        envelope = {
            "source": spec.source,
            "fetched_at": self._clock().astimezone(timezone.utc).isoformat(),
            "reference": reference,
            "provenance": provenance,
            "data": data,
        }
        return _seal(envelope, secret_values=self._secret_values)

    def _argument_problem(self, name: str, exc: ValidationError) -> str:
        # The schema error echoes caller input (whole candle arrays, for one), so keep it short.
        location = "/".join(str(part) for part in exc.absolute_path) or "(root)"
        detail = exc.message if len(exc.message) <= 200 else exc.message[:200] + "..."
        text = f"Invalid arguments for tool {name} at {location}: {detail}"
        return str(_seal(text, secret_values=self._secret_values))


def create_mcp_server(service: ToolService | None = None) -> Server:
    """Create the MCP 2.0 low-level server without opening a transport."""

    tool_service = service or ToolService()

    async def list_tools(_context: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[spec.as_mcp_tool() for spec in tool_service.list_tools()])

    async def call_tool(_context: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            envelope = await tool_service.call(params.name, params.arguments)
        except UnknownToolError as exc:
            # A JSON-RPC error the client can read; raising MCPError also keeps the
            # dispatcher from logging a server-side traceback.
            raise MCPError(code=types.INVALID_PARAMS, message=str(exc)) from None
        except ToolArgumentsError as exc:
            # Reported as a tool execution error so the calling model can correct itself.
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc))],
                isError=True,
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False))],
            structuredContent=envelope,
        )

    return Server(
        "autostock-lite",
        version="0.1.0",
        title="autostock-lite",
        description="Read-only Korean equity research MCP server",
        instructions=SERVER_INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def run_stdio(service: ToolService | None = None) -> None:
    """Run the server on stdio.  Kept separate for transport-free tests."""

    server = create_mcp_server(service)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
