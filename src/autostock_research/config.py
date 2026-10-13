"""리서치 패키지 환경 설정 파싱.

키별 우선순위는 환경변수 > 명시한 env 파일 > 사용자 설정 파일이다.
낮은 우선순위부터 병합하며 빈 문자열이나 공백뿐인 값은 덮어쓰지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Mapping

from .paths import default_env_file


KNOWN_KEYS = frozenset({"DART_API_KEY", "TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET"})


class ConfigError(ValueError):
    pass


def parse_env_file(path: str | Path) -> dict[str, str]:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise ConfigError(f"설정 파일을 읽을 수 없습니다: {source}") from None
    result: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{source}:{line_number}: KEY=VALUE 형식이 아니다")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key not in KNOWN_KEYS:
            raise ConfigError(f"{source}:{line_number}: 알 수 없는 설정 키")
        if key in result:
            raise ConfigError(f"{source}:{line_number}: 설정 키 {key!r} 중복")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        result[key] = value
    return result


def collect_config_values(env_file: str | Path | None = None, *, environ: Mapping[str, str] | None = None, user_env_file: str | Path | None = None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    user_path = default_env_file(environ=environ) if user_env_file is None else Path(user_env_file)
    try:
        user_path.stat()
    except FileNotFoundError:
        user_values = {}
    except OSError:
        raise ConfigError(f"설정 파일을 읽을 수 없습니다: {user_path}") from None
    else:
        user_values = parse_env_file(user_path)
    explicit_values = {} if env_file is None else parse_env_file(env_file)
    result: dict[str, str] = {}
    for values in (user_values, explicit_values, environ):
        for key in KNOWN_KEYS:
            value = values.get(key, "").strip()
            if value:
                result[key] = value
    return result


@dataclass(frozen=True)
class ResearchSettings:
    dart_api_key: str | None = field(default=None, repr=False)
    toss_client_id: str | None = field(default=None, repr=False)
    toss_client_secret: str | None = field(default=None, repr=False)

    @property
    def toss_enabled(self) -> bool:
        return bool(self.toss_client_id and self.toss_client_secret)

    def secret_values(self) -> tuple[str, ...]:
        return tuple(value for value in (self.dart_api_key, self.toss_client_id, self.toss_client_secret) if value)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str], *, require_dart: bool = True) -> "ResearchSettings":
        if set(values) - KNOWN_KEYS:
            raise ConfigError("알 수 없는 설정 키가 있습니다")
        normalized = {key: value.strip() or None for key, value in values.items()}
        if require_dart and not normalized.get("DART_API_KEY"):
            raise ConfigError("DART_API_KEY 필수 설정이 누락되었습니다")
        toss_id = normalized.get("TOSS_CLIENT_ID")
        toss_secret = normalized.get("TOSS_CLIENT_SECRET")
        if bool(toss_id) != bool(toss_secret):
            missing = "TOSS_CLIENT_SECRET" if toss_id else "TOSS_CLIENT_ID"
            raise ConfigError(f"토스 설정 누락: {missing}")
        return cls(dart_api_key=normalized.get("DART_API_KEY"), toss_client_id=toss_id, toss_client_secret=toss_secret)


def load_settings(env_file: str | Path | None = None, *, environ: Mapping[str, str] | None = None, user_env_file: str | Path | None = None, require_dart: bool = True) -> ResearchSettings:
    return ResearchSettings.from_mapping(
        collect_config_values(env_file, environ=environ, user_env_file=user_env_file),
        require_dart=require_dart,
    )
