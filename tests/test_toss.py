"""토스 인증·읽기 요청·속도 제한·세션 잠금·도구 노출·비밀값 경계를 mock HTTP와 주입 시계로 검증한다.

키, 토큰, 종목 응답은 모두 지어낸 값이다. 실제 대기(sleep)는 하지 않는다.
"""

import asyncio
from datetime import datetime, timezone
import io
import json
import math
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from autostock_lite.cli import doctor
from autostock_lite.dart.client import DartLiveError, DartReadonlyClient
from autostock_lite.mcp_runtime import create_live_runtime, run_live_stdio
from autostock_lite.provenance import MASK
from autostock_lite.toss import live, session_lock
from autostock_lite.toss.auth import AccessToken, TossAuthError, TossIpNotAllowed, TossTokenProvider
from autostock_lite.toss.client import (
    BASE_URL, RETRY_AFTER_CAP_SECONDS, Operation, PreparedRequest, RateLimitGroup, RateLimiter,
    TossReadonlyClient, retry_delay,
)
from autostock_lite.toss.live import (
    TossLiveClient, TossLiveError, TossLiveIpNotAllowed, TossRateLimited, TossRequestRejected,
)
from autostock_lite.toss.session_lock import TossSessionBusy, TossSessionLock, TossSessionReentryError
from test_smoke import DART_KEY, SECRETS, TOSS_ID, TOSS_SECRET, IsolatedCase


