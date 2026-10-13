"""실제 키·네트워크·사용자 상태를 사용하지 않는 오프라인 스모크 테스트."""

import asyncio
from contextlib import redirect_stderr
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import anyio
import httpx
from mcp import ClientSession, types
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import create_client_server_memory_streams

import autostock_research
from autostock_research import paths
from autostock_research.cli import build_parser, doctor
from autostock_research.config import (
    ConfigError,
    ResearchSettings,
    collect_config_values,
    load_settings,
)
from autostock_research.dart.corp_code import CorpCodeRecord
from autostock_research.mcp_runtime import create_live_runtime, run_live_stdio
from autostock_research.mcp_server import (
    SERVER_INSTRUCTIONS, TOOL_REGISTRY, UnknownToolError, create_mcp_server,
)
from autostock_research.toss.session_lock import TossSessionBusy, TossSessionLock


ROOT = Path(__file__).resolve().parent.parent
DART_KEY = "test-dart-key-0001"
TOSS_ID = "test-toss-id-0001"
TOSS_SECRET = "test-toss-secret-0001"
SECRETS = (DART_KEY, TOSS_ID, TOSS_SECRET)
DART_TOOLS = {
    "resolve_symbol", "get_company_profile", "get_financial_statements", "list_filings", "get_filing_document",
    "get_filing_section", "diff_filing_sections", "get_major_shareholders",
    "get_major_shareholder_changes", "get_large_holdings", "get_insider_holdings",
    "get_shares_outstanding", "growth_metrics",
}
TOSS_TOOLS = {
    "get_stock_info", "get_price", "get_price_with_change", "get_candles",
    "get_candles_range", "get_orderbook", "get_stock_warnings", "get_price_limits",
    "get_rankings", "get_stock_investor_trading", "get_market_investor_trading",
    "get_market_calendar", "get_exchange_rate", "technical_snapshot",
}
REMOVED_TOOLS = {
    "calculate_equity_market_value", "reverse_dcf", "reverse_dcf_sensitivity",
    "resolve_wacc", "normalize_fcf", "get_financials",
}


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = {
            "APPDATA": str(self.root / "roaming"),
            "LOCALAPPDATA": str(self.root / "local"),
            "HOME": str(self.root / "home"),
            "USERPROFILE": str(self.root / "home"),
        }
        environment_patch = patch.dict(os.environ, self.environment)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        for key in ("DART_API_KEY", "TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET"):
            os.environ.pop(key, None)
        self.requests = []

    def write_env(self, path, text, *, bom=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8-sig" if bom else "utf-8")
        return path

    def mock_client(self, handler=None):
        def record(request):
            self.requests.append(request)
            if handler is None:
                raise AssertionError("HTTP 요청이 없어야 합니다")
            return handler(request)

        client = httpx.Client(transport=httpx.MockTransport(record))
        self.addCleanup(client.close)
        return client

    def settings(self, *, toss=False):
        return ResearchSettings(
            dart_api_key=DART_KEY,
            toss_client_id=TOSS_ID if toss else None,
            toss_client_secret=TOSS_SECRET if toss else None,
        )

    def runtime(self, *, toss=False, default_state=False):
        client = self.mock_client()
        runtime = create_live_runtime(
            self.settings(toss=toss), http_client=client,
            state_dir=None if default_state else self.root / "state",
        )
        self.addCleanup(runtime.close)
        return runtime

    def assert_no_secrets(self, text):
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def stdio(self, *, serve, toss=False, missing_dart=False, client=None, output=None):
        environ = dict(self.environment)
        if not missing_dart:
            environ["DART_API_KEY"] = DART_KEY
        if toss:
            environ.update(TOSS_CLIENT_ID=TOSS_ID, TOSS_CLIENT_SECRET=TOSS_SECRET)
        return run_live_stdio(
            environ=environ, state_dir=self.root / "state", serve=serve,
            http_client=client if client is not None else self.mock_client(),
            output=output if output is not None else io.StringIO(),
        )


class PackageAndPathsTests(IsolatedCase):
    def test_package_import_is_from_this_repository(self):
        # 저장소에서 바로 돌릴 때(개발 실행·editable 설치)만 확인한다.
        # 일반 pip install로 site-packages에서 불러오는 경우는 정상이므로 건너뛴다.
        location = Path(autostock_research.__file__).resolve()
        if not location.is_relative_to((ROOT / "src").resolve()):
            self.skipTest(f"installed package outside this checkout: {location}")
        self.assertEqual(location, (ROOT / "src" / "autostock_research" / "__init__.py").resolve())

    def test_injected_platform_paths_and_fallbacks(self):
        home = self.root / "injected-home"
        environ = {"APPDATA": str(self.root / "config"), "LOCALAPPDATA": str(self.root / "data")}
        for windows in (True, False):
            with self.subTest(windows=windows):
                config = self.root / "config/autostock-lite" if windows else home / ".config/autostock-lite"
                state = self.root / "data/autostock-lite" if windows else home / ".local/state/autostock-lite"
                kw = dict(environ=environ, home=home, windows=windows)
                self.assertEqual(paths.user_config_dir(**kw), config)
                self.assertEqual(paths.default_env_file(**kw), config / ".env")
                self.assertEqual(paths.user_state_dir(**kw), state)
                self.assertEqual(paths.corp_code_db_path(**kw), state / "corp_codes.sqlite3")
                self.assertEqual(paths.toss_session_lock_path(**kw), state / "toss-session.lock")
                explicit = self.root / "explicit-state"
                self.assertEqual(paths.corp_code_db_path(explicit, **kw), explicit / "corp_codes.sqlite3")
                self.assertEqual(paths.toss_session_lock_path(explicit, **kw), explicit / "toss-session.lock")
        self.assertEqual(paths.default_env_file(environ={}, home=home, windows=True), home / "AppData/Roaming/autostock-lite/.env")
        self.assertEqual(paths.user_state_dir(environ={}, home=home, windows=True), home / "AppData/Local/autostock-lite")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_default_lock_uses_isolated_state(self):
        lock_path = TossSessionLock().path.resolve()
        self.assertEqual(lock_path, paths.toss_session_lock_path().resolve())
        self.assertEqual(lock_path.parent, paths.user_state_dir().resolve())
        self.assertTrue(lock_path.is_relative_to(self.root.resolve()))
        self.assertFalse(lock_path.is_relative_to(ROOT))
        self.assertFalse(lock_path.exists())


class ConfigTests(IsolatedCase):
    def test_precedence_each_layer(self):
        user = self.write_env(paths.default_env_file(), "DART_API_KEY=test-user-key-0001\n")
        explicit = self.write_env(self.root / "explicit.env", "DART_API_KEY=test-file-key-0001\n")
        os.environ["DART_API_KEY"] = DART_KEY
        self.assertEqual(load_settings(explicit).dart_api_key, DART_KEY)
        os.environ.pop("DART_API_KEY")
        self.assertEqual(load_settings(explicit).dart_api_key, "test-file-key-0001")
        self.assertEqual(load_settings().dart_api_key, "test-user-key-0001")
        self.assertTrue(user.is_file())

    def test_per_key_merge_and_blank_values(self):
        self.write_env(paths.default_env_file(), f"DART_API_KEY=test-user-key-0001\nTOSS_CLIENT_ID=test-user-id-0001\nTOSS_CLIENT_SECRET={TOSS_SECRET}\n")
        explicit = self.write_env(self.root / "explicit.env", f"DART_API_KEY=\nTOSS_CLIENT_ID={TOSS_ID}\nTOSS_CLIENT_SECRET=\"   \"\n")
        os.environ.update(DART_API_KEY=DART_KEY, TOSS_CLIENT_ID="  ", TOSS_CLIENT_SECRET="")
        self.assertEqual(collect_config_values(explicit), {
            "DART_API_KEY": DART_KEY, "TOSS_CLIENT_ID": TOSS_ID, "TOSS_CLIENT_SECRET": TOSS_SECRET,
        })
        os.environ["DART_API_KEY"] = "  "
        self.assertEqual(collect_config_values(explicit)["DART_API_KEY"], "test-user-key-0001")

    def test_missing_dart_is_config_error(self):
        with self.assertRaises(ConfigError) as caught:
            load_settings()
        self.assertIn("DART_API_KEY", str(caught.exception))
        self.assert_no_secrets(str(caught.exception))

    def test_partial_toss_names_missing_key(self):
        for present, missing, value in (
            ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", TOSS_ID),
            ("TOSS_CLIENT_SECRET", "TOSS_CLIENT_ID", TOSS_SECRET),
        ):
            with self.subTest(present=present), self.assertRaises(ConfigError) as caught:
                ResearchSettings.from_mapping({"DART_API_KEY": DART_KEY, present: value})
            self.assertIn(missing, str(caught.exception))
            self.assert_no_secrets(str(caught.exception))

    def test_toss_enabled_and_repr(self):
        dart_only = ResearchSettings.from_mapping({"DART_API_KEY": DART_KEY})
        complete = ResearchSettings.from_mapping({"DART_API_KEY": DART_KEY, "TOSS_CLIENT_ID": TOSS_ID, "TOSS_CLIENT_SECRET": TOSS_SECRET})
        self.assertFalse(dart_only.toss_enabled)
        self.assertTrue(complete.toss_enabled)
        self.assert_no_secrets(repr(dart_only))
        self.assert_no_secrets(repr(complete))

    def test_unknown_file_key_hides_value(self):
        path = self.write_env(self.root / "unknown.env", "KRX_API_KEY=dummy-value\n")
        with self.assertRaises(ConfigError) as caught:
            load_settings(path)
        self.assertNotIn("dummy-value", str(caught.exception))

    def test_missing_explicit_and_optional_user_files(self):
        os.environ["DART_API_KEY"] = DART_KEY
        self.assertFalse(paths.default_env_file().exists())
        self.assertEqual(load_settings().dart_api_key, DART_KEY)
        with self.assertRaises(ConfigError) as caught:
            load_settings(self.root / "missing.env")
        self.assert_no_secrets(str(caught.exception))

    def test_bom_env_file(self):
        path = self.write_env(self.root / "bom.env", f"DART_API_KEY={DART_KEY}\n", bom=True)
        self.assertEqual(load_settings(path).dart_api_key, DART_KEY)


class RuntimeTests(IsolatedCase):
    def test_dart_only_tools_and_no_http(self):
        runtime = self.runtime()
        names = [spec.name for spec in runtime.service.list_tools()]
        self.assertEqual(set(names), DART_TOOLS)
        self.assertEqual(len(names), 13)
        self.assertTrue(TOSS_TOOLS.isdisjoint(names))
        self.assertTrue(REMOVED_TOOLS.isdisjoint(names))
        self.assertTrue(REMOVED_TOOLS.isdisjoint(TOOL_REGISTRY))
        self.assertIsNone(runtime.toss)
        self.assertFalse(runtime.toss_enabled)
        self.assertIsNotNone(create_mcp_server(runtime.service))
        self.assertEqual(self.requests, [])

    def test_toss_tools_and_no_http(self):
        runtime = self.runtime(toss=True)
        names = [spec.name for spec in runtime.service.list_tools()]
        self.assertEqual(set(names), DART_TOOLS | TOSS_TOOLS)
        self.assertEqual(len(names), 27)
        self.assertIsNotNone(runtime.toss)
        self.assertTrue(runtime.toss_enabled)
        self.assertTrue(REMOVED_TOOLS.isdisjoint(names))
        self.assertEqual(self.requests, [])

    def test_unconfigured_and_unknown_tool_calls(self):
        runtime = self.runtime()
        for name, arguments in (("get_price", {"symbols": ["005930"]}), ("get_price", {}), ("technical_snapshot", None)):
            with self.subTest(name=name, arguments=arguments):
                result = asyncio.run(runtime.service.call(name, arguments))
                self.assertEqual(result["data"], {"status": "unavailable", "error": "handler-not-configured"})
                self.assertEqual(result["source"], "toss" if name == "get_price" else "computed")
                self.assert_no_secrets(repr(result))
        # 등록되지 않은 도구는 UnknownToolError, 등록됐지만 핸들러가 없는 도구는 handler-not-configured envelope이다.
        for name in REMOVED_TOOLS | {"no_such_tool"}:
            with self.subTest(name=name), self.assertRaises(UnknownToolError) as caught:
                asyncio.run(runtime.service.call(name, {}))
            self.assertEqual(str(caught.exception), f"Unknown tool: {name}")
            self.assert_no_secrets(str(caught.exception))
        self.assertEqual(self.requests, [])

    def test_source_labels_and_annotations(self):
        for name, source in {"get_price_with_change": "toss", "diff_filing_sections": "dart", "resolve_symbol": "dart", "technical_snapshot": "computed"}.items():
            self.assertEqual(TOOL_REGISTRY[name].source, source)
            # MCP 2.0의 Python 속성명과 무관하게 공개 wire 필드명을 검증한다.
            annotations = TOOL_REGISTRY[name].as_mcp_tool().annotations.model_dump(by_alias=True)
            self.assertEqual(annotations["openWorldHint"], source != "computed")
        self.assertFalse(TOOL_REGISTRY["growth_metrics"].as_mcp_tool().annotations.model_dump(by_alias=True)["openWorldHint"])
        for spec in TOOL_REGISTRY.values():
            self.assertTrue(spec.as_mcp_tool().annotations.model_dump(by_alias=True)["readOnlyHint"])

    def test_mcp_public_memory_session_lists_dart_tools(self):
        runtime = self.runtime()

        async def list_names():
            server = create_mcp_server(runtime.service)
            with anyio.fail_after(10):
                async with create_client_server_memory_streams() as (client_streams, server_streams):
                    async with anyio.create_task_group() as group:
                        group.start_soon(server.run, *server_streams, server.create_initialization_options())
                        async with ClientSession(*client_streams) as session:
                            await session.initialize()
                            result = await session.list_tools()
                        group.cancel_scope.cancel()
            return [tool.name for tool in result.tools]

        names = asyncio.run(list_names())
        self.assertEqual(set(names), DART_TOOLS)
        self.assertEqual(len(names), 13)
        self.assertEqual(self.requests, [])

    def test_resolve_symbol_from_fresh_synthetic_index(self):
        def handler(request):
            self.assertEqual(request.url.path, "/api/company.json")
            return httpx.Response(200, json={"status": "000", "message": "ok", "corp_code": request.url.params["corp_code"], "corp_cls": "Y"})

        runtime = create_live_runtime(self.settings(), http_client=self.mock_client(handler), state_dir=self.root / "state")
        self.addCleanup(runtime.close)
        runtime.corp_index.replace(
            [CorpCodeRecord("00000001", "Synthetic Corp", "000001", "20260101")],
            refreshed_at=datetime.now(timezone.utc).isoformat(),
        )
        result = asyncio.run(runtime.service.call("resolve_symbol", {"query": "Synthetic Corp"}))
        self.assertTrue(any(item["name"] == "Synthetic Corp" and item["corp_code"] == "00000001" for item in result["data"]))
        self.assertEqual(len(self.requests), len(result["data"]))
        self.assertEqual([request.url.path for request in self.requests], ["/api/company.json"])

    def test_growth_metrics_via_service(self):
        runtime = self.runtime()
        result = asyncio.run(runtime.service.call("growth_metrics", {"series": ["10", "15"], "periods": ["2024", "2025"]}))
        self.assertEqual(result["source"], "computed")
        self.assertNotIn("error", result["data"])
        self.assertEqual(result["data"]["yoy"][0]["status"], "ok")
        self.assertEqual(result["data"]["yoy"][0]["value"], "0.5")
        self.assertEqual(self.requests, [])

    def test_default_and_explicit_state_paths(self):
        self.assertFalse((ROOT / "cache").exists())
        for default_state in (True, False):
            with self.subTest(default_state=default_state):
                runtime = self.runtime(default_state=default_state)
                expected = paths.corp_code_db_path() if default_state else self.root / "state/corp_codes.sqlite3"
                self.assertEqual(runtime.corp_index.path.resolve(), expected.resolve())
                self.assertTrue(expected.is_file())
                self.assertFalse(expected.resolve().is_relative_to(ROOT))
                self.assertFalse(expected.resolve().is_relative_to(Path.cwd().resolve()))
        self.assertFalse((ROOT / "cache").exists())
        self.assertEqual(self.requests, [])


class SessionLockTests(IsolatedCase):
    def test_dart_only_nested_sessions_need_no_lock(self):
        client = self.mock_client()
        calls = []
        lock_path = self.root / "state/toss-session.lock"

        def nested(service):
            calls.append("nested")
            self.assertFalse(lock_path.exists())

        def serve(service):
            calls.append("outer")
            self.assertFalse(lock_path.exists())
            self.assertEqual(self.stdio(serve=nested, client=client), 0)

        self.assertEqual(self.stdio(serve=serve, client=client), 0)
        self.assertEqual(calls, ["outer", "nested"])
        self.assertFalse(lock_path.exists())
        self.assertFalse(client.is_closed)
        self.assertEqual(self.requests, [])

    def test_toss_nested_session_blocked_then_lock_released(self):
        client = self.mock_client()
        output = io.StringIO()
        served = []

        def serve(service):
            served.append("outer")
            self.assertTrue((self.root / "state/toss-session.lock").exists())
            self.assertEqual(self.stdio(serve=lambda service: served.append("blocked"), toss=True, client=client, output=output), 1)

        self.assertEqual(self.stdio(serve=serve, toss=True, client=client), 0)
        self.assertIn("FAIL", output.getvalue())
        self.assert_no_secrets(output.getvalue())
        self.assertEqual(self.stdio(serve=lambda service: served.append("again"), toss=True, client=client), 0)
        self.assertEqual(served, ["outer", "again"])
        self.assertFalse(client.is_closed)
        self.assertEqual(self.requests, [])

    def test_missing_dart_does_not_serve(self):
        client = self.mock_client()
        output = io.StringIO()
        served = []
        self.assertEqual(self.stdio(serve=served.append, missing_dart=True, client=client, output=output), 1)
        self.assertEqual(served, [])
        self.assertIn("FAIL", output.getvalue())
        self.assert_no_secrets(output.getvalue())
        self.assertFalse(client.is_closed)
        self.assertEqual(self.requests, [])


class CliTests(IsolatedCase):
    def test_commands_and_env_file_positions(self):
        parser = build_parser()
        for command in ("mcp", "doctor"):
            self.assertEqual(parser.parse_args([command]).command, command)
            self.assertEqual(parser.parse_args(["--env-file", "a", command]).env_file, Path("a"))
            self.assertEqual(parser.parse_args([command, "--env-file", "a"]).env_file, Path("a"))
        for command in ("orders", "trade", "account-list", "not-a-command"):
            with self.subTest(command=command), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                parser.parse_args([command])
            self.assertEqual(caught.exception.code, 2)

    def doctor_call(self, *, environ=None, env_file=None, handler=None):
        output = io.StringIO()
        client = self.mock_client(handler)
        code = doctor(
            env_file=env_file, environ={} if environ is None else environ,
            user_env_file=self.root / "absent.env", output=output, http_client=client,
        )
        self.assertFalse(client.is_closed)
        self.assert_no_secrets(output.getvalue())
        return code, output.getvalue()

    @staticmethod
    def company_response(request):
        # 요청 식별자만 반영하고 회사 정보는 최소 합성값으로 만든다.
        return httpx.Response(200, json={"status": "000", "message": "ok", "corp_code": request.url.params["corp_code"], "corp_name": "Synthetic Corp"})

    def test_doctor_dart_only_success(self):
        env_file = self.write_env(self.root / "doctor.env", f"DART_API_KEY={DART_KEY}\n")
        code, output = self.doctor_call(env_file=env_file, handler=self.company_response)
        self.assertEqual(code, 0)
        self.assertIn("DART_API_KEY", output)
        self.assertIn("PASS", output)
        self.assertIn("SKIP", output)
        self.assertNotIn("Synthetic Corp", output)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.path, "/api/company.json")
        self.assertEqual([r for r in self.requests if "toss" in r.url.host], [])

    def test_doctor_missing_dart_no_http(self):
        code, output = self.doctor_call()
        self.assertEqual(code, 1)
        self.assertIn("FAIL", output)
        self.assertEqual(self.requests, [])

    def test_doctor_partial_toss_no_http(self):
        for present, missing, value in (("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", TOSS_ID), ("TOSS_CLIENT_SECRET", "TOSS_CLIENT_ID", TOSS_SECRET)):
            with self.subTest(present=present):
                code, output = self.doctor_call(environ={"DART_API_KEY": DART_KEY, present: value})
                self.assertEqual(code, 1)
                self.assertIn(missing, output)
                self.assertEqual(self.requests, [])

    def test_doctor_toss_auth_failure_keeps_dart_pass(self):
        def handler(request):
            if request.url.path == "/api/company.json":
                return self.company_response(request)
            self.assertEqual(request.url.path, "/oauth2/token")
            return httpx.Response(401, json={"error": "synthetic-auth-failure"})

        code, output = self.doctor_call(environ={"DART_API_KEY": DART_KEY, "TOSS_CLIENT_ID": TOSS_ID, "TOSS_CLIENT_SECRET": TOSS_SECRET}, handler=handler)
        self.assertEqual(code, 1)
        self.assertIn("PASS    : DART", output)
        self.assertIn("doctor: FAIL", output)
        self.assertEqual([r.url.path for r in self.requests], ["/api/company.json", "/oauth2/token"])

    def test_doctor_toss_success(self):
        def handler(request):
            if request.url.path == "/api/company.json":
                return self.company_response(request)
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json={"access_token": "test-token-0001", "token_type": "Bearer", "expires_in": 3600})
            self.assertEqual(request.url.path, "/api/v1/prices")
            # 요청된 심볼만 반영하며 가격은 손으로 만든 최소 합성값이다.
            return httpx.Response(200, json={"result": [{"symbol": request.url.params["symbols"], "lastPrice": "1", "currency": "KRW"}]})

        code, output = self.doctor_call(environ={"DART_API_KEY": DART_KEY, "TOSS_CLIENT_ID": TOSS_ID, "TOSS_CLIENT_SECRET": TOSS_SECRET}, handler=handler)
        self.assertEqual(code, 0)
        self.assertIn("doctor: PASS", output)
        self.assertNotIn("test-token-0001", output)
        self.assertEqual([r.url.path for r in self.requests], ["/api/company.json", "/oauth2/token", "/api/v1/prices"])


