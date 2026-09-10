from __future__ import annotations

import unittest

from benchmarks.p1.summarize_results import summarize


class P1ResultSummaryTests(unittest.TestCase):
    def test_summary_keeps_missing_token_fields_unavailable(self):
        document = {
            "schema_version": 2,
            "arm": "B0-probe",
            "provider": "synthetic",
            "model": "synthetic-model",
            "thinking": "low",
            "results": [{
                "case_id": "AB01_fact",
                "category": "fact",
                "run_status": "success",
                "expected_rubric": ["preserve alpha"],
                "process_wall_ms": 120,
                "fresh_instance_visibility_consistent": True,
                "process_result": {
                    "deferred_candidates": 0,
                    "unresolved_evidence_count": 0,
                },
                "call_graph": [
                    {
                        "stage": "gate",
                        "operation": "gate_primary",
                        "retry": False,
                        "failed": False,
                        "invalid_output": False,
                        "request_duration_ms": 50,
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                    },
                    {
                        "stage": "summarize",
                        "operation": "summary",
                        "retry": False,
                        "failed": False,
                        "invalid_output": False,
                        "request_duration_ms": 40,
                        "prompt_tokens": 60,
                        "completion_tokens": 15,
                    },
                ],
            }],
        }
        report = summarize(document)
        self.assertEqual(report["processes"]["wall_ms_p50"], 120)
        self.assertEqual(report["model_calls"]["total"], 2)
        self.assertEqual(report["model_calls"]["tokens"]["prompt_tokens"]["total"], 160)
        self.assertFalse(report["model_calls"]["tokens"]["reasoning_tokens"]["complete"])
        self.assertIsNone(report["model_calls"]["tokens"]["reasoning_tokens"]["total"])
        self.assertEqual(report["quality_status"], "PENDING_INDEPENDENT_GRADING")
        self.assertEqual(report["scenarios"][0]["semantic_grade_status"], "PENDING_INDEPENDENT_GRADING")

    def test_summary_reports_retry_failure_and_nearest_rank_percentiles(self):
        results = []
        for index, wall in enumerate((100, 200, 300), start=1):
            results.append({
                "case_id": "AB01_fact",
                "run_status": "error" if index == 3 else "success",
                "process_wall_ms": wall,
                "call_graph": [{
                    "stage": "gate",
                    "operation": "gate_primary",
                    "retry": index == 2,
                    "failed": index == 3,
                    "invalid_output": index == 2,
                    "request_duration_ms": wall // 2,
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 10,
                    "reasoning_tokens": 0,
                }],
            })
        report = summarize({"schema_version": 2, "results": results})
        self.assertEqual(report["processes"]["wall_ms_p50"], 200)
        self.assertEqual(report["processes"]["wall_ms_p95"], 300)
        self.assertEqual(report["processes"]["error_count"], 1)
        self.assertEqual(report["model_calls"]["retry_calls"], 1)
        self.assertEqual(report["model_calls"]["failed_calls"], 1)
        self.assertEqual(report["model_calls"]["invalid_output_calls"], 1)
        self.assertTrue(report["model_calls"]["tokens"]["total_tokens"]["complete"])
        self.assertEqual(report["model_calls"]["tokens"]["total_tokens"]["total"], 45)

    def test_empty_results_never_invent_zero_token_or_latency_evidence(self):
        report = summarize({"schema_version": 2, "results": []})
        self.assertIsNone(report["processes"]["wall_ms_p50"])
        self.assertIsNone(report["model_calls"]["request_duration"]["sum_ms"])
        self.assertFalse(report["model_calls"]["tokens"]["prompt_tokens"]["complete"])
        self.assertIsNone(report["model_calls"]["tokens"]["prompt_tokens"]["total"])


if __name__ == "__main__":
    unittest.main()
