from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from _support import SRC  # noqa: F401
from daily_chip_news.cost import (
    PRICING,
    ModelPrice,
    PricingUnavailableError,
    UsageTotals,
    actual_call_cost_usd,
    input_cost_usd,
    lookup_price,
    output_cost_usd,
    projected_call_cost_usd,
    usage_totals_from_metadata,
)


TODAY = date(2026, 9, 21)


class PriceLookupTests(unittest.TestCase):
    def test_known_models_resolve_today(self) -> None:
        lite = lookup_price("gemini-3.5-flash-lite", now=TODAY)
        self.assertEqual(0.30, lite.input_usd_per_1m)
        self.assertEqual(2.50, lite.output_usd_per_1m)
        flash = lookup_price("gemini-3.6-flash", now=TODAY)
        self.assertEqual(0.75, flash.input_usd_per_1m)
        self.assertEqual(4.50, flash.output_usd_per_1m)

    def test_unknown_model_fails_closed(self) -> None:
        with self.assertRaises(PricingUnavailableError):
            lookup_price("gemini-9.9-ultra", now=TODAY)

    def test_blank_model_fails_closed(self) -> None:
        with self.assertRaises(PricingUnavailableError):
            lookup_price("   ", now=TODAY)

    def test_intro_rate_applies_through_the_last_promotional_day(self) -> None:
        record = lookup_price("gemini-3.7-flash", now=date(2026, 12, 31))
        self.assertEqual(4.50, record.output_usd_per_1m)

    def test_post_promotion_rate_replaces_the_intro_rate(self) -> None:
        record = lookup_price("gemini-3.7-flash", now=date(2027, 1, 1))
        self.assertEqual(1.50, record.input_usd_per_1m)
        self.assertEqual(7.50, record.output_usd_per_1m)

    def test_expired_pricing_fails_closed(self) -> None:
        expired = (
            ModelPrice(
                "model-x",
                date(2026, 1, 1),
                date(2026, 6, 30),
                1.0,
                1.0,
                "expired",
            ),
        )
        with self.assertRaises(PricingUnavailableError):
            lookup_price("model-x", now=date(2026, 7, 1), pricing=expired)

    def test_datetime_is_accepted_and_normalized(self) -> None:
        record = lookup_price(
            "gemini-3.5-flash-lite",
            now=datetime(2026, 9, 21, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual("gemini-3.5-flash-lite", record.model)

    def test_every_production_model_has_a_current_price(self) -> None:
        for model in (
            "gemini-3.5-flash-lite",
            "gemini-3.6-flash",
            "gemini-3.7-flash",
        ):
            self.assertIsInstance(lookup_price(model, now=TODAY), ModelPrice)
        self.assertGreaterEqual(len(PRICING), 5)


class CostMathTests(unittest.TestCase):
    def price(self) -> ModelPrice:
        return lookup_price("gemini-3.6-flash", now=TODAY)

    def test_input_cost(self) -> None:
        self.assertAlmostEqual(0.00075, input_cost_usd(1_000, self.price()), places=9)

    def test_thinking_tokens_are_billed_as_output(self) -> None:
        price = self.price()
        visible_only = output_cost_usd(1_000, 0, price)
        with_thinking = output_cost_usd(1_000, 500, price)
        self.assertAlmostEqual(0.0045, visible_only, places=9)
        self.assertAlmostEqual(0.00675, with_thinking, places=9)

    def test_projected_cost_uses_the_output_ceiling_and_input_margin(self) -> None:
        price = lookup_price("gemini-3.5-flash-lite", now=TODAY)
        projected = projected_call_cost_usd(
            prompt_tokens=4_000, max_output_tokens=3_072, price=price
        )
        expected = 4_000 * 1.05 * 0.30 / 1_000_000 + 3_072 * 2.50 / 1_000_000
        self.assertAlmostEqual(expected, projected, places=9)

    def test_projected_cost_is_never_below_actual_for_valid_usage(self) -> None:
        price = self.price()
        projected = projected_call_cost_usd(
            prompt_tokens=2_000, max_output_tokens=2_560, price=price
        )
        actual = actual_call_cost_usd(
            prompt_tokens=2_000,
            candidate_tokens=900,
            thought_tokens=700,
            price=price,
        )
        self.assertLessEqual(actual, projected)

    def test_negative_inputs_are_clamped(self) -> None:
        price = self.price()
        self.assertEqual(0.0, input_cost_usd(-5, price))
        self.assertEqual(0.0, output_cost_usd(-1, -1, price))


class UsageMetadataTests(unittest.TestCase):
    def price(self) -> ModelPrice:
        return lookup_price("gemini-3.6-flash", now=TODAY)

    def test_full_metadata_is_parsed_and_priced(self) -> None:
        totals = usage_totals_from_metadata(
            {
                "promptTokenCount": 2_000,
                "candidatesTokenCount": 700,
                "thoughtsTokenCount": 300,
                "totalTokenCount": 3_000,
            },
            self.price(),
        )
        self.assertIsNotNone(totals)
        assert totals is not None
        self.assertEqual(2_000, totals.prompt_tokens)
        self.assertEqual(700, totals.candidate_tokens)
        self.assertEqual(300, totals.thought_tokens)
        self.assertEqual(1, totals.billable_requests)
        expected = 2_000 * 0.75 / 1_000_000 + 1_000 * 4.50 / 1_000_000
        self.assertAlmostEqual(expected, totals.cost_usd, places=9)

    def test_missing_thinking_tokens_count_as_zero(self) -> None:
        totals = usage_totals_from_metadata(
            {"promptTokenCount": 100, "candidatesTokenCount": 50},
            self.price(),
        )
        assert totals is not None
        self.assertEqual(0, totals.thought_tokens)
        self.assertEqual(150, totals.total_tokens)

    def test_malformed_metadata_returns_none(self) -> None:
        price = self.price()
        self.assertIsNone(usage_totals_from_metadata(None, price))
        self.assertIsNone(usage_totals_from_metadata({}, price))
        self.assertIsNone(
            usage_totals_from_metadata({"promptTokenCount": "many"}, price)
        )
        self.assertIsNone(
            usage_totals_from_metadata(
                {"promptTokenCount": 10, "candidatesTokenCount": None}, price
            )
        )


class UsageTotalsTests(unittest.TestCase):
    def test_accumulation(self) -> None:
        total = UsageTotals()
        first = UsageTotals(100, 50, 10, 160, 1, 0.001)
        second = UsageTotals(200, 60, 20, 280, 1, 0.002)
        total.add(first)
        total.add(second)
        self.assertEqual(300, total.prompt_tokens)
        self.assertEqual(110, total.candidate_tokens)
        self.assertEqual(30, total.thought_tokens)
        self.assertEqual(440, total.total_tokens)
        self.assertEqual(2, total.billable_requests)
        self.assertAlmostEqual(0.003, total.cost_usd, places=9)


if __name__ == "__main__":
    unittest.main()