def run_session(service, action):
    """in-memory MCP 세션을 열고 action(session, init_result)의 결과를 돌려준다."""

    async def main():
        server = create_mcp_server(service)
        with anyio.fail_after(10):
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                async with anyio.create_task_group() as group:
                    group.start_soon(server.run, *server_streams, server.create_initialization_options())
                    async with ClientSession(*client_streams) as session:
                        initialized = await session.initialize()
                        result = await action(session, initialized)
                    group.cancel_scope.cancel()
        return result

    return asyncio.run(main())


class McpSessionTests(IsolatedCase):
    def test_unknown_tool_is_clean_client_error(self):
        runtime = self.runtime()

        async def action(session, _initialized):
            outcomes = {}
            for name in ("reverse_dcf", "get_financials", "no_such_tool"):
                try:
                    await session.call_tool(name, {})
                except MCPError as exc:
                    outcomes[name] = (exc.error.code, exc.error.message)
            # 오류 뒤에도 세션은 계속 쓸 수 있어야 한다.
            outcomes["listed"] = len((await session.list_tools()).tools)
            return outcomes

        # 서버 쪽 traceback은 dispatcher 로거의 ERROR 기록으로 나타난다. 하나도 없어야 한다.
        with self.assertNoLogs("mcp", level="ERROR"):
            outcomes = run_session(runtime.service, action)
        for name in ("reverse_dcf", "get_financials", "no_such_tool"):
            self.assertEqual(outcomes[name], (types.INVALID_PARAMS, f"Unknown tool: {name}"))
        self.assertEqual(outcomes["listed"], 13)
        self.assertEqual(self.requests, [])

    def test_invalid_arguments_are_tool_errors(self):
        runtime = self.runtime()
        bad_calls = (
            ("get_financial_statements", {"corp_code": "99000001", "bsns_year": "2025", "reprt_code": "11015"}, "reprt_code"),
            ("get_financial_statements", {"corp_code": "99000001", "bsns_year": "2025", "fs_div": "SFS"}, "fs_div"),
            ("get_financial_statements", {"bsns_year": "2025"}, "corp_code"),
        )

        async def action(session, _initialized):
            return [await session.call_tool(name, arguments) for name, arguments, _field in bad_calls]

        with self.assertNoLogs("mcp", level="ERROR"):
            results = run_session(runtime.service, action)
        for (name, _arguments, field), result in zip(bad_calls, results):
            with self.subTest(field=field):
                self.assertTrue(result.is_error)
                text = result.content[0].text
                self.assertIn(f"Invalid arguments for tool {name}", text)
                self.assertIn(field, text)
                self.assertNotIn("Traceback", text)
        self.assertEqual(self.requests, [])

    def test_instructions_reach_the_client(self):
        async def action(_session, initialized):
            return initialized

        initialized = run_session(self.runtime().service, action)
        self.assertEqual(initialized.instructions, SERVER_INSTRUCTIONS)


