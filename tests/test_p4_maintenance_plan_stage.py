from __future__ import annotations

import json
import unittest

from memleaf.maintenance_plan_protocol import PROTOCOL_VERSION
from memleaf.maintenance_plan_stage import (
    MAINTENANCE_PLAN_SYSTEM,
    MAX_PLAN_ITEMS,
    build_maintenance_plan_prompt,
    run_maintenance_plan_stage,
)
from memleaf.validation import ModelOutputError


def candidate(candidate_id: str, **extra):
    value = {
        "candidate_id": candidate_id,
        "memory": f"Memory {candidate_id}",
        "type": "fact",
        "scopes": ["global"],
        "scope_source": "model",
    }
    value.update(extra)
    return value


def evidence(candidate_id: str):
    return [{
        "event_key": f"event-{candidate_id}",
        "timestamp": "2026-09-10T08:00:00Z",
        "role": "user",
        "content": f"Fact for {candidate_id}.",
        "evidence_origin": "user_assertion",
        "extra_untrusted_metadata": "must-not-project",
    }]


def lookup(status: str, targets=()):
    return {
        "status": status,
        "allowed_target_memory_ids": list(targets),
    }


def related(memory_id: str):
    return {
        "memory_id": memory_id,
        "title": f"Old {memory_id}",
        "body": "Old state.",
        "type": "fact",
        "scopes": ["global"],
        "sources": [{"event_key": "old"}],
    }


def validate_summary(candidate_id, decision, target, summary):
    if not isinstance(summary.get("title"), str) or not isinstance(summary.get("body"), str):
        raise ModelOutputError("invalid test summary", validation_detail="candidate_shape")
    value = dict(summary)
    value["validated_for"] = candidate_id
    value["validated_decision"] = decision
    value["validated_target"] = target
    return value


class FakeExecutor:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def _complete_json_stage(
        self,
        backend,
        prompt,
        *,
        system,
        purpose,
        parser,
        diagnostic_context=None,
    ):
        self.calls.append({
            "backend": backend,
            "prompt": prompt,
            "system": system,
            "purpose": purpose,
            "diagnostic_context": diagnostic_context,
        })
        return parser(self.raw)


