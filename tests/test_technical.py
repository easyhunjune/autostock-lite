"""technical_snapshot(공식 1.0)을 손으로 만든 합성 일봉으로 검증한다.

가격과 거래량은 계산 결과를 손으로 확인할 수 있게 고른 값이다.
"""

import asyncio
from dataclasses import asdict, replace
from datetime import date, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
import unittest

from autostock_lite.calc.technical import (
    TECHNICAL_FORMULA_VERSION, TECHNICAL_WINDOW, TechnicalInputError,
    TechnicalInsufficientHistory, technical_snapshot,
)
from autostock_lite.mcp_runtime import create_live_runtime
from autostock_lite.mcp_server import TOOL_REGISTRY, ToolArgumentsError
from autostock_lite.toss.models import Candle
from test_smoke import IsolatedCase


BASE = date(2098, 1, 1)
RESULT_KEYS = {
    "formula_version", "adjusted", "currency", "as_of", "window_trading_days",
    "sma20", "sma60", "sma120", "rsi14", "position_252d_pct", "volume_zscore_60",
}


def stamp(index):
    return f"{(BASE + timedelta(days=index)).isoformat()}T00:00:00+09:00"


def candle(index, close, *, high=None, low=None, volume="1000", currency="KRW"):
    close = str(close)
    return Candle(stamp(index), close, str(high or close), str(low or close), close, str(volume), currency)


def series(closes, volumes=None):
    volumes = volumes or ["1000"] * len(closes)
    return [candle(index, close, volume=volume) for index, (close, volume) in enumerate(zip(closes, volumes))]


def snapshot(candles):
    return technical_snapshot(candles, adjusted=True)


def exact(fraction):
    with localcontext() as context:
        context.prec = 80
        return Decimal(fraction.numerator) / Decimal(fraction.denominator)


def reference_wilder_rsi(closes, period=14):
    """독립 구현: 분수로 정확히 계산한 Wilder RSI."""

    changes = [Fraction(current) - Fraction(previous) for previous, current in zip(closes, closes[1:])]
    gains = [max(change, Fraction(0)) for change in changes]
    losses = [max(-change, Fraction(0)) for change in changes]
    average_gain = sum(gains[:period], Fraction(0)) / period
    average_loss = sum(losses[:period], Fraction(0)) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
    return 100 - Fraction(100) / (1 + average_gain / average_loss)


LINEAR = list(range(1, 253))
Z_VOLUMES = ["100"] * 52 + ["92", "106", "94", "103", "97", "103", "97", "108"]


