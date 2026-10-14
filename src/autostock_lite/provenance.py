"""응답 출처 식별과 인증정보 마스킹.

마스킹은 응답 본문을 다시 조립하지 않는다. 공시 원문에 들어 있는 URL, 줄바꿈,
탭, 표 행, 산문의 "bearer"는 바이트 단위로 그대로 남고, 다음 세 경우만 해당
부분 문자열을 바꾼다.

- 설정된 키 값과 정확히 같은 부분 문자열
- ``http(s)://`` URL 안에서 인증용 쿼리 파라미터(예: ``crtfc_key``)의 값.
  값은 키 모양 문자(``[A-Za-z0-9._~%+/=-]``)까지만 보고, 뒤에 붙은 한글·따옴표·
  문장 끝 마침표는 남긴다.
- ``Bearer <토큰>``의 토큰(``Authorization:`` 접두가 있든 없든). JWT 모양이거나
  글자와 숫자가 섞인 24자 이상(헤더 접두가 있으면 16자 이상) 불투명 토큰일 때만
  가리므로 "bearer bonds", "bearer 3.5% notes", "bearer U.S. persons" 같은 산문은
  바뀌지 않는다. 짧고 평범한 값(예: ``Bearer abc123``)은 산문과 구별할 수 없어
  남는다.

설정된 키 값은 패턴보다 먼저 통째로 바꾼다. 매핑의 민감한 키(예: ``crtfc_key``,
``Authorization``)는 값 전체를 가린다.
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
# 키의 '-'와 '_'는 서로 바꿔 써도 같은 키로 본다(_is_sensitive_key와 같은 규칙).
_SECRET_PARAM = re.compile(
    r"(?i)([?&;](?:"
    + "|".join(
        "[-_]".join(re.escape(part) for part in key.split("_"))
        for key in sorted(_NORMALIZED_KEYS)
    )
    + r")=)([A-Za-z0-9._~%+/=-]+)"
)
# "Bearer <t>", optionally after an "Authorization:" header prefix (group 1, also
# the JSON '"authorization": "Bearer <t>"' form).  _mask_bearer masks <t> only
# when it is token-shaped, so English prose after "bearer" is never rewritten.
_BEARER_TOKEN = re.compile(
    r"(?i)(\bauthorization[\"']?[ \t]*[:=][ \t]*[\"']?)?(\bbearer[ \t]+)([A-Za-z0-9._~+/=-]+)"
)
# Token shapes (see _is_token):
# - JWT-like: two dot-separated base64url segments of 8+ chars, an optional third,
#   and at least one digit (real JWT headers always carry one; "instruments.Further"
#   does not).
# - opaque: 24+ chars (16+ after an Authorization header) that mix letters and
#   digits and contain no stand-alone hyphenated run of alphabetic words such as
#   "non-interest-bearing".
_JWT = re.compile(r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*)?")
_WORD_RUN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]+(?:-[A-Za-z]+)+(?![A-Za-z0-9])")
_LETTER = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"[0-9]")
_MIN_BARE_TOKEN_LENGTH = 24
_MIN_HEADER_TOKEN_LENGTH = 16


def _is_sensitive_key(key: object) -> bool:
    return str(key).strip().lower().replace("-", "_") in _NORMALIZED_KEYS


def _split_sentence_dots(value: str) -> tuple[str, str]:
    """Keep a sentence-final '.' out of the masked span."""

    core = value.rstrip(".")
    return core, value[len(core):]


def _mask_param(match: re.Match[str]) -> str:
    core, tail = _split_sentence_dots(match.group(2))
    return match.group(1) + MASK + tail if core else match.group(0)


def _mask_url(match: re.Match[str]) -> str:
    return _SECRET_PARAM.sub(_mask_param, match.group(0))


def _is_token(value: str, *, header: bool) -> bool:
    has_digit = _DIGIT.search(value) is not None
    if _JWT.fullmatch(value) is not None and has_digit:
        return True
    minimum = _MIN_HEADER_TOKEN_LENGTH if header else _MIN_BARE_TOKEN_LENGTH
    return (
        len(value) >= minimum
        and has_digit
        and _LETTER.search(value) is not None
        and _WORD_RUN.search(value) is None
    )


def _mask_bearer(match: re.Match[str]) -> str:
    prefix, bearer = match.group(1) or "", match.group(2)
    core, tail = _split_sentence_dots(match.group(3))
    if not core or not _is_token(core, header=bool(prefix)):
        return match.group(0)
    return prefix + bearer + MASK + tail


def redact_text(text: str, *, secret_values: tuple[str, ...] = ()) -> str:
    """민감한 부분 문자열만 바꾸고 나머지 바이트는 그대로 둔다.

    설정된 키 값을 먼저 통째로 바꾼다. 패턴을 먼저 돌리면 값 문자 집합 밖의 문자
    (예: ``!``, ``*``)가 들어 있는 키는 앞부분만 가려지고 뒷부분이 새어 나간다.
    """

    result = text
    for secret in secret_values:
        if secret:
            result = result.replace(secret, MASK)
    result = _URL.sub(_mask_url, result) if "://" in result else result
    return _BEARER_TOKEN.sub(_mask_bearer, result)


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