class InstructionTests(unittest.TestCase):
    """서버 안내문의 핵심 규칙을 정확한 부분 문자열로 고정한다."""

    PINS = (
        "읽기 전용으로 관찰하는",
        "투자 자문이 아니며",
        "합계·평균·비율·증감률·성장률을 직접 계산하지 않는다",
        "기준 시각(as_of 또는 fetched_at)",
        "매수·매도·보유를 권하지 않는다",
        "목표가나 진입가를 제시하지 않는다",
        "eligible: false인 후보는 절대 고르지 않는다",
        "후보마다 reason을 보여 주고 거기서 멈춘다",
        "사용자에게 고르게 한다",
        "dart-corp-cls인 후보는 거래정지·정리매매·관리종목·상장폐지 예정 여부를 알 수 없다",
        "list_filings로 그 접수번호를 먼저 조회해야 한다",
        "사업보고서, 반기·분기보고서, 증권신고서뿐이다",
        "연결·별도 구분(fs_div)",
        "오늘 KST 00:00의 1초 전(전날 23:59:59+09:00)",
        '"장중 잠정"',
        "market은 국가 코드 KR 또는 US",
        "잔량 합계나 매수·매도 불균형 수치를 만들지 않는다",
        '"이 내용은 정보 제공용이며 투자 권유가 아닙니다."',
    )

    def test_pinned_rules(self):
        for pin in self.PINS:
            with self.subTest(pin=pin):
                self.assertIn(pin, SERVER_INSTRUCTIONS)

    def test_concise_and_wired(self):
        self.assertLess(len(SERVER_INSTRUCTIONS), 2000)
        self.assertEqual(create_mcp_server().instructions, SERVER_INSTRUCTIONS)


