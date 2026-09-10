from __future__ import annotations

import json
import unittest

from memleaf.maintenance_plan_protocol import (
    PROTOCOL_VERSION,
    parse_maintenance_plan_output,
    validate_lookup_states,
)
from memleaf.validation import ModelOutputError


def lookup(status: str, targets=()):
    return {
        "status": status,
        "allowed_target_memory_ids": list(targets),
    }


def summary_validator(candidate_id, decision, target, summary):
    if set(summary) != {"title", "body"}:
        raise ModelOutputError("bad summary", validation_detail="candidate_shape")
    return {
        "title": summary["title"],
        "body": summary["body"],
        "validated_for": candidate_id,
        "validated_decision": decision,
        "validated_target": target,
    }


def envelope(items, *, version=PROTOCOL_VERSION, extra=None):
    value = {
        "protocol_version": version,
        "items": items,
    }
    if extra is not None:
        value["extra"] = extra
    return json.dumps(value)


class MaintenancePlanProtocolTests(unittest.TestCase):
    def test_valid_four_decisions_are_reordered_to_candidate_order(self):
        candidate_ids = ["c-create", "c-update", "c-nochange", "c-defer"]
        lookups = {
            "c-create": lookup("complete_no_target"),
            "c-update": lookup("complete_candidates", ["Mem-A", "Mem-B"]),
            "c-nochange": lookup("complete_candidates", ["Mem-C"]),
            "c-defer": lookup("too_many_candidates", ["Mem-D"]),
        }
        raw = envelope([
            {"candidate_id": "c-defer", "decision": "DEFERRED", "reason": "target_ambiguous"},
            {"candidate_id": "c-nochange", "decision": "NO_CHANGE", "target_memory_id": "mem-c"},
            {
                "candidate_id": "c-update",
                "decision": "UPDATE",
                "target_memory_id": "mem-a",
                "summary": {"title": "New", "body": "Updated fact."},
            },
            {
                "candidate_id": "c-create",
                "decision": "CREATE",
                "summary": {"title": "Fresh", "body": "Fresh fact."},
            },
        ])
        result = parse_maintenance_plan_output(
            raw,
            candidate_ids=candidate_ids,
            lookup_states=lookups,
            validate_summary=summary_validator,
        )
        self.assertEqual([item["candidate_id"] for item in result], candidate_ids)
        self.assertEqual(result[1]["target_memory_id"], "Mem-A")
        self.assertEqual(result[2]["target_memory_id"], "Mem-C")
        self.assertEqual(result[0]["summary"]["validated_decision"], "CREATE")
        self.assertEqual(result[1]["summary"]["validated_target"], "Mem-A")
        self.assertEqual(result[3], {
            "candidate_id": "c-defer",
            "decision": "DEFERRED",
            "reason": "target_ambiguous",
        })

    def test_incomplete_lookup_states_can_only_defer(self):
        reasons = {
            "too_many_candidates": "lookup_incomplete",
            "search_error": "lookup_failed",
            "evidence_insufficient": "evidence_insufficient",
        }
        for status, reason in reasons.items():
            with self.subTest(status=status):
                lookups = {"c1": lookup(status, ["m1"] if status == "too_many_candidates" else [])}
                create = envelope([
                    {
                        "candidate_id": "c1",
                        "decision": "CREATE",
                        "summary": {"title": "x", "body": "y"},
                    }
                ])
                with self.assertRaises(ModelOutputError):
                    parse_maintenance_plan_output(
                        create,
                        candidate_ids=["c1"],
                        lookup_states=lookups,
                        validate_summary=summary_validator,
                    )
                deferred = envelope([
                    {"candidate_id": "c1", "decision": "DEFERRED", "reason": reason}
                ])
                result = parse_maintenance_plan_output(
                    deferred,
                    candidate_ids=["c1"],
                    lookup_states=lookups,
                    validate_summary=summary_validator,
                )
                self.assertEqual(result[0]["decision"], "DEFERRED")

    def test_incomplete_lookup_reason_must_match_failure_class(self):
        raw = envelope([
            {"candidate_id": "c1", "decision": "DEFERRED", "reason": "maintenance_uncertain"}
        ])
        with self.assertRaises(ModelOutputError):
            parse_maintenance_plan_output(
                raw,
                candidate_ids=["c1"],
                lookup_states={"c1": lookup("search_error")},
                validate_summary=summary_validator,
            )

    def test_complete_no_target_cannot_update_or_no_change(self):
        for decision in ("UPDATE", "NO_CHANGE"):
            with self.subTest(decision=decision):
                item = {
                    "candidate_id": "c1",
                    "decision": decision,
                    "target_memory_id": "m1",
                }
                if decision == "UPDATE":
                    item["summary"] = {"title": "x", "body": "y"}
                with self.assertRaises(ModelOutputError):
                    parse_maintenance_plan_output(
                        envelope([item]),
                        candidate_ids=["c1"],
                        lookup_states={"c1": lookup("complete_no_target")},
                        validate_summary=summary_validator,
                    )

    def test_target_decisions_require_lookup_authorized_target(self):
        raw = envelope([
            {
                "candidate_id": "c1",
                "decision": "UPDATE",
                "target_memory_id": "m2",
                "summary": {"title": "x", "body": "y"},
            }
        ])
        with self.assertRaises(ModelOutputError):
            parse_maintenance_plan_output(
                raw,
                candidate_ids=["c1"],
                lookup_states={"c1": lookup("complete_candidates", ["m1"])},
                validate_summary=summary_validator,
            )

    def test_candidate_coverage_is_exact_and_duplicate_rows_fail(self):
        missing = envelope([
            {"candidate_id": "c1", "decision": "CREATE", "summary": {"title": "x", "body": "y"}}
        ])
        with self.assertRaises(ModelOutputError):
            parse_maintenance_plan_output(
                missing,
                candidate_ids=["c1", "c2"],
                lookup_states={
                    "c1": lookup("complete_no_target"),
                    "c2": lookup("complete_no_target"),
                },
                validate_summary=summary_validator,
            )
        duplicate = envelope([
            {"candidate_id": "c1", "decision": "CREATE", "summary": {"title": "x", "body": "y"}},
            {"candidate_id": "c1", "decision": "CREATE", "summary": {"title": "x2", "body": "y2"}},
        ])
        with self.assertRaises(ModelOutputError):
            parse_maintenance_plan_output(
                duplicate,
                candidate_ids=["c1"],
                lookup_states={"c1": lookup("complete_no_target")},
                validate_summary=summary_validator,
            )

    def test_decision_specific_fields_are_strict(self):
        cases = [
            {"candidate_id": "c1", "decision": "CREATE"},
            {
                "candidate_id": "c1",
                "decision": "CREATE",
                "summary": {"title": "x", "body": "y"},
                "target_memory_id": "m1",
            },
            {"candidate_id": "c1", "decision": "DEFERRED", "reason": "lookup_failed", "summary": {}},
        ]
        for item in cases:
            with self.subTest(item=item):
                with self.assertRaises(ModelOutputError):
                    parse_maintenance_plan_output(
                        envelope([item]),
                        candidate_ids=["c1"],
                        lookup_states={"c1": lookup("complete_no_target")},
                        validate_summary=summary_validator,
                    )

    def test_protocol_version_and_envelope_fields_are_strict(self):
        item = {
            "candidate_id": "c1",
            "decision": "CREATE",
            "summary": {"title": "x", "body": "y"},
        }
        for raw in (
            envelope([item], version="p4-maintenance-plan-v0"),
            envelope([item], extra=True),
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ModelOutputError):
                    parse_maintenance_plan_output(
                        raw,
                        candidate_ids=["c1"],
                        lookup_states={"c1": lookup("complete_no_target")},
                        validate_summary=summary_validator,
                    )

    def test_lookup_state_shape_is_strict_and_targets_are_casefold_unique(self):
        with self.assertRaises(ModelOutputError):
            validate_lookup_states(
                ["c1"],
                {"c1": lookup("complete_candidates", ["Mem-A", "mem-a"])},
            )
        with self.assertRaises(ModelOutputError):
            validate_lookup_states(
                ["c1"],
                {"c1": {"status": "complete_no_target"}},
            )
        with self.assertRaises(ModelOutputError):
            validate_lookup_states(
                ["c1"],
                {"c1": lookup("complete_candidates")},
            )


if __name__ == "__main__":
    unittest.main()
