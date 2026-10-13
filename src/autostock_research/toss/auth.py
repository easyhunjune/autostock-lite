"""토스증권 OAuth2 Client Credentials 인증과 단일 토큰 수명주기."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Callable

import httpx

from .client import BASE_URL
from ..status_codes import ErrorCode


class TossAuthError(RuntimeError):
    """인증정보를 포함하지 않는 토스 인증 오류."""

    code = ErrorCode.TOSS_AUTH_FAILED


class TossIpNotAllowed(TossAuthError):
    code = ErrorCode.TOSS_IP_NOT_ALLOWED


@dataclass(frozen=True)
class AccessToken:
    value: str = field(repr=False)
    token_type: str
    expires_at: float

    def usable(self, now: float, *, refresh_margin: float = 60.0) -> bool:
        return bool(self.value) and now < self.expires_at - refresh_margin


class TossTokenProvider:
    """프로세스 안에서 client당 유효 토큰을 하나만 유지한다."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        http_client: httpx.Client,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("client_id와 client_secret이 필요하다")
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http_client
        self._clock = clock
        self._token: AccessToken | None = None
        self._lock = threading.Lock()

    def get_token(self) -> str:
        now = self._clock()
        if self._token is not None and self._token.usable(now):
            return self._token.value
        with self._lock:
            now = self._clock()
            if self._token is not None and self._token.usable(now):
                return self._token.value
            self._token = self._issue(now)
            return self._token.value

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def _issue(self, now: float) -> AccessToken:
        try:
            response = self._http.post(
                f"{BASE_URL}/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.RequestError as exc:
            raise TossAuthError("토스 인증 서버에 연결하지 못했습니다") from exc
        if response.status_code == 403:
            raise TossIpNotAllowed(
                "현재 IP가 토스 Open API 허용 IP에 등록되지 않았습니다"
            )
        if response.status_code != 200:
            code = "unknown"
            try:
                payload = response.json()
                if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                    code = payload["error"]
            except ValueError:
                pass
            raise TossAuthError(
                f"토스 토큰 발급 실패: HTTP {response.status_code}, code={code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TossAuthError("토스 토큰 응답이 JSON이 아닙니다") from exc
        if not isinstance(payload, dict):
            raise TossAuthError("토스 토큰 응답 형식이 올바르지 않습니다")
        value = payload.get("access_token")
        token_type = payload.get("token_type")
        expires_in = payload.get("expires_in")
        if (
            not isinstance(value, str)
            or not value
            or token_type != "Bearer"
            or not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or expires_in <= 0
        ):
            raise TossAuthError("토스 토큰 응답 계약이 OpenAPI와 다릅니다")
        return AccessToken(value=value, token_type=token_type, expires_at=now + expires_in)