class MaintenancePlanStageTests(unittest.TestCase):
    def test_prompt_excludes_gate_target_fields_and_extra_evidence_metadata(self):
        candidates = [candidate(
            "c1",
            update_memory_id="stale-target",
            duplicate_memory_id="stale-duplicate",
            duplicate=False,
            worth=True,
            extra_secret="do-not-project",
        )]
        prompt, candidate_ids, lookups = build_maintenance_plan_prompt(
            candidates=candidates,
            evidence_by_candidate={"c1": evidence("c1")},
            lookup_states={"c1": lookup("complete_candidates", ["m1"])},
            related_memories_by_candidate={"c1": [related("m1")]},
            native_context_by_candidate={"c1": [{
                "native_id": "native-1",
                "title": "Native context",
                "content": "Comparison only.",
                "password": "must-not-project",
            }]},
        )
        self.assertEqual(candidate_ids, ["c1"])
        self.assertEqual(lookups["c1"]["allowed_target_memory_ids"], ["m1"])
        self.assertNotIn("stale-target", prompt)
        self.assertNotIn("stale-duplicate", prompt)
        self.assertNotIn("do-not-project", prompt)
        self.assertNotIn("must-not-project", prompt)
        self.assertIn("native-1", prompt)
        self.assertIn("m1", prompt)
        self.assertIn(PROTOCOL_VERSION, prompt)

    def test_complete_candidates_requires_exact_related_target_context(self):
        with self.assertRaises(ModelOutputError):
            build_maintenance_plan_prompt(
                candidates=[candidate("c1")],
                evidence_by_candidate={"c1": evidence("c1")},
                lookup_states={"c1": lookup("complete_candidates", ["m1", "m2"])},
                related_memories_by_candidate={"c1": [related("m1")]},
            )
        with self.assertRaises(ModelOutputError):
            build_maintenance_plan_prompt(
                candidates=[candidate("c1")],
                evidence_by_candidate={"c1": evidence("c1")},
                lookup_states={"c1": lookup("complete_no_target")},
                related_memories_by_candidate={"c1": [related("m1")]},
            )

    def test_incomplete_lookup_may_expose_only_declared_partial_targets(self):
        prompt, _, _ = build_maintenance_plan_prompt(
            candidates=[candidate("c1")],
            evidence_by_candidate={"c1": evidence("c1")},
            lookup_states={"c1": lookup("too_many_candidates", ["m1", "m2"])},
            related_memories_by_candidate={"c1": [related("m1")]},
        )
        self.assertIn("too_many_candidates", prompt)
        with self.assertRaises(ModelOutputError):
            build_maintenance_plan_prompt(
                candidates=[candidate("c1")],
                evidence_by_candidate={"c1": evidence("c1")},
                lookup_states={"c1": lookup("too_many_candidates", ["m1"])},
                related_memories_by_candidate={"c1": [related("m2")]},
            )

    def test_tool_role_cannot_enter_admitted_evidence(self):
        rows = evidence("c1")
        rows[0]["role"] = "tool"
        with self.assertRaises(ModelOutputError):
            build_maintenance_plan_prompt(
                candidates=[candidate("c1")],
                evidence_by_candidate={"c1": rows},
                lookup_states={"c1": lookup("complete_no_target")},
                related_memories_by_candidate={"c1": []},
            )

    def test_run_stage_makes_exactly_one_model_call_and_parses_by_candidate_id(self):
        raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [
                {
                    "candidate_id": "c2",
                    "decision": "UPDATE",
                    "target_memory_id": "M2",
                    "summary": {"title": "Two", "body": "New two."},
                },
                {
                    "candidate_id": "c1",
                    "decision": "CREATE",
                    "summary": {"title": "One", "body": "New one."},
                },
            ],
        })
        executor = FakeExecutor(raw)
        result = run_maintenance_plan_stage(
            executor,
            "backend",
            candidates=[candidate("c1"), candidate("c2")],
            evidence_by_candidate={"c1": evidence("c1"), "c2": evidence("c2")},
            lookup_states={
                "c1": lookup("complete_no_target"),
                "c2": lookup("complete_candidates", ["m2"]),
            },
            related_memories_by_candidate={
                "c1": [],
                "c2": [related("m2")],
            },
            validate_summary=validate_summary,
            diagnostic_context={"session_id": "s"},
        )
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual([item["candidate_id"] for item in result], ["c1", "c2"])
        self.assertEqual(result[1]["target_memory_id"], "m2")
        self.assertEqual(result[1]["summary"]["validated_target"], "m2")
        self.assertEqual(executor.calls[0]["system"], MAINTENANCE_PLAN_SYSTEM)
        self.assertEqual(executor.calls[0]["purpose"], "summarize")

    def test_native_context_never_authorizes_update_target(self):
        raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [{
                "candidate_id": "c1",
                "decision": "UPDATE",
                "target_memory_id": "native-1",
                "summary": {"title": "x", "body": "y"},
            }],
        })
        executor = FakeExecutor(raw)
        with self.assertRaises(ModelOutputError):
            run_maintenance_plan_stage(
                executor,
                "backend",
                candidates=[candidate("c1")],
                evidence_by_candidate={"c1": evidence("c1")},
                lookup_states={"c1": lookup("complete_candidates", ["m1"])},
                related_memories_by_candidate={"c1": [related("m1")]},
                native_context_by_candidate={"c1": [{
                    "native_id": "native-1",
                    "content": "Comparison only.",
                }]},
                validate_summary=validate_summary,
            )
        self.assertEqual(len(executor.calls), 1)

    def test_more_than_max_batch_fails_before_model_call(self):
        candidates = [candidate(f"c{i}") for i in range(MAX_PLAN_ITEMS + 1)]
        ids = [item["candidate_id"] for item in candidates]
        executor = FakeExecutor("{}")
        with self.assertRaises(ModelOutputError):
            run_maintenance_plan_stage(
                executor,
                "backend",
                candidates=candidates,
                evidence_by_candidate={value: evidence(value) for value in ids},
                lookup_states={value: lookup("complete_no_target") for value in ids},
                related_memories_by_candidate={value: [] for value in ids},
                validate_summary=validate_summary,
            )
        self.assertEqual(executor.calls, [])

    def test_prompt_budget_fails_closed_without_hidden_split(self):
        huge = evidence("c1")
        huge[0]["content"] = "x" * (300 * 1024)
        executor = FakeExecutor("{}")
        with self.assertRaises(ModelOutputError):
            run_maintenance_plan_stage(
                executor,
                "backend",
                candidates=[candidate("c1")],
                evidence_by_candidate={"c1": huge},
                lookup_states={"c1": lookup("complete_no_target")},
                related_memories_by_candidate={"c1": []},
                validate_summary=validate_summary,
            )
        self.assertEqual(executor.calls, [])


if __name__ == "__main__":
    unittest.main()
