"""응답 출처 식별과 인증정보 마스킹.

마스킹은 응답 본문을 다시 조립하지 않는다. 공시 원문에 들어 있는 URL, 줄바꿈,
탭, 표 행은 바이트 단위로 그대로 남고, 다음 세 경우만 해당 부분 문자열을 바꾼다.

- 설정된 키 값과 정확히 같은 부분 문자열
- ``http(s)://`` URL 안에서 인증용 쿼리 파라미터(예: ``crtfc_key``)의 값
- ``Authorization: Bearer <토큰>`` 형태의 토큰
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
import re
from typing import Any, Mapping


MASK = "***REDACTED***"
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "crtfc_key",
        "client_secret",
        "toss_client_secret",
        "dart_api_key",
    }
)
_NORMALIZED_KEYS = frozenset(key.replace("-", "_") for key in SENSITIVE_KEYS)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_SECRET_PARAM = re.compile(
    r"(?i)([?&](?:"
    + "|".join(re.escape(key).replace("_", "[-_]") for key in sorted(SENSITIVE_KEYS))
    + r")=)([^&#\s]+)"
)
_AUTHORIZATION_TOKEN = re.compile(
    r"(?i)(\bauthorization[\"']?\s*[:=]\s*[\"']?bearer\s+)[A-Za-z0-9._~+/-]+=*"
)


def _is_sensitive_key(key: object) -> bool:
    return str(key).strip().lower().replace("-", "_") in _NORMALIZED_KEYS


def _mask_url(match: re.Match[str]) -> str:
    return _SECRET_PARAM.sub(lambda param: param.group(1) + MASK, match.group(0))


def redact_text(text: str, *, secret_values: tuple[str, ...] = ()) -> str:
    """민감한 부분 문자열만 바꾸고 나머지 바이트는 그대로 둔다."""

    result = _URL.sub(_mask_url, text) if "://" in text else text
    result = _AUTHORIZATION_TOKEN.sub(lambda match: match.group(1) + MASK, result)
    for secret in secret_values:
        if secret:
            result = result.replace(secret, MASK)
    return result


def redact(value: Any, *, secret_values: tuple[str, ...] = ()) -> Any:
    """로그·응답에 들어갈 수 있는 중첩 값을 재귀적으로 마스킹한다."""

    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {
            str(key): MASK if _is_sensitive_key(key) else redact(item, secret_values=secret_values)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, secret_values=secret_values) for item in value]
    if isinstance(value, str):
        return redact_text(value, secret_values=secret_values)
    return value


def safe_params_hash(params: Mapping[str, Any]) -> str:
    """인증값을 제외한 정규화 파라미터의 SHA-256을 반환한다."""

    safe = {
        str(key): value
        for key, value in params.items()
        if not _is_sensitive_key(key)
    }
    canonical = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def dart_public_url(rcept_no: str) -> str:
    if not rcept_no.isdigit():
        raise ValueError("rcept_no는 숫자 문자열이어야 한다")
    return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
