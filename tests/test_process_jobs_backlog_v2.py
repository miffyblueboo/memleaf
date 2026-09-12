from __future__ import annotations

import unittest

from memleaf.process_jobs import _aggregate_attempt_results, _result_status, _safe_result


class ProcessJobsBacklogV2Tests(unittest.TestCase):
    def test_pending_inbox_turns_survive_safe_projection_and_defer_job(self):
        result = {
            "processed_turns": 4,
            "pending_inbox_turns": 3,
            "deferred_candidates": 0,
            "deferred_inbox_turns": 0,
            "unresolved_evidence_count": 0,
        }

        safe = _safe_result(result)

        self.assertEqual(safe["pending_inbox_turns"], 3)
        self.assertEqual(_result_status(safe), "deferred")

    def test_zero_pending_backlog_does_not_defer_success(self):
        result = {
            "processed_turns": 2,
            "pending_inbox_turns": 0,
            "deferred_candidates": 0,
            "deferred_inbox_turns": 0,
            "unresolved_evidence_count": 0,
            "coverage_status": "complete",
        }

        safe = _safe_result(result)

        self.assertEqual(safe["pending_inbox_turns"], 0)
        self.assertEqual(_result_status(safe), "succeeded")

    def test_aggregate_uses_latest_backlog_gauge_not_sum(self):
        attempts = [
            {"result": {"processed_turns": 4, "pending_inbox_turns": 3}},
            {"result": {"processed_turns": 3, "pending_inbox_turns": 0}},
        ]

        aggregate = _aggregate_attempt_results(attempts)

        self.assertEqual(aggregate["processed_turns"], 7)
        self.assertEqual(aggregate["pending_inbox_turns"], 0)


if __name__ == "__main__":
    unittest.main()
