from __future__ import annotations

import unittest
from decimal import Decimal

from benchmarks.p1.cost_report import summarize_cost


class CostReportTests(unittest.TestCase):
    def test_exact_and_conservative_cost_use_provider_usage_only(self):
        document = {
            "schema_version": 2,
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "thinking": "low",
            "model_calls_used": 2,
            "results": [
                {"call_graph": [
                    {"prompt_tokens": 1000, "completion_tokens": 200,
                     "prompt_cache_hit_tokens": 250, "prompt_cache_miss_tokens": 750},
                    {"prompt_tokens": 500, "completion_tokens": 100,
                     "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 500},
                ]},
            ],
        }
        report = summarize_cost(
            document,
            input_cache_hit_per_million=Decimal("0.10"),
            input_cache_miss_per_million=Decimal("3.0"),
            output_per_million=Decimal("9.0"),
            currency="CNY",
            project_calls=3,
        )
        self.assertTrue(report["exact_cost_available"])
        self.assertTrue(report["conservative_observed_cost_available"])
        self.assertEqual(report["exact_observed_cost"], "0.006475")
        self.assertEqual(report["conservative_observed_cost"], "0.0072")
        self.assertEqual(report["projected_calls"]["conservative_rate_projection"], "0.0144")

    def test_partial_cache_split_never_claims_exact_cost(self):
        document = {
            "results": [{"call_graph": [{
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "prompt_cache_hit_tokens": 100,
                "prompt_cache_miss_tokens": 700,
            }]}]
        }
        report = summarize_cost(
            document,
            input_cache_hit_per_million=Decimal("0.10"),
            input_cache_miss_per_million=Decimal("3.0"),
            output_per_million=Decimal("9.0"),
            currency="CNY",
        )
        self.assertFalse(report["exact_cost_available"])
        self.assertTrue(report["conservative_observed_cost_available"])
        self.assertIsNone(report["exact_observed_cost"])

    def test_missing_provider_tokens_does_not_infer_from_text(self):
        document = {"results": [{"call_graph": [{"input_chars": 5000, "output_chars": 1000}]}]}
        report = summarize_cost(
            document,
            input_cache_hit_per_million=Decimal("0.10"),
            input_cache_miss_per_million=Decimal("3.0"),
            output_per_million=Decimal("9.0"),
            currency="CNY",
        )
        self.assertFalse(report["exact_cost_available"])
        self.assertFalse(report["conservative_observed_cost_available"])
        self.assertIsNone(report["conservative_observed_cost"])


if __name__ == "__main__":
    unittest.main()
