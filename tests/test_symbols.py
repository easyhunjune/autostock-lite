"""합성 회사와 주입 시계로 검증하는 상장 상태 판정."""

import asyncio
from datetime import datetime, timedelta, timezone
import inspect

import httpx

from autostock_lite.dart.client import DartLiveError, DartNoData, DartReadonlyClient
from autostock_lite.dart.corp_code import CorpCodeIndex, CorpCodeRecord
from autostock_lite.mcp_runtime import create_live_runtime
from autostock_lite.symbols import (
    DartListingClassifier, ListingStatusUnavailable, SymbolResolver,
)
from autostock_lite.toss import live
from autostock_lite.toss.client import (
    DEFAULT_TPS, OPERATION_CONTRACTS, Operation, RateLimitGroup, TossMarketData,
)
from autostock_lite.toss.models import StockInfo
from test_smoke import IsolatedCase


class SymbolTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.records = [
            CorpCodeRecord(f"{number:08d}", f"Synthetic {number}", f"{number:06d}", "20260101")
            for number in range(1, 8)
        ]
        self.index = CorpCodeIndex(self.root / "symbols.sqlite3")
        self.index.replace(self.records, refreshed_at=self.now.isoformat())
        self.lookups = []

    def classifier(self, lookup, **kw):
        def record(corp_code):
            self.lookups.append(corp_code)
            return lookup(corp_code)

        return DartListingClassifier(record, clock=lambda: self.now, **kw)

    def test_dart_classes_and_unknown_fields(self):
        for value, market, eligible, reason in (
            ("Y", "KOSPI", True, None), ("K", "KOSDAQ", True, None),
            ("N", "KONEX", False, "market-not-supported"), ("E", None, False, "not-listed"),
        ):
            with self.subTest(value=value):
                resolver = SymbolResolver(self.index, listing_classifier=self.classifier(lambda code: {"corp_cls": value}))
                candidate, = resolver.resolve("000001")
                self.assertEqual((candidate.market, candidate.eligible, candidate.reason), (market, eligible, reason))
                self.assertEqual((candidate.name, candidate.symbol, candidate.corp_code), ("Synthetic 1", "000001", "00000001"))
                self.assertEqual(candidate.confidence, 100.0)
                self.assertEqual(candidate.listing_source, "dart-corp-cls")
                for field in ("security_type", "is_common_share", "status", "trading_suspended", "liquidation_trading"):
                    self.assertIsNone(getattr(candidate, field))

    def test_failures_are_candidate_local_and_not_cached(self):
        bad_results = [DartLiveError("test-dart-key-0001"), DartNoData("synthetic no data"), RuntimeError("synthetic error"),
                       {"corp_cls": "X"}, {"corp_cls": ""}, {"corp_cls": None}, {"corp_cls": 1}, {}, [], None, {"corp_cls": " Y"}]
        for bad in bad_results:
            with self.subTest(bad=type(bad).__name__):
                self.lookups.clear()

                def lookup(code):
                    if code == "00000001":
                        if isinstance(bad, Exception):
                            raise bad
                        return bad
                    return {"corp_cls": "Y"}

                classifier = self.classifier(lookup)
                resolver = SymbolResolver(self.index, listing_classifier=classifier)
                for _ in range(2):
                    candidates = resolver.resolve("Synthetic", limit=2)
                    by_code = {item.corp_code: item for item in candidates}
                    self.assertEqual(by_code["00000001"].reason, "listing-status-unavailable")
                    self.assertFalse(by_code["00000001"].eligible)
                    self.assertIsNone(by_code["00000001"].market)
                    self.assertTrue(by_code["00000002"].eligible)
                self.assertEqual(self.lookups.count("00000001"), 2)
                self.assertEqual(self.lookups.count("00000002"), 1)
                with self.assertRaises(ListingStatusUnavailable) as caught:
                    classifier.corp_cls("00000001")
                self.assert_no_secrets(str(caught.exception))

    def test_success_cache_expires_at_ttl(self):
        classifier = self.classifier(lambda code: {"corp_cls": "Y"})
        resolver = SymbolResolver(self.index, listing_classifier=classifier)
        resolver.resolve("Synthetic", limit=2)
        self.now += timedelta(hours=23)
        resolver.resolve("Synthetic", limit=2)
        self.assertEqual(len(self.lookups), 2)
        self.now += timedelta(hours=1)
        resolver.resolve("Synthetic", limit=2)
        self.assertEqual(len(self.lookups), 4)

    def test_custom_ttl_and_failure_recovery(self):
        responses = iter([{}, {"corp_cls": "E"}, {"corp_cls": "K"}])
        classifier = self.classifier(lambda code: next(responses), ttl=timedelta(minutes=5))
        resolver = SymbolResolver(self.index, listing_classifier=classifier)
        self.assertEqual(resolver.resolve("000001")[0].reason, "listing-status-unavailable")
        self.assertEqual(resolver.resolve("000001")[0].reason, "not-listed")
        self.assertEqual(resolver.resolve("000001")[0].reason, "not-listed")
        self.assertEqual(len(self.lookups), 2)
        self.now += timedelta(minutes=5)
        self.assertEqual(resolver.resolve("000001")[0].market, "KOSDAQ")
        self.assertEqual(len(self.lookups), 3)

    def test_only_ranked_candidates_are_looked_up(self):
        resolver = SymbolResolver(self.index, listing_classifier=self.classifier(lambda code: {"corp_cls": "Y"}))
        candidates = resolver.resolve("Synthetic")
        self.assertEqual(len(candidates), 5)
        self.assertEqual(self.lookups, [record.corp_code for record in self.records[:5]])
        self.assertEqual([item.symbol for item in candidates], [record.stock_code for record in self.records[:5]])
        self.assertEqual(len(resolver.resolve("Synthetic", limit=2)), 2)
        for limit in (0, 6):
            with self.assertRaises(ValueError):
                resolver.resolve("Synthetic", limit=limit)
        with self.assertRaises(ValueError):
            resolver.resolve("")
        empty_index = CorpCodeIndex(self.root / "empty.sqlite3")
        empty_resolver = SymbolResolver(empty_index, listing_classifier=self.classifier(lambda code: {"corp_cls": "Y"}))
        self.assertEqual(empty_resolver.resolve("Synthetic"), ())
        self.assertEqual(len(self.lookups), 5)

    def test_ranking_eligibility_tiebreak_and_choice(self):
        records = [
            CorpCodeRecord("00000001", "Synthetic", "000001", "20260101"),
            CorpCodeRecord("00000002", "Synthetic B", "000002", "20260101"),
            CorpCodeRecord("00000003", "Synthetic C", "000003", "20260101"),
            CorpCodeRecord("00000004", "Synthetic D", "000004", "20260101"),
        ]
        self.index.replace(records, refreshed_at=self.now.isoformat())
        resolver = SymbolResolver(self.index, listing_classifier=self.classifier(lambda code: {"corp_cls": "E" if code in {"00000001", "00000002"} else "Y"}))
        candidates = resolver.resolve("Synthetic")
        self.assertEqual([item.symbol for item in candidates], ["000001", "000003", "000004", "000002"])
        self.assertEqual([item.confidence for item in candidates], [100.0, 90.0, 90.0, 90.0])
        self.assertTrue(resolver.needs_user_choice(candidates))
        self.assertFalse(resolver.needs_user_choice(candidates[:2]))
        self.assertFalse(resolver.needs_user_choice([candidates[0]]))
        self.assertFalse(resolver.needs_user_choice([]))

    def test_constructor_requires_exactly_one_provider(self):
        classifier = self.classifier(lambda code: {"corp_cls": "Y"})
        with self.assertRaises(ValueError):
            SymbolResolver(self.index)
        with self.assertRaises(ValueError):
            SymbolResolver(self.index, lambda symbols: (), listing_classifier=classifier)
        self.assertEqual(self.lookups, [])

    def test_company_stock_code_must_match_index(self):
        # 색인 레코드 000001의 회사개황이 다른 종목코드를 공시하면 상장 상태를 판정하지 않는다.
        cases = (
            ({"corp_cls": "Y", "stock_code": "000002"}, False, "listing-status-unavailable"),
            ({"corp_cls": "Y", "stock_code": 1}, False, "listing-status-unavailable"),
            ({"corp_cls": "Y", "stock_code": "000001"}, True, None),
            ({"corp_cls": "Y", "stock_code": " "}, True, None),
            ({"corp_cls": "Y"}, True, None),
            ({"corp_cls": "E", "stock_code": ""}, False, "not-listed"),
        )
        for company, eligible, reason in cases:
            with self.subTest(company=company):
                resolver = SymbolResolver(self.index, listing_classifier=self.classifier(lambda code: company))
                candidate, = resolver.resolve("000001")
                self.assertEqual((candidate.eligible, candidate.reason), (eligible, reason))
                self.assertEqual(candidate.listing_source, "dart-corp-cls")
                if reason == "listing-status-unavailable":
                    self.assertIsNone(candidate.market)

    def test_toss_reasons_and_no_dart_lookup(self):
        def payload(**changes):
            return {
                "symbol": "000001", "name": "Synthetic Stock", "englishName": "Synthetic Stock",
                "isinCode": "SYNTHETIC001", "market": "KOSPI", "securityType": "STOCK",
                "isCommonShare": True, "status": "ACTIVE", "currency": "KRW", "sharesOutstanding": "100",
                "koreanMarketDetail": {"liquidationTrading": False, "krxTradingSuspended": False, "nxtSupported": False, "nxtTradingSuspended": None},
                **changes,
            }

        cases = [({}, None), ({"status": "DELISTED"}, "delisted"), ({"status": "SCHEDULED"}, "listing-scheduled"),
                 ({"market": "SYNTHETIC"}, "market-not-supported"), ({"securityType": "ETF"}, "asset-type-not-supported"),
                 ({"koreanMarketDetail": None}, "liquidation-trading")]
        for changes, reason in cases:
            with self.subTest(reason=reason):
                calls = []

                def stock_lookup(symbols):
                    calls.append(symbols)
                    return [StockInfo.from_dict(payload(**changes))]

                candidate, = SymbolResolver(self.index, stock_lookup).resolve("000001")
                self.assertEqual(candidate.listing_source, "toss")
                self.assertEqual(candidate.reason, reason)
                self.assertEqual(candidate.eligible, reason is None)
                self.assertEqual(calls, [["000001"]])
        candidate, = SymbolResolver(self.index, lambda symbols: ()).resolve("000001")
        self.assertEqual(candidate.reason, "stock-not-found")
        self.assertEqual(candidate.listing_source, "toss")
        self.assertEqual(self.lookups, [])

    def test_runtime_dart_lookup_cached(self):
        def handler(request):
            self.assertEqual(request.url.path, "/api/company.json")
            self.assertEqual(request.url.params["corp_code"], "00000001")
            return httpx.Response(200, json={"status": "000", "message": "ok", "corp_code": request.url.params["corp_code"], "corp_cls": "Y"})

        runtime = create_live_runtime(self.settings(), http_client=self.mock_client(handler), state_dir=self.root / "state")
        self.addCleanup(runtime.close)
        runtime.corp_index.replace(self.records[:1], refreshed_at=datetime.now(timezone.utc).isoformat())
        for _ in range(2):
            envelope = asyncio.run(runtime.service.call("resolve_symbol", {"query": "000001"}))
            candidate, = envelope["data"]
            self.assertEqual(candidate["listing_source"], "dart-corp-cls")
            self.assertEqual(candidate["market"], "KOSPI")
            self.assertTrue(candidate["eligible"])
            self.assert_no_secrets(repr(envelope))
        self.assertEqual(len(self.requests), 1)

    def test_runtime_toss_does_not_call_dart_classifier(self):
        def handler(request):
            self.assertEqual(request.url.path, "/api/v1/stocks")
            return httpx.Response(200, json={"result": [{
                "symbol": "000001", "name": "Synthetic Stock", "englishName": "Synthetic Stock",
                "isinCode": "SYNTHETIC001", "market": "KOSPI", "securityType": "STOCK",
                "isCommonShare": True, "status": "ACTIVE", "currency": "KRW", "sharesOutstanding": "100",
                "koreanMarketDetail": {"liquidationTrading": False, "krxTradingSuspended": False, "nxtSupported": False, "nxtTradingSuspended": None},
            }]})

        # 유효한 토큰을 미리 공급하는 대신 시장 객체의 공개 reader 주입점을 사용한다.
        from autostock_lite.mcp_runtime import McpRuntime

        class Clock:
            def now(self):
                return datetime.now(timezone.utc)

        class Dart:
            def get_company(self, corp_code):
                raise AssertionError("Toss 경로에서 DART 회사개황을 호출하면 안 됩니다")

        client = self.mock_client(handler)

        def reader(request):
            return client.get("https://synthetic.invalid" + request.path, params=request.params).json()

        runtime = McpRuntime(http_client=client, dart=Dart(), toss=TossMarketData(reader),
                             corp_index=self.index, corp_refresh=None, clock=Clock(), close_http_client=False)
        self.addCleanup(runtime.close)
        candidate, = asyncio.run(runtime.service.call("resolve_symbol", {"query": "000001"}))["data"]
        self.assertEqual(candidate["listing_source"], "toss")
        self.assertTrue(candidate["eligible"])
        self.assertEqual([request.url.path for request in self.requests], ["/api/v1/stocks"])


