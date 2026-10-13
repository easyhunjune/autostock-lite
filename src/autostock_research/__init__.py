"""autostock-lite의 읽기 전용 기업 리서치 패키지."""

from .calc import growth_metrics, technical_snapshot
from .config import ConfigError, ResearchSettings, load_settings
from .mcp_server import ToolService, create_mcp_server, run_stdio
from .mcp_runtime import McpRuntime, create_live_runtime, run_live_stdio
from .status_codes import ErrorCode, WarningCode

__version__ = "0.1.0"

__all__ = [
    "ErrorCode",
    "WarningCode",
    "ToolService",
    "create_mcp_server",
    "run_stdio",
    "McpRuntime",
    "create_live_runtime",
    "run_live_stdio",
    "ResearchSettings",
    "ConfigError",
    "load_settings",
    "growth_metrics",
    "technical_snapshot",
]
