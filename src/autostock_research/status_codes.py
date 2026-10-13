"""Registry of failure and warning codes exposed by the MCP tools.

Runtime modules import values from here instead of inventing adjacent
spellings.  ``ToolService`` turns a handler failure into an envelope with the
exception's ``code`` only when that code is registered in ``ERROR_CODES``; any
other failure becomes ``internal-error``.  Codes in ``UNAVAILABLE_CODES`` mean
the source could not be reached or refused the caller (envelope status
``unavailable``); every other code means the server declined to return data it
could not verify (status ``blocked``).
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    # Caller arguments that pass the input schema but cannot be served.
    INVALID_ARGUMENT = "invalid-argument"

    # OpenDART transport and business statuses.
    DART_NO_DATA = "dart-no-data"
    DART_AUTH_FAILED = "dart-auth-failed"
    DART_RATE_LIMITED = "dart-rate-limited"
    DART_REQUEST_INVALID = "dart-request-invalid"
    DART_MAINTENANCE = "dart-maintenance"
    DART_RESPONSE_INVALID = "dart-response-invalid"
    DART_REQUEST_FAILED = "dart-request-failed"

    # Toss Securities transport and response contract.
    TOSS_AUTH_FAILED = "toss-auth-failed"
    TOSS_IP_NOT_ALLOWED = "toss-ip-not-allowed"
    TOSS_RATE_LIMITED = "toss-rate-limited"
    TOSS_RESPONSE_INVALID = "toss-response-invalid"
    TOSS_REQUEST_FAILED = "toss-request-failed"

    # Disclosed financial statements (get_financial_statements).
    FINANCIALS_NO_DATA = "financials-no-data"
    FINANCIALS_IDENTITY_MISMATCH = "financials-identity-mismatch"
    FINANCIALS_SCHEMA_INVALID = "financials-schema-invalid"
    FINANCIALS_DATE_UNAVAILABLE = "financials-date-unavailable"

    # Deterministic calculations over caller-supplied inputs.
    GROWTH_INPUT_INVALID = "growth-input-invalid"
    TECHNICAL_INPUT_INVALID = "technical-input-invalid"
    TECHNICAL_INSUFFICIENT_HISTORY = "technical-insufficient-history"

    # Symbol, filing, and ownership paths.
    ASSET_TYPE_NOT_SUPPORTED = "asset-type-not-supported"
    STOCK_NOT_FOUND = "stock-not-found"
    SECTION_NOT_APPLICABLE = "section-not-applicable"
    SECTION_NOT_FOUND = "section-not-found"
    UNKNOWN_DOC_TYPE = "unknown-doc-type"
    FILING_ERROR = "filing-error"
    FILING_SCHEMA_INVALID = "filing-schema-invalid"
    FILING_ARCHIVE_INVALID = "filing-archive-invalid"
    FILING_ENCODING_INVALID = "filing-encoding-invalid"
    FILING_CONTEXT_UNAVAILABLE = "filing-context-unavailable"
    OWNERSHIP_ERROR = "ownership-error"
    OWNERSHIP_SCHEMA_INVALID = "ownership-schema-invalid"

    # Tool service.
    HANDLER_NOT_CONFIGURED = "handler-not-configured"
    INTERNAL_ERROR = "internal-error"


class WarningCode(str, Enum):
    SOURCE_TEXT_UNREADABLE = "source-text-unreadable"


ERROR_CODES = frozenset(code.value for code in ErrorCode)
UNAVAILABLE_CODES = frozenset(
    code.value
    for code in (
        ErrorCode.DART_AUTH_FAILED,
        ErrorCode.DART_RATE_LIMITED,
        ErrorCode.DART_MAINTENANCE,
        ErrorCode.DART_REQUEST_FAILED,
        ErrorCode.TOSS_AUTH_FAILED,
        ErrorCode.TOSS_IP_NOT_ALLOWED,
        ErrorCode.TOSS_RATE_LIMITED,
        ErrorCode.TOSS_REQUEST_FAILED,
        ErrorCode.HANDLER_NOT_CONFIGURED,
        ErrorCode.INTERNAL_ERROR,
    )
)
WARNING_CODES = frozenset(code.value for code in WarningCode)


class InvalidArgumentError(ValueError):
    """Arguments that are well-formed but cannot be served, e.g. from > to."""

    code = ErrorCode.INVALID_ARGUMENT