class RemovedSurfaceTests(IsolatedCase):
    def test_account_and_unused_dart_surface_removed(self):
        for name in ("GET_ACCOUNTS", "GET_HOLDINGS"):
            self.assertFalse(hasattr(Operation, name))
        for name in ("get_accounts", "get_holdings"):
            self.assertFalse(hasattr(TossMarketData, name))
        self.assertFalse(hasattr(live, "ACCOUNT_HEADER_OPERATIONS"))
        self.assertFalse(hasattr(live, "TossLiveAccountRequired"))
        self.assertNotIn("account_seq", inspect.signature(live.TossLiveClient).parameters)
        for name in ("resolve_corp_codes", "list_market_disclosures"):
            self.assertFalse(hasattr(DartReadonlyClient, name))
        self.assertEqual(set(OPERATION_CONTRACTS), set(Operation))
        groups = {contract.rate_limit_group for contract in OPERATION_CONTRACTS.values()}
        self.assertEqual(groups, set(RateLimitGroup))
        self.assertEqual(groups, set(DEFAULT_TPS))
        self.assertNotIn("ACCOUNT", RateLimitGroup.__members__)
        self.assertNotIn("ASSET", RateLimitGroup.__members__)
        self.assertTrue(all(contract.method == "GET" for contract in OPERATION_CONTRACTS.values()))