ACCESS_TOKEN = "synthetic-access-0001"
DART_TOOL_COUNT, ALL_TOOL_COUNT = 13, 27
# 거부 응답 본문에만 들어 있는 비밀이 아닌 표지. 예외 메시지에 나오면 본문이 샌 것이다.
REJECTION_BODY_MARKER = "synthetic-rejection-body-0001"
# 설정값이 아니라 마스킹 대상이 아닌 가짜 비밀. 토큰 발급 오류 본문에만 넣는다.
UNCONFIGURED_SECRET = "synthetic-unconfigured-secret-0001"
REJECTED_STATUSES = (400, 404, 405, 409, 410, 412, 413, 415, 422)
# 응답 본문은 정상인데 X-RateLimit-* 헤더만 망가진 경우. Reset은 Remaining 0과 함께여야 적용 대상이 된다.
MALFORMED_RATE_LIMIT_HEADERS = (
    {"X-RateLimit-Remaining": "abc"},
    {"X-RateLimit-Remaining": "-1"},
    {"X-RateLimit-Remaining": "nan"},
    {"X-RateLimit-Remaining": "inf"},
    {"X-RateLimit-Limit": "0"},
    {"X-RateLimit-Limit": "abc"},
    {"X-RateLimit-Limit": "-4"},
    {"X-RateLimit-Limit": "inf"},
    {"X-RateLimit-Limit": "nan"},
    # 1 미만의 양수 Limit은 토큰이 1에 닿지 못하거나 대기가 사실상 무한해진다.
    {"X-RateLimit-Limit": "0.5"},
    {"X-RateLimit-Limit": "0.001"},
    {"X-RateLimit-Limit": "1e-6"},
    {"X-RateLimit-Limit": "5e-324"},
    {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "inf"},
    {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1e9"},
    {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "-5"},
    {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "nan"},
    {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "soon"},
)


def bucket_state(bucket):
    return (bucket.rate, bucket.capacity, bucket.tokens, bucket.updated_at, bucket.blocked_until)


class FakeClock:
    """RateLimiter용 단조 시계: sleep은 기록만 하고 시간을 앞당긴다."""

    def __init__(self, start=1000.0):
        self.value = start
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


def token_response(value, *, expires_in=3600, token_type="Bearer"):
    return httpx.Response(200, json={"access_token": value, "token_type": token_type, "expires_in": expires_in})


def refuse(request):
    raise httpx.ConnectError("synthetic connection failure", request=request)


class TokenProviderTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.now = 0.0

    def provider(self, handler):
        return TossTokenProvider(TOSS_ID, TOSS_SECRET, http_client=self.mock_client(handler), clock=lambda: self.now)

    def issuing(self, *values, expires_in=100):
        issued = iter(values)

        def handler(request):
            self.assertEqual((request.method, str(request.url)), ("POST", f"{BASE_URL}/oauth2/token"))
            return token_response(next(issued), expires_in=expires_in)

        return handler

    def test_token_is_reused_until_the_refresh_margin(self):
        provider = self.provider(self.issuing("tok-a", "tok-b"))
        self.assertEqual(provider.get_token(), "tok-a")
        self.now = 39.0  # 만료 100초, 갱신 여유 60초: 40초 전까지는 재사용한다.
        self.assertEqual(provider.get_token(), "tok-a")
        self.assertEqual(len(self.requests), 1)
        self.now = 40.0
        self.assertEqual(provider.get_token(), "tok-b")
        self.assertEqual(len(self.requests), 2)

    def test_credentials_travel_only_in_the_form_body(self):
        provider = self.provider(self.issuing("tok-a"))
        provider.get_token()
        request, = self.requests
        self.assertEqual(parse_qs(request.content.decode("ascii")), {
            "grant_type": ["client_credentials"], "client_id": [TOSS_ID], "client_secret": [TOSS_SECRET],
        })
        self.assertEqual(request.headers["accept"], "application/json")
        self.assertEqual(request.headers["content-type"], "application/x-www-form-urlencoded")
        self.assertNotIn("authorization", request.headers)
        for secret in SECRETS:
            self.assertNotIn(secret, str(request.url))

    def test_invalidate_forces_a_new_token(self):
        provider = self.provider(self.issuing("tok-a", "tok-b"))
        provider.get_token()
        provider.invalidate()
        self.assertEqual(provider.get_token(), "tok-b")
        self.assertEqual(len(self.requests), 2)

    def test_rejected_or_malformed_token_responses(self):
        cases = (
            ("ip not allowed", lambda r: httpx.Response(403, json={"error": "forbidden"}), TossIpNotAllowed),
            ("bad client", lambda r: httpx.Response(401, json={"error": "invalid_client"}), TossAuthError),
            ("server error text", lambda r: httpx.Response(500, text="synthetic"), TossAuthError),
            ("not json", lambda r: httpx.Response(200, text="synthetic"), TossAuthError),
            ("json list", lambda r: httpx.Response(200, json=[ACCESS_TOKEN]), TossAuthError),
            ("lowercase type", lambda r: token_response("tok-a", token_type="bearer"), TossAuthError),
            ("boolean lifetime", lambda r: token_response("tok-a", expires_in=True), TossAuthError),
            ("zero lifetime", lambda r: token_response("tok-a", expires_in=0), TossAuthError),
            ("negative lifetime", lambda r: token_response("tok-a", expires_in=-1), TossAuthError),
            ("string lifetime", lambda r: token_response("tok-a", expires_in="3600"), TossAuthError),
            ("float lifetime", lambda r: token_response("tok-a", expires_in=3600.0), TossAuthError),
            ("empty token", lambda r: token_response(""), TossAuthError),
            ("missing token", lambda r: httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3600}), TossAuthError),
            ("connection", refuse, TossAuthError),
        )
        for label, handler, expected in cases:
            with self.subTest(label), self.assertRaises(TossAuthError) as caught:
                self.provider(handler).get_token()
            self.assertIs(type(caught.exception), expected)
            self.assertEqual(caught.exception.code.value, "toss-ip-not-allowed" if expected is TossIpNotAllowed else "toss-auth-failed")
            self.assert_no_secrets(str(caught.exception))

    def test_token_error_text_is_kept_only_when_it_is_an_oauth_code(self):
        for status, body, shown in (
            (401, {"error": "invalid_client"}, "invalid_client"),
            (400, {"error": "unsupported_grant_type"}, "unsupported_grant_type"),
            (400, {"error": f"{UNCONFIGURED_SECRET}\nX-Injected: yes"}, "unknown"),
            (401, {"error": "invalid_client\n"}, "unknown"),
            (401, {"error": "Invalid_Client"}, "unknown"),
            (401, {"error": "invalid client"}, "unknown"),
            (401, {"error": "a" * 41}, "unknown"),
            (401, {"error": ""}, "unknown"),
            (401, {"error": ["invalid_client"]}, "unknown"),
            (500, ["invalid_client"], "unknown"),
        ):
            with self.subTest(status=status, body=body):
                with self.assertRaises(TossAuthError) as caught:
                    self.provider(lambda request, status=status, body=body: httpx.Response(status, json=body)).get_token()
                message = str(caught.exception)
                self.assertEqual(message, f"토스 토큰 발급 실패: HTTP {status}, code={shown}")
                self.assertNotIn(UNCONFIGURED_SECRET, message)
                self.assertNotIn("\n", message)

    def test_failed_issue_is_not_cached(self):
        answers = iter([httpx.Response(500, text="synthetic"), token_response("tok-a")])
        provider = self.provider(lambda request: next(answers))
        with self.assertRaises(TossAuthError):
            provider.get_token()
        self.assertEqual(provider.get_token(), "tok-a")

    def test_both_credentials_are_required_and_token_repr_is_hidden(self):
        client = self.mock_client()
        for client_id, client_secret in (("", TOSS_SECRET), (TOSS_ID, ""), ("", "")):
            with self.subTest(client_id=bool(client_id), client_secret=bool(client_secret)), self.assertRaises(ValueError):
                TossTokenProvider(client_id, client_secret, http_client=client)
        token = AccessToken(ACCESS_TOKEN, "Bearer", 100.0)
        self.assertNotIn(ACCESS_TOKEN, repr(token))
        self.assertTrue(token.usable(39.0))
        self.assertFalse(token.usable(40.0))
        self.assertFalse(AccessToken("", "Bearer", 100.0).usable(0.0))
        self.assertEqual(self.requests, [])