class IndicatorMathTests(unittest.TestCase):
    def test_rising_series(self):
        candles = series(LINEAR)
        result = snapshot(candles)
        self.assertEqual(set(result), RESULT_KEYS)
        self.assertEqual(result, {
            "formula_version": "1.0", "adjusted": True, "currency": "KRW", "as_of": candles[-1].timestamp,
            "window_trading_days": 252, "sma20": "242.5", "sma60": "222.5", "sma120": "192.5",
            "rsi14": "100", "position_252d_pct": "100", "volume_zscore_60": None,
        })
        self.assertEqual((TECHNICAL_FORMULA_VERSION, TECHNICAL_WINDOW), ("1.0", 252))

    def test_falling_and_flat_series(self):
        falling = snapshot(series(list(reversed(LINEAR))))
        self.assertEqual(
            (falling["sma20"], falling["sma60"], falling["sma120"], falling["rsi14"], falling["position_252d_pct"]),
            ("10.5", "30.5", "60.5", "0", "0"),
        )
        flat = snapshot(series([100] * 252))
        self.assertEqual(
            (flat["sma20"], flat["sma60"], flat["sma120"], flat["rsi14"], flat["position_252d_pct"], flat["volume_zscore_60"]),
            ("100", "100", "100", "50", None, None),
        )

    def test_position_uses_the_252_day_high_and_low(self):
        candles = series([100] * 252)
        candles[10] = candle(10, 100, low=50)
        candles[20] = candle(20, 100, high=200)
        candles[-1] = candle(251, 125, low=100)
        self.assertEqual(snapshot(candles)["position_252d_pct"], "50")

    def test_wilder_rsi_closed_form(self):
        # 처음 14번의 변화에서 상승 합이 하락 합의 세 배이고 이후 변화가 없으면 RS=3, RSI=75다.
        steps = [3, -1] * 7
        closes = [100]
        for step in steps:
            closes.append(closes[-1] + step)
        closes += [closes[-1]] * (252 - len(closes))
        rsi = Decimal(snapshot(series(closes))["rsi14"])
        self.assertLess(abs(rsi - 75), Decimal("1e-40"))

    def test_wilder_rsi_matches_an_exact_reference(self):
        closes = [1000 + (index * 37) % 23 - (index * 11) % 7 for index in range(252)]
        rsi = Decimal(snapshot(series(closes))["rsi14"])
        expected = exact(reference_wilder_rsi(closes))
        self.assertLess(abs(rsi - expected), Decimal("1e-40"))
        self.assertTrue(Decimal(0) < rsi < Decimal(100))

    def test_volume_z_score_uses_the_last_60_sessions(self):
        # 마지막 60개 평균은 100, 표본분산은 236/59 = 4라서 마지막 값 108의 z는 정확히 4다.
        volumes = ["1", "1000000"] * 96 + Z_VOLUMES
        self.assertEqual(snapshot(series([100] * 252, volumes))["volume_zscore_60"], "4")
        lowest_last = Z_VOLUMES[:-1] + ["92"]
        lowest_last[52] = "108"
        self.assertEqual(snapshot(series([100] * 252, ["100"] * 192 + lowest_last))["volume_zscore_60"], "-4")
        flat_tail = ["5"] * 192 + ["100"] * 60
        self.assertIsNone(snapshot(series([100] * 252, flat_tail))["volume_zscore_60"])

    def test_only_the_trailing_window_participates(self):
        candles = series(LINEAR, ["100"] * 192 + Z_VOLUMES)
        older = [Candle(f"{(BASE - timedelta(days=1)).isoformat()}T00:00:00+09:00", "1", "999999", "1", "500000", "99999999", "KRW")]
        self.assertEqual(snapshot(older + candles), snapshot(candles))


class DecimalRuleTests(unittest.TestCase):
    def test_precision_is_fixed_and_text_is_plain(self):
        candles = series([100] * 252)
        candles[30] = candle(30, 100, high=130)
        candles[-1] = candle(251, 110, low=100)
        result = snapshot(candles)
        self.assertEqual(result["position_252d_pct"], "33." + "3" * 48)
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(snapshot(candles), result)
            self.assertEqual(snapshot(series(LINEAR))["sma20"], "242.5")
        for value in result.values():
            if isinstance(value, str) and value[:1].isdigit():
                self.assertNotIn("E", value)

    def test_trailing_zeros_are_stripped(self):
        decimals = series(["100.50"] * 252)
        result = snapshot(decimals)
        self.assertEqual((result["sma20"], result["sma120"]), ("100.5", "100.5"))
        whole = snapshot(series(["1E+3"] * 252))
        self.assertEqual(whole["sma60"], "1000")


