"""autostock-lite 명령행 진입점."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys
from typing import ContextManager, Mapping, Sequence, TextIO

import httpx

from . import paths
from .config import ConfigError, ResearchSettings, collect_config_values
from .dart.client import DartLiveError, DartReadonlyClient
from .mcp_runtime import run_live_stdio
from .paths import default_env_file
from .toss.auth import TossAuthError, TossTokenProvider
from .toss.client import Operation
from .toss.live import TossLiveClient, TossLiveError
from .toss.models import PriceResponse, StrictModelError
from .toss.session_lock import TossSessionBusy, TossSessionLock


DOCTOR_CORP_CODE = "00126380"
DOCTOR_TOSS_SYMBOL = "005930"


def doctor(
    *,
    env_file: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    user_env_file: str | Path | None = None,
    output: TextIO | None = None,
    http_client: httpx.Client | None = None,
    state_dir: str | Path | None = None,
    session_lock: ContextManager[object] | None = None,
    lock_path: str | Path | None = None,
) -> int:
    """설정과 DART·선택 토스 연결을 인증정보 출력 없이 점검한다.

    토스 점검은 MCP 서버와 같은 토스 세션 잠금 안에서만 한다. 토큰을 새로
    발급하면 실행 중인 서버의 토큰이 무효가 되므로, 잠금을 얻지 못하면 토큰을
    요청하지 않고 실패로 끝낸다. DART만 설정된 경우에는 잠금을 건드리지 않는다.
    """

    if session_lock is not None and lock_path is not None:
        raise ValueError("session_lock과 lock_path는 동시에 지정할 수 없습니다")
    output = sys.stdout if output is None else output
    try:
        user_path = default_env_file(environ=environ) if user_env_file is None else Path(user_env_file)
        try:
            user_path.stat()
        except FileNotFoundError:
            user_exists = False
        except OSError:
            raise ConfigError(f"설정 파일을 읽을 수 없습니다: {user_path}") from None
        else:
            user_exists = True
        print(f"사용자 설정 파일: {user_path} ({'있음' if user_exists else '없음'})", file=output)
        print(f"명시한 설정 파일: {env_file if env_file is not None else '없음'}", file=output)
        values = collect_config_values(env_file, environ=environ, user_env_file=user_path)
        for key in ("DART_API_KEY", "TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET"):
            status = "설정됨" if key in values else "없음" if key == "DART_API_KEY" else "없음 (선택)"
            print(f"{key}: {status}", file=output)
        settings = ResearchSettings.from_mapping(values)
    except ConfigError as exc:
        print(f"FAIL    : {exc}", file=output)
        print("doctor: FAIL", file=output)
        return 1
    except Exception as exc:
        print(f"FAIL    : {type(exc).__name__}", file=output)
        print("doctor: FAIL", file=output)
        return 1

    def _failure(stage: str, exc: Exception) -> None:
        # 일반 예외에는 요청 URL과 키가 들어갈 수 있어 클래스명만 표시한다.
        if isinstance(exc, (DartLiveError, TossAuthError, TossLiveError, StrictModelError)):
            detail = str(exc)
            for secret in settings.secret_values():
                detail = detail.replace(secret, "***REDACTED***")
        else:
            detail = type(exc).__name__
        print(f"FAIL    : {stage}: {detail}", file=output)

    failed = False
    owned_client = http_client is None
    client = http_client
    try:
        if client is None:
            client = httpx.Client(timeout=20.0, follow_redirects=True)
        try:
            company = DartReadonlyClient(settings.dart_api_key or "", http_client=client).get_company(DOCTOR_CORP_CODE)
            if not isinstance(company.get("corp_name"), str) or not company["corp_name"].strip():
                raise DartLiveError("DART 회사 응답에 corp_name이 없습니다")
        except Exception as exc:
            failed = True
            _failure("DART 인증 및 회사개황 점검", exc)
        else:
            print("PASS    : DART 인증 및 회사개황 응답", file=output)

        # DART 실패와 무관하게 토스 연결은 독립적으로 점검한다.
        if settings.toss_enabled:
            lock = session_lock if session_lock is not None else TossSessionLock(
                paths.toss_session_lock_path(state_dir) if lock_path is None else lock_path,
                timeout=0,
            )
            try:
                lock.__enter__()
            except TossSessionBusy:
                failed = True
                print(
                    "FAIL    : 다른 프로세스(실행 중인 MCP 서버 등)가 토스 세션을 쓰고 있어 토스 점검을 하지 않았습니다. "
                    "토큰을 새로 발급하면 그 프로세스의 토큰이 무효가 됩니다. 서버를 끈 뒤 다시 실행하세요",
                    file=output,
                )
            except (RuntimeError, OSError) as exc:
                failed = True
                detail = getattr(exc, "strerror", None) or type(exc).__name__
                print(f"FAIL    : 토스 세션 잠금을 얻지 못해 토스 점검을 하지 않았습니다 ({detail})", file=output)
            else:
                try:
                    if not _check_toss(settings, client, output, _failure):
                        failed = True
                finally:
                    try:
                        lock.__exit__(None, None, None)
                    except OSError as exc:
                        failed = True
                        detail = exc.strerror or type(exc).__name__
                        print(f"FAIL    : 토스 세션 잠금을 해제하지 못했습니다 ({detail})", file=output)
        else:
            print("SKIP    : 토스 키가 없어 시세 점검을 건너뜁니다 (선택 기능)", file=output)
    except Exception as exc:
        failed = True
        _failure("연결 점검 준비", exc)
    finally:
        if owned_client and client is not None:
            try:
                client.close()
            except Exception as exc:
                failed = True
                _failure("연결 종료", exc)

    print("doctor: FAIL" if failed else "doctor: PASS", file=output)
    return 1 if failed else 0


def _check_toss(settings: ResearchSettings, client: httpx.Client, output: TextIO, failure) -> bool:
    """토스 인증과 현재가 응답을 점검한다. 호출자가 토스 세션 잠금을 쥔 상태여야 한다."""

    try:
        tokens = TossTokenProvider(
            settings.toss_client_id or "",
            settings.toss_client_secret or "",
            http_client=client,
        )
        tokens.get_token()
    except Exception as exc:
        failure("토스 인증 점검", exc)
        return False
    print("PASS    : 토스 OAuth2 인증 및 허용 IP", file=output)
    try:
        toss = TossLiveClient(http_client=client, token_provider=tokens)
        request = toss.readonly.prepare_get(
            Operation.GET_PRICES,
            "/api/v1/prices",
            params={"symbols": DOCTOR_TOSS_SYMBOL},
        )
        payload = toss.get_json(request)
        result = payload.get("result")
        if not isinstance(result, list) or not result:
            raise TossLiveError("토스 현재가 응답 result가 비어 있습니다")
        price = PriceResponse.from_dict(result[0])
        if price.symbol != DOCTOR_TOSS_SYMBOL:
            raise TossLiveError("토스 현재가 응답 심볼이 요청과 다릅니다")
    except Exception as exc:
        failure("토스 현재가 점검", exc)
        return False
    print("PASS    : 토스 현재가 응답이 내부 모델과 일치", file=output)
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autostock-lite")
    parser.add_argument("--env-file", type=Path, default=None, help="설정 파일 경로를 지정합니다")
    subparsers = parser.add_subparsers(dest="command", required=True)
    mcp = subparsers.add_parser("mcp", help="읽기 전용 MCP 서버를 표준 입출력으로 실행합니다")
    diagnostic = subparsers.add_parser("doctor", help="설정과 DART·선택 토스 연결을 점검합니다")
    for subparser in (mcp, diagnostic):
        subparser.add_argument("--env-file", type=Path, default=argparse.SUPPRESS, help="설정 파일 경로를 지정합니다")
    return parser


def quiet_http_loggers() -> None:
    """httpx와 httpcore는 INFO에서 요청 URL을 기록한다. DART 키가 쿼리에 실리므로 막아 둔다."""

    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: Sequence[str] | None = None) -> int:
    quiet_http_loggers()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    if args.command == "mcp":
        return run_live_stdio(env_file=args.env_file)
    if args.command == "doctor":
        return doctor(env_file=args.env_file)
    raise AssertionError("처리되지 않은 서브명령입니다")


if __name__ == "__main__":
    raise SystemExit(main())