class LiveClientTests(IsolatedCase):
    def client(self, api_handler, *, tokens=("tok-a", "tok-b", "tok-c"), max_retries=2):
        issued = iter(tokens)

        def handler(request):
            if request.url.path == "/oauth2/token":
                return token_response(next(issued))
            return api_handler(request)

        self.clock = FakeClock()
        http = self.mock_client(handler)
        provider = TossTokenProvider(TOSS_ID, TOSS_SECRET, http_client=http, clock=lambda: 0.0)
        readonly = TossReadonlyClient(rate_limiter=RateLimiter(clock=self.clock))
        return TossLiveClient(http_client=http, token_provider=provider, readonly_client=readonly, max_retries=max_retries)

    @staticmethod
    def orderbook(client):
        return client.readonly.prepare_get(Operation.GET_ORDERBOOK, "/api/v1/orderbook", params={"symbol": "000001"})

    def api_requests(self):
        return [request for request in self.requests if request.url.path != "/oauth2/token"]

    def token_requests(self):
        return [request for request in self.requests if request.url.path == "/oauth2/token"]

    def answers(self, *responses):
        queue = list(responses)
        return lambda request: queue.pop(0)

    def get(self, client):
        with patch.object(live.time, "sleep") as sleep:
            try:
                return client.get_json(self.orderbook(client))
            finally:
                self.sleeps = [call.args[0] for call in sleep.call_args_list]

    def test_token_is_sent_only_to_the_api_and_reused(self):
        client = self.client(lambda request: httpx.Response(200, json={"result": {"symbol": "000001"}}))
        self.assertEqual(self.get(client), {"result": {"symbol": "000001"}})
        self.assertEqual(self.get(client), {"result": {"symbol": "000001"}})
        self.assertEqual(len(self.token_requests()), 1)
        for request in self.api_requests():
            self.assertEqual(str(request.url), f"{BASE_URL}/api/v1/orderbook?symbol=000001")
            self.assertEqual(request.headers["authorization"], "Bearer tok-a")
            self.assertEqual(request.headers["accept"], "application/json")
        for request in self.requests:
            for secret in SECRETS:
                self.assertNotIn(secret, str(request.url))
        self.assertEqual(self.sleeps, [])

    def test_401_reauthenticates_once(self):
        client = self.client(self.answers(httpx.Response(401), httpx.Response(200, json={"result": {}})))
        self.assertEqual(self.get(client), {"result": {}})
        self.assertEqual([r.headers["authorization"] for r in self.api_requests()], ["Bearer tok-a", "Bearer tok-b"])
        self.assertEqual(len(self.token_requests()), 2)

    def test_repeated_401_is_an_auth_failure(self):
        client = self.client(lambda request: httpx.Response(401))
        with self.assertRaises(TossAuthError) as caught:
            self.get(client)
        self.assertIs(type(caught.exception), TossAuthError)
        self.assertEqual((len(self.api_requests()), len(self.token_requests())), (2, 2))
        self.assert_no_secrets(str(caught.exception))

    def test_403_is_not_retried(self):
        client = self.client(lambda request: httpx.Response(403))
        with self.assertRaises(TossLiveIpNotAllowed):
            self.get(client)
        self.assertEqual((len(self.api_requests()), self.sleeps), (1, []))

    def test_429_honours_retry_after_then_gives_up(self):
        client = self.client(lambda request: httpx.Response(429, headers={"Retry-After": "3"}))
        with self.assertRaises(TossRateLimited) as caught:
            self.get(client)
        self.assertEqual(caught.exception.code.value, "toss-rate-limited")
        self.assertEqual(self.sleeps, [3.0, 3.0])
        self.assertEqual(len(self.api_requests()), 3)

    def test_429_recovers_and_backs_off_without_retry_after(self):
        client = self.client(self.answers(httpx.Response(429, headers={"Retry-After": "0.5"}), httpx.Response(200, json={"result": {}})))
        self.assertEqual(self.get(client), {"result": {}})
        self.assertEqual(self.sleeps, [0.5])
        client = self.client(lambda request: httpx.Response(429))
        with self.assertRaises(TossRateLimited):
            self.get(client)
        self.assertEqual(self.sleeps, [1.0, 2.0])
        client = self.client(lambda request: httpx.Response(429, headers={"Retry-After": "120"}), max_retries=1)
        with self.assertRaises(TossRateLimited):
            self.get(client)
        self.assertEqual(self.sleeps, [30.0])
        client = self.client(lambda request: httpx.Response(429, headers={"Retry-After": "3"}), max_retries=0)
        with self.assertRaises(TossRateLimited):
            self.get(client)
        self.assertEqual(self.sleeps, [])

    def test_server_errors_are_retried(self):
        client = self.client(self.answers(httpx.Response(502), httpx.Response(200, json={"result": {}})))
        self.assertEqual(self.get(client), {"result": {}})
        self.assertEqual(self.sleeps, [1.0])
        client = self.client(lambda request: httpx.Response(503))
        with self.assertRaises(TossLiveError) as caught:
            self.get(client)
        self.assertIs(type(caught.exception), TossLiveError)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_unusable_responses_and_requests(self):
        for label, handler in (
            ("not json", lambda request: httpx.Response(200, text="synthetic")),
            ("json list", lambda request: httpx.Response(200, json=[{"result": {}}])),
            ("connection", refuse),
        ):
            with self.subTest(label), self.assertRaises(TossLiveError) as caught:
                self.get(self.client(handler))
            self.assertIs(type(caught.exception), TossLiveError)
        self.requests.clear()
        client = self.client(lambda request: self.fail("no request expected"))
        post = PreparedRequest(Operation.GET_ORDERBOOK, "POST", "/api/v1/orderbook", {}, RateLimitGroup.MARKET_DATA)
        with self.assertRaises(TossLiveError):
            client.get_json(post)
        self.assertEqual(self.requests, [])

    def test_rate_limit_headers_hold_the_next_request(self):
        headers = {"X-RateLimit-Limit": "4", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "2"}
        client = self.client(lambda request: httpx.Response(200, json={"result": {}}, headers=headers))
        self.get(client)
        self.assertEqual(self.clock.sleeps, [])
        bucket = client.readonly.rate_limiter.buckets[RateLimitGroup.MARKET_DATA]
        self.assertEqual((bucket.rate, bucket.capacity, bucket.tokens, bucket.blocked_until), (4.0, 4.0, 0.0, 1002.0))
        self.get(client)
        self.assertEqual(self.clock.sleeps, [2.0])
        self.assertEqual(self.sleeps, [])
        # 다른 그룹은 영향을 받지 않는다.
        client.readonly.wait_for_slot(client.readonly.prepare_get(Operation.GET_RANKINGS, "/api/v1/rankings"))
        self.assertEqual(self.clock.sleeps, [2.0])

    def test_malformed_rate_limit_headers_do_not_break_a_good_response(self):
        payload = {"result": {"symbol": "000001"}}
        for headers in MALFORMED_RATE_LIMIT_HEADERS:
            with self.subTest(headers=headers):
                client = self.client(lambda request, headers=headers: httpx.Response(200, json=payload, headers=headers))
                self.assertEqual(self.get(client), payload)
                bucket = client.readonly.rate_limiter.buckets[RateLimitGroup.MARKET_DATA]
                self.assertTrue(all(math.isfinite(value) for value in bucket_state(bucket)), bucket_state(bucket))
                self.assertEqual((bucket.rate, bucket.capacity), (10.0, 10.0))
                self.assertLessEqual(bucket.blocked_until, self.clock.now() + RETRY_AFTER_CAP_SECONDS)
                # 다음 요청도 유한한 시간만 기다리고 본문을 받는다.
                self.assertEqual(self.get(client), payload)
                self.assertTrue(all(0 <= seconds <= RETRY_AFTER_CAP_SECONDS for seconds in self.clock.sleeps), self.clock.sleeps)
                self.assertEqual(self.sleeps, [])

    def test_other_4xx_is_rejected_without_retry(self):
        body = f"{REJECTION_BODY_MARKER} Bearer {ACCESS_TOKEN} {TOSS_SECRET}"
        for status in REJECTED_STATUSES:
            with self.subTest(status=status):
                self.requests.clear()
                client = self.client(lambda request, status=status: httpx.Response(status, text=body, headers={"Retry-After": "1"}))
                with self.assertRaises(TossRequestRejected) as caught:
                    self.get(client)
                self.assertIsInstance(caught.exception, TossLiveError)
                self.assertEqual(caught.exception.code.value, "toss-request-rejected")
                self.assertEqual(str(caught.exception), f"토스 API가 요청을 거부했습니다: HTTP {status}")
                self.assertEqual((len(self.api_requests()), len(self.token_requests()), self.sleeps), (1, 1, []))

    def test_408_stays_a_request_failure_without_retry(self):
        client = self.client(lambda request: httpx.Response(408, text=REJECTION_BODY_MARKER))
        with self.assertRaises(TossLiveError) as caught:
            self.get(client)
        self.assertIs(type(caught.exception), TossLiveError)
        self.assertEqual(caught.exception.code.value, "toss-request-failed")
        self.assertNotIn(REJECTION_BODY_MARKER, str(caught.exception))
        self.assertEqual((len(self.api_requests()), self.sleeps), (1, []))

    def test_failure_messages_never_carry_the_response_body(self):
        # envelope는 코드만 싣는다. 본문이 새는지는 여기서 예외 메시지로 확인한다(doctor가 str(exc)를 출력한다).
        body = f"{REJECTION_BODY_MARKER} Bearer {ACCESS_TOKEN} id={TOSS_ID} secret={TOSS_SECRET} {UNCONFIGURED_SECRET}"
        for status in (*REJECTED_STATUSES, 401, 403, 408, 429, 500, 503):
            with self.subTest(status=status):
                client = self.client(lambda request, status=status: httpx.Response(status, text=body))
                with self.assertRaises((TossLiveError, TossAuthError)) as caught:
                    self.get(client)
                message = str(caught.exception)
                for leaked in (REJECTION_BODY_MARKER, ACCESS_TOKEN, UNCONFIGURED_SECRET):
                    self.assertNotIn(leaked, message)
                self.assert_no_secrets(message)