class FakeLock:
    """doctor가 토스 점검 동안 잠금을 쥐는지 기록하는 가짜 잠금."""

    def __init__(self, error=None):
        self.error = error
        self.held = False
        self.events = []

    def __enter__(self):
        self.events.append("enter")
        if self.error is not None:
            raise self.error
        self.held = True
        return self

    def __exit__(self, *exc_info):
        self.events.append("exit")
        self.held = False


class DoctorLockTests(IsolatedCase):
    TOSS_ENV = {"DART_API_KEY": DART_KEY, "TOSS_CLIENT_ID": TOSS_ID, "TOSS_CLIENT_SECRET": TOSS_SECRET}

    def toss_handler(self, lock=None, lock_file=None):
        def handler(request):
            if request.url.path == "/api/company.json":
                if lock is not None:
                    self.assertFalse(lock.held, "DART 점검은 토스 잠금 밖에서 한다")
                return CliTests.company_response(request)
            if lock is not None:
                self.assertTrue(lock.held, "토스 요청은 잠금 안에서만 나간다")
            if lock_file is not None:
                self.assertTrue(lock_file.exists())
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json={"access_token": "test-token-0001", "token_type": "Bearer", "expires_in": 3600})
            self.assertEqual(request.url.path, "/api/v1/prices")
            return httpx.Response(200, json={"result": [{"symbol": request.url.params["symbols"], "lastPrice": "1", "currency": "KRW"}]})

        return handler

    def run_doctor(self, environ, handler=None, **kw):
        output = io.StringIO()
        client = self.mock_client(handler)
        code = doctor(environ=environ, user_env_file=self.root / "absent.env", output=output, http_client=client, **kw)
        self.assert_no_secrets(output.getvalue())
        return code, output.getvalue()

    def paths_requested(self):
        return [request.url.path for request in self.requests]

    def test_busy_lock_stops_before_any_token_request(self):
        lock = FakeLock(TossSessionBusy("synthetic busy"))
        code, output = self.run_doctor(self.TOSS_ENV, self.toss_handler(), session_lock=lock)
        self.assertEqual(code, 1)
        self.assertIn("토스 세션을 쓰고 있어 토스 점검을 하지 않았습니다", output)
        self.assertIn("doctor: FAIL", output)
        self.assertNotIn("PASS    : 토스", output)
        self.assertEqual(self.paths_requested(), ["/api/company.json"])
        self.assertEqual(lock.events, ["enter"])

    def test_other_lock_failures_also_skip_toss(self):
        for error in (OSError(13, "synthetic denied"), RuntimeError("synthetic reentry")):
            with self.subTest(error=type(error).__name__):
                self.requests.clear()
                code, output = self.run_doctor(self.TOSS_ENV, self.toss_handler(), session_lock=FakeLock(error))
                self.assertEqual(code, 1)
                self.assertIn("토스 세션 잠금을 얻지 못해", output)
                self.assertEqual(self.paths_requested(), ["/api/company.json"])

    def test_toss_checks_run_inside_the_lock(self):
        lock = FakeLock()
        code, output = self.run_doctor(self.TOSS_ENV, self.toss_handler(lock), session_lock=lock)
        self.assertEqual(code, 0, output)
        self.assertEqual(lock.events, ["enter", "exit"])
        self.assertEqual(self.paths_requested(), ["/api/company.json", "/oauth2/token", "/api/v1/prices"])

    def test_default_lock_path_is_the_server_lock_and_is_released(self):
        lock_file = paths.toss_session_lock_path()
        self.assertTrue(lock_file.is_relative_to(self.root))
        self.assertFalse(lock_file.exists())
        code, output = self.run_doctor(self.TOSS_ENV, self.toss_handler(lock_file=lock_file))
        self.assertEqual(code, 0, output)
        self.assertTrue(lock_file.exists())
        with TossSessionLock(lock_file, timeout=0):
            pass

    def test_lock_held_by_another_process_blocks_token(self):
        lock_file = self.root / "state" / "toss-session.lock"
        holder = subprocess.Popen(
            [sys.executable, "-c", (
                "import sys\n"
                "from autostock_research.toss.session_lock import TossSessionLock\n"
                "with TossSessionLock(sys.argv[1], timeout=0):\n"
                "    print('locked', flush=True)\n"
                "    sys.stdin.read()\n"
            ), str(lock_file)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        self.addCleanup(holder.wait, 10)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            code, output = self.run_doctor(self.TOSS_ENV, self.toss_handler(), state_dir=self.root / "state")
        finally:
            holder.stdin.close()
            holder.stdout.close()
        self.assertEqual(code, 1)
        self.assertIn("토스 세션을 쓰고 있어 토스 점검을 하지 않았습니다", output)
        self.assertEqual(self.paths_requested(), ["/api/company.json"])

    def test_dart_only_never_takes_the_lock(self):
        lock = FakeLock()
        code, output = self.run_doctor({"DART_API_KEY": DART_KEY}, CliTests.company_response, session_lock=lock)
        self.assertEqual(code, 0, output)
        self.assertEqual(lock.events, [])
        code, output = self.run_doctor({"DART_API_KEY": DART_KEY}, CliTests.company_response)
        self.assertEqual(code, 0, output)
        self.assertFalse(paths.toss_session_lock_path().exists())
        self.assertFalse(paths.user_state_dir().exists())


if __name__ == "__main__":
    unittest.main()
