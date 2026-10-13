"""사용자 설정과 상태 파일 경로를 계산하는 순수 함수."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


APP_DIR_NAME = "autostock-lite"


def user_config_dir(*, environ: Mapping[str, str] | None = None, home: str | Path | None = None, windows: bool | None = None) -> Path:
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    windows = os.name == "nt" if windows is None else windows
    if windows:
        base = Path(environ["APPDATA"]) if environ.get("APPDATA") else home / "AppData" / "Roaming"
    else:
        base = home / ".config"
    return base / APP_DIR_NAME


def default_env_file(*, environ: Mapping[str, str] | None = None, home: str | Path | None = None, windows: bool | None = None) -> Path:
    return user_config_dir(environ=environ, home=home, windows=windows) / ".env"


def user_state_dir(*, environ: Mapping[str, str] | None = None, home: str | Path | None = None, windows: bool | None = None) -> Path:
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    windows = os.name == "nt" if windows is None else windows
    if windows:
        base = Path(environ["LOCALAPPDATA"]) if environ.get("LOCALAPPDATA") else home / "AppData" / "Local"
    else:
        base = home / ".local" / "state"
    return base / APP_DIR_NAME


def corp_code_db_path(state_dir: str | Path | None = None, **kw) -> Path:
    return (user_state_dir(**kw) if state_dir is None else Path(state_dir)) / "corp_codes.sqlite3"


def toss_session_lock_path(state_dir: str | Path | None = None, **kw) -> Path:
    return (user_state_dir(**kw) if state_dir is None else Path(state_dir)) / "toss-session.lock"