class RateLimiterTests(unittest.TestCase):
    def test_default_bucket_spacing(self):
        clock = FakeClock()
        limiter = RateLimiter(clock=clock)
        limiter.acquire(RateLimitGroup.STOCK_TRADING_TREND)
        self.assertEqual(clock.sleeps, [])
        limiter.acquire(RateLimitGroup.STOCK_TRADING_TREND)
        self.assertEqual(clock.sleeps, [1.0])
        for _ in range(10):
            limiter.acquire(RateLimitGroup.MARKET_DATA)
        self.assertEqual(clock.sleeps, [1.0])
        limiter.acquire(RateLimitGroup.MARKET_DATA)
        self.assertEqual(len(clock.sleeps), 2)
        self.assertAlmostEqual(clock.sleeps[1], 0.1)

    def test_headers_are_case_insensitive_and_reset_needs_an_empty_bucket(self):
        clock = FakeClock()
        limiter = RateLimiter(clock=clock)
        limiter.update_from_headers(RateLimitGroup.STOCK, {"x-ratelimit-remaining": "1", "X-RATELIMIT-RESET": "30"})
        limiter.acquire(RateLimitGroup.STOCK)
        self.assertEqual(clock.sleeps, [])
        limiter.update_from_headers(RateLimitGroup.STOCK, {"X-RateLimit-Limit": "2", "x-ratelimit-remaining": "0", "X-Ratelimit-Reset": "3"})
        limiter.acquire(RateLimitGroup.STOCK)
        self.assertEqual(clock.sleeps, [3.0])
        bucket = limiter.buckets[RateLimitGroup.STOCK]
        self.assertEqual((bucket.rate, bucket.capacity, bucket.tokens, bucket.blocked_until), (2.0, 2.0, 1.0, 0.0))
        limiter.update_from_headers(RateLimitGroup.STOCK, {"Content-Type": "application/json"})
        self.assertEqual(bucket.tokens, 1.0)

    def test_unusable_headers_never_raise_and_leave_their_part_unchanged(self):
        for headers in MALFORMED_RATE_LIMIT_HEADERS:
            if "X-RateLimit-Reset" in headers:
                continue
            with self.subTest(headers=headers):
                clock = FakeClock()
                limiter = RateLimiter(clock=clock)
                bucket = limiter.buckets[RateLimitGroup.STOCK]
                before = bucket_state(bucket)
                clock.value += 1.0  # Remaining이 적용되면 updated_at이 움직인다.
                limiter.update_from_headers(RateLimitGroup.STOCK, headers)
                self.assertEqual(bucket_state(bucket), before)
        # Remaining 0은 적용되고, 쓸 수 없는 Reset만 무시된다. 1e9는 상한으로 잘린다.
        for reset, blocked_until in (
            ("inf", 0.0), ("-inf", 0.0), ("nan", 0.0), ("-5", 0.0), ("soon", 0.0), ("", 0.0),
            ("1e9", 1001.0 + RETRY_AFTER_CAP_SECONDS), ("30.5", 1001.0 + RETRY_AFTER_CAP_SECONDS), ("0", 1001.0),
        ):
            with self.subTest(reset=reset):
                clock = FakeClock()
                limiter = RateLimiter(clock=clock)
                clock.value += 1.0
                limiter.update_from_headers(RateLimitGroup.STOCK, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset})
                self.assertEqual(bucket_state(limiter.buckets[RateLimitGroup.STOCK]), (5.0, 5.0, 0.0, 1001.0, blocked_until))

    def test_reset_is_capped_by_the_retry_after_cap(self):
        self.assertEqual(RETRY_AFTER_CAP_SECONDS, 30.0)
        self.assertEqual(retry_delay(0, retry_after="1e9"), RETRY_AFTER_CAP_SECONDS)
        clock = FakeClock()
        limiter = RateLimiter(clock=clock)
        limiter.update_from_headers(RateLimitGroup.STOCK, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1e9"})
        limiter.acquire(RateLimitGroup.STOCK)
        self.assertEqual(clock.sleeps, [RETRY_AFTER_CAP_SECONDS])

    def test_acquire_never_waits_longer_than_the_cap(self):
        # 헤더 검증을 우회해 버킷을 직접 망가뜨려도 한 번의 대기는 상한을 넘지 않는다.
        for rate in (1e-6, 5e-324):
            with self.subTest(rate=rate):
                clock = FakeClock()
                limiter = RateLimiter(clock=clock)
                bucket = limiter.buckets[RateLimitGroup.STOCK]
                bucket.rate, bucket.tokens = rate, 0.0
                limiter.acquire(RateLimitGroup.STOCK)
                self.assertEqual(clock.sleeps, [RETRY_AFTER_CAP_SECONDS])
        clock = FakeClock()
        limiter = RateLimiter(clock=clock)
        limiter.buckets[RateLimitGroup.STOCK].blocked_until = clock.now() + 1e9
        limiter.acquire(RateLimitGroup.STOCK)
        self.assertEqual(clock.sleeps, [RETRY_AFTER_CAP_SECONDS])

    def test_limit_must_be_at_least_one(self):
        for limit, expected_rate in (("0.999", 5.0), ("1", 1.0), ("1.5", 1.5), ("1e9", 1e9)):
            with self.subTest(limit=limit):
                clock = FakeClock()
                limiter = RateLimiter(clock=clock)
                limiter.update_from_headers(RateLimitGroup.STOCK, {"X-RateLimit-Limit": limit})
                bucket = limiter.buckets[RateLimitGroup.STOCK]
                self.assertEqual((bucket.rate, bucket.capacity), (expected_rate, expected_rate))
                self.assertEqual(bucket.tokens, min(5.0, expected_rate))

    def test_valid_headers_still_apply_next_to_unusable_ones(self):
        clock = FakeClock()
        limiter = RateLimiter(clock=clock)
        bucket = limiter.buckets[RateLimitGroup.STOCK]
        limiter.update_from_headers(RateLimitGroup.STOCK, {"X-RateLimit-Limit": "abc", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "2"})
        self.assertEqual(bucket_state(bucket), (5.0, 5.0, 0.0, 1000.0, 1002.0))
        clock.value = 1010.0
        limiter.update_from_headers(RateLimitGroup.STOCK, {"X-RateLimit-Limit": "4", "X-RateLimit-Remaining": "nan", "X-RateLimit-Reset": "inf"})
        self.assertEqual(bucket_state(bucket), (4.0, 4.0, 0.0, 1000.0, 1002.0))
        limiter.acquire(RateLimitGroup.STOCK)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(bucket_state(bucket), (4.0, 4.0, 3.0, 1010.0, 0.0))

    def test_retry_delay(self):
        for attempt, retry_after, expected in (
            (0, "3", 3.0), (0, "0", 0.0), (0, "120", 30.0), (0, "-1", 1.0), (1, "soon", 2.0),
            (1, None, 2.0), (2, None, 4.0), (10, None, 30.0),
        ):
            with self.subTest(attempt=attempt, retry_after=retry_after):
                self.assertEqual(retry_delay(attempt, retry_after=retry_after), expected)
        self.assertEqual(retry_delay(0, retry_after=None, jitter=lambda: 0.5), 1.5)
        self.assertEqual(retry_delay(0, retry_after=None, jitter=lambda: -5.0), 1.0)
        with self.assertRaises(ValueError):
            retry_delay(-1, retry_after=None)


class SessionLockTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.lock_path = self.root / "state" / "toss-session.lock"

    def hold_from_another_handle(self):
        """다른 프로세스처럼 별도 파일 핸들로 OS 잠금을 쥔다 (Windows·POSIX 모두 핸들 단위 잠금)."""

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch()
        handle = open(self.lock_path, "r+b")
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

            def unlock():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def unlock():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        state = {"held": True}

        def release():
            if state["held"]:
                state["held"] = False
                unlock()
                handle.close()

        self.addCleanup(release)
        return release

    def test_second_holder_fails_fast_without_waiting(self):
        release_other = self.hold_from_another_handle()
        with patch.object(session_lock.time, "sleep", side_effect=AssertionError("no polling with timeout=0")):
            with self.assertRaises(TossSessionBusy):
                TossSessionLock(self.lock_path, timeout=0).acquire()
        release_other()
        with TossSessionLock(self.lock_path, timeout=0):
            pass

    def test_reentry_in_one_process_is_refused(self):
        lock = TossSessionLock(self.lock_path, timeout=0)
        with lock:
            for other in (lock, TossSessionLock(self.lock_path), TossSessionLock(self.root / "state" / "x" / ".." / "toss-session.lock")):
                with self.subTest(path=str(other.path)), self.assertRaises(TossSessionReentryError):
                    other.acquire()
        with lock:
            pass

    def test_lock_is_released_on_exit_and_on_error(self):
        lock = TossSessionLock(self.lock_path, timeout=0)
        with self.assertRaises(RuntimeError):
            with lock:
                raise RuntimeError("synthetic failure")
        lock.release()
        lock.release()
        with TossSessionLock(self.lock_path, timeout=0):
            pass
        metadata = json.loads(self.lock_path.read_text(encoding="utf-8"))
        self.assertEqual(set(metadata), {"pid", "started_at"})
        self.assertEqual(metadata["pid"], os.getpid())
        started = datetime.fromisoformat(metadata["started_at"])
        self.assertEqual(started.utcoffset(), timezone.utc.utcoffset(None))

    def test_timeout_must_be_finite_and_nonnegative(self):
        for timeout in (-1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                TossSessionLock(self.lock_path, timeout=timeout)
        self.assertFalse(self.lock_path.exists())

    def stdio(self, serve, output):
        environ = dict(self.environment, DART_API_KEY=DART_KEY, TOSS_CLIENT_ID=TOSS_ID, TOSS_CLIENT_SECRET=TOSS_SECRET)
        return run_live_stdio(environ=environ, state_dir=self.root / "state", serve=serve, http_client=self.mock_client(), output=output)

    def test_server_refuses_to_start_while_another_holder_has_the_lock(self):
        self.hold_from_another_handle()
        output = io.StringIO()
        served = []
        self.assertEqual(self.stdio(served.append, output), 1)
        self.assertEqual(served, [])
        self.assertIn("FAIL", output.getvalue())
        self.assert_no_secrets(output.getvalue())
        self.assertEqual(self.requests, [])

    def test_server_releases_the_lock_when_serving_fails(self):
        def serve(service):
            raise RuntimeError("synthetic serve failure")

        with self.assertRaises(RuntimeError):
            self.stdio(serve, io.StringIO())
        with TossSessionLock(self.lock_path, timeout=0):
            pass


class ToolExposureTests(IsolatedCase):
    """시장 데이터 도구는 토스 키 두 개가 모두 있을 때만 노출된다."""

    def serve_with(self, extra):
        environ = dict(self.environment, DART_API_KEY=DART_KEY, **extra)
        output = io.StringIO()
        served = []

        def serve(service):
            served.append(([spec.name for spec in service.list_tools()], self.lock_path().exists()))

        code = run_live_stdio(environ=environ, state_dir=self.root / "state", serve=serve,
                              http_client=self.mock_client(), output=output)
        self.assert_no_secrets(output.getvalue())
        return code, served, output.getvalue()

    def lock_path(self):
        return self.root / "state" / "toss-session.lock"

    def test_counts_by_configuration(self):
        code, served, _ = self.serve_with({})
        self.assertEqual(code, 0)
        (names, locked), = served
        self.assertEqual((len(names), locked), (DART_TOOL_COUNT, False))
        self.assertNotIn("get_price", names)
        self.assertFalse(self.lock_path().exists())

        code, served, _ = self.serve_with({"TOSS_CLIENT_ID": TOSS_ID, "TOSS_CLIENT_SECRET": TOSS_SECRET})
        self.assertEqual(code, 0)
        (names, locked), = served
        self.assertEqual((len(names), locked), (ALL_TOOL_COUNT, True))
        self.assertIn("get_price", names)
        self.assertIn("technical_snapshot", names)
        self.assertEqual(self.requests, [])

    def test_partial_toss_configuration_does_not_serve(self):
        for extra, missing in (
            ({"TOSS_CLIENT_ID": TOSS_ID}, "TOSS_CLIENT_SECRET"),
            ({"TOSS_CLIENT_SECRET": TOSS_SECRET}, "TOSS_CLIENT_ID"),
            ({"TOSS_CLIENT_ID": TOSS_ID, "TOSS_CLIENT_SECRET": "   "}, "TOSS_CLIENT_SECRET"),
        ):
            with self.subTest(missing=missing):
                code, served, output = self.serve_with(extra)
                self.assertEqual((code, served), (1, []))
                self.assertIn(missing, output)
                self.assertFalse(self.lock_path().exists())
        self.assertEqual(self.requests, [])


class SecretBoundaryTests(IsolatedCase):
    """토스 client id·secret과 접근 토큰은 응답 envelope에 나오지 않는다."""

    def runtime(self, api_handler, token_handler=None):
        def handler(request):
            if request.url.path == "/oauth2/token":
                return token_handler(request) if token_handler else token_response(ACCESS_TOKEN)
            return api_handler(request)

        runtime = create_live_runtime(self.settings(toss=True), http_client=self.mock_client(handler), state_dir=self.root / "state")
        self.addCleanup(runtime.close)
        return runtime

    def call(self, runtime, name, arguments):
        with patch.object(live.time, "sleep"):
            return asyncio.run(runtime.service.call(name, arguments))

    def test_echoed_credentials_are_masked_and_token_stays_in_headers(self):
        echo = {"result": {"symbol": "000001", "note": f"id={TOSS_ID} secret={TOSS_SECRET}", "levels": [{"memo": TOSS_SECRET}]}}
        runtime = self.runtime(lambda request: httpx.Response(200, json=echo))
        result = self.call(runtime, "get_orderbook", {"symbol": "000001"})
        self.assertEqual(result["data"], {"symbol": "000001", "note": f"id={MASK} secret={MASK}", "levels": [{"memo": MASK}]})
        self.assertEqual(result["source"], "toss")
        rendered = repr(result) + json.dumps(result, ensure_ascii=False)
        self.assert_no_secrets(rendered)
        self.assertNotIn(ACCESS_TOKEN, rendered)
        token_request, api_request = self.requests
        self.assertEqual(token_request.url.path, "/oauth2/token")
        self.assertIn(TOSS_SECRET, token_request.content.decode("ascii"))
        self.assertEqual(api_request.headers["authorization"], f"Bearer {ACCESS_TOKEN}")
        for request in self.requests:
            self.assert_no_secrets(str(request.url))
            self.assertNotIn(ACCESS_TOKEN, str(request.url))

    def test_failures_carry_only_codes(self):
        cases = (
            ("token error echoes the secret", lambda r: httpx.Response(401, json={"error": TOSS_SECRET}), None, ("unavailable", "toss-auth-failed")),
            ("api rate limited", None, lambda r: httpx.Response(429, headers={"Retry-After": "1"}), ("unavailable", "toss-rate-limited")),
            ("api echoes the token on 500", None, lambda r: httpx.Response(500, text=f"Bearer {ACCESS_TOKEN} {TOSS_SECRET}"), ("unavailable", "toss-request-failed")),
        )
        for label, token_handler, api_handler, (status, code) in cases:
            with self.subTest(label):
                runtime = self.runtime(api_handler or (lambda r: httpx.Response(200, json={"result": {}})), token_handler)
                result = self.call(runtime, "get_orderbook", {"symbol": "000001"})
                self.assertEqual(result["data"], {"status": status, "error": code})
                self.assert_no_secrets(repr(result))
                self.assertNotIn(ACCESS_TOKEN, repr(result))

    def test_malformed_rate_limit_headers_still_return_the_payload(self):
        payload = {"result": {"symbol": "000001", "levels": []}}
        for headers in MALFORMED_RATE_LIMIT_HEADERS:
            with self.subTest(headers=headers):
                runtime = self.runtime(lambda request, headers=headers: httpx.Response(200, json=payload, headers=headers))
                result = self.call(runtime, "get_orderbook", {"symbol": "000001"})
                self.assertEqual(result["data"], payload["result"])

    def test_http_statuses_map_to_envelope_codes(self):
        body = f"{REJECTION_BODY_MARKER} Bearer {ACCESS_TOKEN} id={TOSS_ID} secret={TOSS_SECRET}"
        rejected = ("blocked", "toss-request-rejected")
        cases = (
            (400, rejected, 1), (404, rejected, 1), (409, rejected, 1), (422, rejected, 1),
            (408, ("unavailable", "toss-request-failed"), 1),
            (401, ("unavailable", "toss-auth-failed"), 2),
            (403, ("unavailable", "toss-ip-not-allowed"), 1),
            (429, ("unavailable", "toss-rate-limited"), 3),
            (500, ("unavailable", "toss-request-failed"), 3),
            (503, ("unavailable", "toss-request-failed"), 3),
        )
        for status, (envelope_status, code), attempts in cases:
            with self.subTest(status=status):
                self.requests.clear()
                runtime = self.runtime(lambda request, status=status: httpx.Response(status, text=body))
                result = self.call(runtime, "get_orderbook", {"symbol": "000001"})
                self.assertEqual(result["data"], {"status": envelope_status, "error": code})
                self.assertEqual(len([request for request in self.requests if request.url.path != "/oauth2/token"]), attempts)
                # envelope 모양 점검: envelope는 상태와 코드만 싣고 예외 메시지를 담지 않는다.
                # 본문 유출 자체는 LiveClientTests.test_failure_messages_never_carry_the_response_body가 str(exc)로 본다.
                rendered = repr(result) + json.dumps(result, ensure_ascii=False)
                self.assertNotIn(REJECTION_BODY_MARKER, rendered)
                self.assert_no_secrets(rendered)
                self.assertNotIn(ACCESS_TOKEN, rendered)

    def doctor_output(self, token_status, token_body):
        def handler(request):
            if request.url.path == "/api/company.json":
                return httpx.Response(200, json={"status": "000", "corp_code": request.url.params["corp_code"], "corp_name": "가상회사"})
            return httpx.Response(token_status, json=token_body)

        output = io.StringIO()
        code = doctor(environ={"DART_API_KEY": DART_KEY, "TOSS_CLIENT_ID": TOSS_ID, "TOSS_CLIENT_SECRET": TOSS_SECRET},
                      user_env_file=self.root / "absent.env", output=output, http_client=self.mock_client(handler),
                      state_dir=self.root / "state")
        self.assertEqual(code, 1)
        self.assert_no_secrets(output.getvalue())
        return output.getvalue()

    def test_doctor_never_prints_the_token_endpoint_error_text(self):
        # 설정된 비밀이 섞인 error는 이제 마스킹 전에 통째로 버려진다(예전에는 MASK로 가려 출력했다).
        output = self.doctor_output(401, {"error": f"bad secret {TOSS_SECRET} for {TOSS_ID}"})
        self.assertIn("FAIL    : 토스 인증 점검: 토스 토큰 발급 실패: HTTP 401, code=unknown\n", output)
        self.assertNotIn(MASK, output)
        # 설정값이 아닌 가짜 비밀과 줄바꿈은 마스킹 대상이 아니므로 출력 전에 버려져야 한다.
        output = self.doctor_output(400, {"error": f"{UNCONFIGURED_SECRET}\nPASS    : 위조된 줄"})
        self.assertIn("FAIL    : 토스 인증 점검: 토스 토큰 발급 실패: HTTP 400, code=unknown\n", output)
        self.assertNotIn(UNCONFIGURED_SECRET, output)
        self.assertNotIn("위조된 줄", output)
        # 정상적인 OAuth2 오류 코드는 그대로 보인다.
        output = self.doctor_output(401, {"error": "invalid_client"})
        self.assertIn("FAIL    : 토스 인증 점검: 토스 토큰 발급 실패: HTTP 401, code=invalid_client\n", output)

    def test_doctor_masks_configured_secrets_in_failure_text(self):
        # auth.py가 서버 error 문자열을 버리므로, 설정된 비밀이 든 예외를 직접 만들어 doctor의 마스킹을 확인한다.
        output = io.StringIO()
        with patch.object(DartReadonlyClient, "get_company", side_effect=DartLiveError(f"x {DART_KEY}")), \
                patch.object(TossTokenProvider, "get_token", side_effect=TossAuthError(f"synthetic {TOSS_SECRET} {TOSS_ID}")):
            code = doctor(environ={"DART_API_KEY": DART_KEY, "TOSS_CLIENT_ID": TOSS_ID, "TOSS_CLIENT_SECRET": TOSS_SECRET},
                          user_env_file=self.root / "absent.env", output=output, http_client=self.mock_client(),
                          state_dir=self.root / "state")
        self.assertEqual(code, 1)
        text = output.getvalue()
        self.assertIn(f"FAIL    : DART 인증 및 회사개황 점검: x {MASK}\n", text)
        self.assertIn(f"FAIL    : 토스 인증 점검: synthetic {MASK} {MASK}\n", text)
        self.assert_no_secrets(text)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