class InputContractTests(unittest.TestCase):
    def assert_input_invalid(self, candles, adjusted=True):
        with self.assertRaises(TechnicalInputError) as caught:
            technical_snapshot(candles, adjusted=adjusted)
        self.assertNotIsInstance(caught.exception, TechnicalInsufficientHistory)
        self.assertEqual(caught.exception.code.value, "technical-input-invalid")

    def test_insufficient_history(self):
        for count in (0, 1, 251):
            with self.subTest(count=count), self.assertRaises(TechnicalInsufficientHistory) as caught:
                snapshot(series(LINEAR[:count]))
            self.assertEqual(caught.exception.code.value, "technical-insufficient-history")

    def test_contract_violations(self):
        base = series(LINEAR)

        def changed(index, **fields):
            candles = list(base)
            candles[index] = replace(candles[index], **fields)
            return candles

        swapped = list(base)
        swapped[100], swapped[101] = swapped[101], swapped[100]
        cases = {
            "out of order": swapped,
            "duplicate timestamp": changed(101, timestamp=base[100].timestamp),
            "same date twice": changed(101, timestamp=stamp(100).replace("T00:00", "T12:00")),
            "naive timestamp": changed(0, timestamp="2098-01-01T00:00:00"),
            "not ISO": changed(0, timestamp="01/01/2098"),
            "mixed currency": changed(5, currency="USD"),
            "lowercase currency": [replace(item, currency="krw") for item in base],
            "zero price": changed(7, openPrice="0"),
            "negative low": changed(7, lowPrice="-1"),
            "negative volume": changed(7, volume="-1"),
            "high below close": changed(7, highPrice="7"),
            "low above open": changed(7, lowPrice="9", highPrice="9", closePrice="9"),
            "not a number": changed(7, closePrice="NaN"),
            "infinite": changed(7, highPrice="Infinity"),
            "not a decimal string": changed(7, volume=1000),
            "not candle models": [asdict(item) for item in base],
        }
        for label, candles in cases.items():
            with self.subTest(label):
                self.assert_input_invalid(candles)
        for adjusted in (False, None, 1):
            with self.subTest(adjusted=adjusted):
                self.assert_input_invalid(base, adjusted=adjusted)


class TechnicalToolTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.runtime = create_live_runtime(self.settings(toss=True), http_client=self.mock_client(), state_dir=self.root / "state")
        self.addCleanup(self.runtime.close)

    def call(self, arguments):
        return asyncio.run(self.runtime.service.call("technical_snapshot", arguments))

    def test_happy_path_matches_the_direct_calculation(self):
        candles = series(LINEAR, ["100"] * 192 + Z_VOLUMES)
        result = self.call({"candles": [asdict(item) for item in candles]})
        self.assertEqual(result["source"], "computed")
        self.assertEqual(result["reference"], {"kind": "formula", "value": "technical-snapshot:1.0", "public_url": None})
        self.assertEqual(TOOL_REGISTRY["technical_snapshot"].reference_value, f"technical-snapshot:{TECHNICAL_FORMULA_VERSION}")
        self.assertEqual(result["data"], snapshot(candles))
        self.assertEqual(result["data"]["volume_zscore_60"], "4")
        self.assertEqual(self.requests, [])

    def test_schema_rejects_short_unadjusted_or_extra_input(self):
        rows = [asdict(item) for item in series(LINEAR)]
        for label, arguments in (
            ("251 candles", {"candles": rows[:251]}),
            ("adjusted false", {"candles": rows, "adjusted": False}),
            ("extra candle field", {"candles": rows[:-1] + [dict(rows[-1], amount="1")]}),
            ("missing candle field", {"candles": rows[:-1] + [{k: v for k, v in rows[-1].items() if k != "volume"}]}),
            ("numeric price", {"candles": rows[:-1] + [dict(rows[-1], closePrice=252)]}),
        ):
            with self.subTest(label), self.assertRaises(ToolArgumentsError):
                self.call(arguments)
        self.assertEqual(self.requests, [])

    def test_contract_failures_are_blocked(self):
        rows = [asdict(item) for item in series(LINEAR)]
        swapped = list(rows)
        swapped[0], swapped[1] = swapped[1], swapped[0]
        for label, candles in (
            ("not a number", rows[:-1] + [dict(rows[-1], closePrice="NaN")]),
            ("ohlc bounds", rows[:-1] + [dict(rows[-1], highPrice="1")]),
            ("out of order", swapped),
        ):
            with self.subTest(label):
                self.assertEqual(self.call({"candles": candles})["data"], {"status": "blocked", "error": "technical-input-invalid"})
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
