"""화이트리스트를 통과한 토스 읽기 요청만 실행하는 실환경 계층."""

from __future__ import annotations

import time
from typing import Any, Mapping

import httpx

from .auth import TossAuthError, TossTokenProvider
from .client import (
    BASE_URL,
    PreparedRequest,
    TossReadonlyClient,
    retry_delay,
)
from ..status_codes import ErrorCode


class TossLiveError(RuntimeError):
    code = ErrorCode.TOSS_REQUEST_FAILED


class TossLiveIpNotAllowed(TossLiveError):
    code = ErrorCode.TOSS_IP_NOT_ALLOWED


class TossRateLimited(TossLiveError):
    code = ErrorCode.TOSS_RATE_LIMITED


class TossRequestRejected(TossLiveError):
    """401·403·408·429를 뺀 HTTP 4xx. 같은 요청은 재시도해도 같은 답을 받는다."""

    code = ErrorCode.TOSS_REQUEST_REJECTED


class TossLiveClient:
    def __init__(
        self,
        *,
        http_client: httpx.Client,
        token_provider: TossTokenProvider,
        readonly_client: TossReadonlyClient | None = None,
        max_retries: int = 2,
    ) -> None:
        self._http = http_client
        self._tokens = token_provider
        self._readonly = readonly_client or TossReadonlyClient()
        self._max_retries = max_retries

    @property
    def readonly(self) -> TossReadonlyClient:
        return self._readonly

    def get_json(self, request: PreparedRequest) -> Mapping[str, Any]:
        if request.method != "GET":
            raise TossLiveError("읽기 전용 클라이언트는 GET만 실행합니다")
        headers = {"Accept": "application/json"}
        reauthenticated = False
        for attempt in range(self._max_retries + 1):
            self._readonly.wait_for_slot(request)
            token = self._tokens.get_token()
            request_headers = {
                **headers,
                "Authorization": f"Bearer {token}",
            }
            try:
                response = self._http.get(
                    f"{BASE_URL}{request.path}",
                    params=request.params,
                    headers=request_headers,
                )
            except httpx.RequestError as exc:
                raise TossLiveError("토스 API에 연결하지 못했습니다") from exc
            self._readonly.apply_rate_limit_headers(request, response.headers)
            if response.status_code == 401 and not reauthenticated:
                self._tokens.invalidate()
                reauthenticated = True
                continue
            if response.status_code == 401:
                raise TossAuthError(
                    "재인증 후에도 HTTP 401이 지속되어 토스 인증에 실패했습니다"
                )
            if response.status_code == 403:
                raise TossLiveIpNotAllowed(
                    "현재 IP가 토스 Open API 허용 IP에 등록되지 않았습니다"
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self._max_retries:
                    time.sleep(
                        retry_delay(
                            attempt,
                            retry_after=response.headers.get("Retry-After"),
                        )
                    )
                    continue
            if response.status_code == 429:
                raise TossRateLimited("토스 API 요청 한도를 초과했습니다: HTTP 429")
            if 400 <= response.status_code < 500 and response.status_code != 408:
                # 응답 본문은 메시지에 싣지 않는다. 상태 코드만 남긴다.
                raise TossRequestRejected(
                    f"토스 API가 요청을 거부했습니다: HTTP {response.status_code}"
                )
            if response.status_code != 200:
                raise TossLiveError(f"토스 API 응답 실패: HTTP {response.status_code}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise TossLiveError("토스 API 응답이 JSON이 아닙니다") from exc
            if not isinstance(payload, Mapping):
                raise TossLiveError("토스 API 응답 최상위 값이 객체가 아닙니다")
            return payload
        raise TossLiveError("토스 API 재시도 한도를 초과했습니다")
