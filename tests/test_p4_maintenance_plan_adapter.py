from __future__ import annotations

import json
import unittest

from memleaf.maintenance_plan_adapter import (
    adapt_core_lookup_contexts,
    compare_shadow_outcomes,
    make_existing_summary_validator,
)
from memleaf.validation import ModelOutputError


def candidate(candidate_id: str):
    return {"candidate_id": candidate_id}


def local(memory_id: str, **extra):
    value = {
        "memory_id": memory_id,
        "title": memory_id,
        "body": "Old state.",
        "type": "fact",
        "scopes": ["global"],
    }
    value.update(extra)
    return value


class MaintenancePlanAdapterTests(unittest.TestCase):
    def test_zero_rows_require_explicit_complete_lookup_before_create_safe_state(self):
        candidates = [candidate("c1")]
        states, local_rows, native_rows = adapt_core_lookup_contexts(
            candidates,
            {"c1": []},
            lookup_complete_by_candidate={"c1": True},
        )
        self.assertEqual(states["c1"], {
            "status": "complete_no_target",
            "allowed_target_memory_ids": [],
        })
        self.assertEqual(local_rows["c1"], [])
        self.assertEqual(native_rows["c1"], [])

        states, _, _ = adapt_core_lookup_contexts(
            candidates,
            {"c1": []},
            lookup_complete_by_candidate={"c1": False},
        )
        self.assertEqual(states["c1"]["status"], "too_many_candidates")

    def test_filters_native_history_inactive_and_deduplicates_local_targets(self):
        rows = [
            local("M1"),
            local("m1", body="duplicate representation"),
            local("m-history", area="history"),
            local("m-inactive", active=False),
            {"native": True, "native_id": "N1", "content": "Native"},
            {"native": True, "native_id": "n1", "content": "Duplicate native"},
        ]
        states, local_rows, native_rows = adapt_core_lookup_contexts(
            [candidate("c1")],
            {"c1": rows},
            lookup_complete_by_candidate={"c1": True},
        )
        self.assertEqual(states["c1"]["status"], "complete_candidates")
        self.assertEqual(states["c1"]["allowed_target_memory_ids"], ["M1"])
        self.assertEqual([row["memory_id"] for row in local_rows["c1"]], ["M1"])
        self.assertEqual([row["native_id"] for row in native_rows["c1"]], ["N1"])

    def test_search_error_and_evidence_insufficient_override_complete_lookup(self):
        candidates = [candidate("c1"), candidate("c2")]
        states, _, _ = adapt_core_lookup_contexts(
            candidates,
            {"c1": [local("m1")], "c2": []},
            lookup_complete_by_candidate={"c1": True, "c2": True},
            search_error_candidate_ids=["c1"],
            evidence_insufficient_candidate_ids=["c2"],
        )
        self.assertEqual(states["c1"]["status"], "search_error")
        self.assertEqual(states["c2"]["status"], "evidence_insufficient")
        self.assertEqual(states["c1"]["allowed_target_memory_ids"], ["m1"])

    def test_large_target_set_is_not_truncated_into_complete_lookup(self):
        rows = [local(f"m{i}") for i in range(4)]
        states, local_rows, _ = adapt_core_lookup_contexts(
            [candidate("c1")],
            {"c1": rows},
            lookup_complete_by_candidate={"c1": True},
            max_local_targets=3,
        )
        self.assertEqual(states["c1"]["status"], "too_many_candidates")
        self.assertEqual(len(states["c1"]["allowed_target_memory_ids"]), 4)
        self.assertEqual(len(local_rows["c1"]), 4)

    def test_lookup_maps_and_failure_markers_must_cover_known_candidates(self):
        with self.assertRaises(ModelOutputError):
            adapt_core_lookup_contexts(
                [candidate("c1")],
                {},
                lookup_complete_by_candidate={"c1": True},
            )
        with self.assertRaises(ModelOutputError):
            adapt_core_lookup_contexts(
                [candidate("c1")],
                {"c1": []},
                lookup_complete_by_candidate={},
            )
        with self.assertRaises(ModelOutputError):
            adapt_core_lookup_contexts(
                [candidate("c1")],
                {"c1": []},
                lookup_complete_by_candidate={"c1": True},
                search_error_candidate_ids=["unknown"],
            )

    def test_existing_summary_validator_delegates_decision_and_canonical_target(self):
        calls = []

        def factory(candidate_id, decision, target):
            calls.append((candidate_id, decision, target))

            def parser(raw):
                value = json.loads(raw)
                value["parsed"] = True
                return value

            return parser

        validator = make_existing_summary_validator(factory)
        create = validator("c1", "CREATE", None, {"title": "A", "body": "B"})
        update = validator(
            "c2",
            "UPDATE",
            "Mem-2",
            {"update_memory_id": "mem-2", "title": "C", "body": "D"},
        )
        self.assertEqual(calls, [
            ("c1", "CREATE", None),
            ("c2", "UPDATE", "Mem-2"),
        ])
        self.assertTrue(create["parsed"])
        self.assertTrue(update["parsed"])
        with self.assertRaises(ModelOutputError):
            validator("c3", "UPDATE", None, {"title": "E", "body": "F"})
        with self.assertRaises(ModelOutputError):
            validator("c3", "UPDATE", "m3", {"title": "E", "body": "F"})
        with self.assertRaises(ModelOutputError):
            validator(
                "c3",
                "UPDATE",
                "m3",
                {"update_memory_id": "wrong", "title": "E", "body": "F"},
            )

    def test_shadow_comparison_reports_exact_candidate_decision_target_parity(self):
        baseline = [
            {"candidate_id": "c1", "disposition": "CREATE"},
            {"candidate_id": "c2", "disposition": "UPDATE", "memory_id": "m2"},
            {"candidate_id": "c3", "disposition": "NO_CHANGE", "memory_id": "m3"},
            {"candidate_id": "c4", "disposition": "DEFERRED"},
        ]
        p4 = [
            {"candidate_id": "c4", "decision": "DEFERRED", "reason": "maintenance_uncertain"},
            {"candidate_id": "c2", "decision": "UPDATE", "target_memory_id": "m2", "summary": {}},
            {"candidate_id": "c1", "decision": "CREATE", "summary": {}},
            {"candidate_id": "c3", "decision": "NO_CHANGE", "target_memory_id": "m3"},
        ]
        report = compare_shadow_outcomes(baseline, p4)
        self.assertTrue(report["candidate_set_equal"])
        self.assertTrue(report["decision_target_equal"])
        self.assertEqual(report["differences"], [])

    def test_shadow_comparison_surfaces_missing_decision_and_target_changes(self):
        report = compare_shadow_outcomes(
            [
                {"candidate_id": "c1", "decision": "UPDATE", "target_memory_id": "m1"},
                {"candidate_id": "c2", "decision": "CREATE"},
            ],
            [
                {"candidate_id": "c1", "decision": "UPDATE", "target_memory_id": "m2"},
                {"candidate_id": "c3", "decision": "CREATE"},
            ],
        )
        self.assertFalse(report["candidate_set_equal"])
        self.assertFalse(report["decision_target_equal"])
        self.assertEqual([row["candidate_id"] for row in report["differences"]], ["c1", "c2", "c3"])


if __name__ == "__main__":
    unittest.main()
