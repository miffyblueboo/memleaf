"""Unit contracts for candidate-level target reconciliation.

These tests exercise only the pure helper and a fake ModelExecutor boundary;
they do not construct a Vault, contact a model service, or perform writes.
"""

from __future__ import annotations

import copy
import json
import unittest

from memleaf.llm import ModelError
from memleaf.target_reconciliation import reconcile_candidate_target


class RawExecutor:
    """Minimal bounded-stage double that still runs the supplied strict parser."""

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


def candidate(*, memory="alpha deployment changes the rollout rule.", type="todo"):
    return {
        "candidate_id": "candidate-1",
        "memory": memory,
        "evidence_event_ids": ["hermes/session/turn/user"],
        "_evidence_bindings": [{
            "candidate_id": "candidate-1",
            "claims": [{
                "unit_id": "unit-1",
                "quote": "The rollout rule changed.",
                "role": "assertion",
            }],
        }],
        "duplicate": False,
        "worth": True,
        "type": type,
        "scopes": ["project:alpha"],
        "scope_source": "model",
    }


def memory(
    memory_id,
    *,
    title="alpha deployment rule",
    body="alpha deployment rule uses the old rollout.",
    type="project",
    scopes=None,
    **extra,
):
    return {
        "memory_id": memory_id,
        "title": title,
        "body": body,
        "type": type,
        "scopes": ["project:alpha"] if scopes is None else list(scopes),
        **extra,
    }


class TargetReconciliationTests(unittest.TestCase):
    def run_reconciliation(self, raw, *, related=None, source=None):
        original = candidate()
        executor = RawExecutor(raw) if source is None else source
        result = reconcile_candidate_target(
            executor,
            object(),
            original,
            related_memories=related or [memory("mem-alpha")],
            validated_bindings=original["_evidence_bindings"],
            summary_evidence=[{
                "unit_id": "unit-1",
                "content": "The rollout rule changed.",
            }],
            diagnostic_context={"candidate_id": original["candidate_id"]},
        )
        return original, executor, result

    def test_paraphrase_can_be_no_change_and_keeps_candidate_evidence(self):
        original, executor, result = self.run_reconciliation(
            json.dumps({"decision": "NO_CHANGE", "target_memory_id": "mem-alpha"}),
        )

        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(executor.calls[0]["purpose"], "gate")
        self.assertEqual(executor.calls[0]["backend"].__class__, object)
        prompt = executor.calls[0]["prompt"]
        self.assertIn("unit-1", prompt)
        self.assertIn("mem-alpha", prompt)
        self.assertEqual(result["duplicate_memory_id"], "mem-alpha")
        self.assertTrue(result["duplicate"])
        self.assertFalse(result["worth"])
        self.assertEqual(result["memory"], original["memory"])
        self.assertEqual(result["scopes"], original["scopes"])
        self.assertEqual(result["evidence_event_ids"], original["evidence_event_ids"])
        self.assertEqual(result["_evidence_bindings"], original["_evidence_bindings"])
        self.assertNotIn("update_memory_id", result)

    def test_paraphrase_update_corrects_initial_type_to_immutable_target_type(self):
        original = candidate(type="todo")
        before = copy.deepcopy(original)
        executor = RawExecutor(json.dumps({
            "decision": "UPDATE",
            "target_memory_id": "mem-alpha",
            "type": "project",
        }))
        result = reconcile_candidate_target(
            executor,
            "fake-backend",
            original,
            related_memories=[memory("mem-alpha", type="project")],
            validated_bindings=original["_evidence_bindings"],
        )

        self.assertEqual(result["update_memory_id"], "mem-alpha")
        self.assertEqual(result["type"], "project")
        self.assertTrue(result["worth"])
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["memory"], before["memory"])
        self.assertEqual(result["scopes"], before["scopes"])
        self.assertEqual(result["evidence_event_ids"], before["evidence_event_ids"])
        self.assertEqual(result["_evidence_bindings"], before["_evidence_bindings"])
        self.assertEqual(original, before)
        self.assertEqual(executor.calls[0]["purpose"], "gate")

    def test_conversation_document_and_tool_evidence_share_the_same_contract(self):
        variants = (
            ("conversation", "I prefer tea after lunch.", "preference"),
            ("document", "The guide uses UTF-8.", "fact"),
            ("tool", "The thermometer returned 21 C.", "fact"),
        )
        for source, quote, candidate_type in variants:
            with self.subTest(source=source):
                original = candidate(memory=quote, type=candidate_type)
                original["_evidence_bindings"] = [{
                    "candidate_id": original["candidate_id"],
                    "claims": [{
                        "unit_id": f"{source}-unit",
                        "quote": quote,
                        "role": "assertion",
                        "source": source,
                    }],
                }]
                before = copy.deepcopy(original)
                executor = RawExecutor(json.dumps({
                    "decision": "UPDATE",
                    "target_memory_id": "mem-alpha",
                    "type": "project",
                }))
                result = reconcile_candidate_target(
                    executor,
                    "backend",
                    original,
                    related_memories=[memory("mem-alpha")],
                    validated_bindings=original["_evidence_bindings"],
                )
                self.assertEqual(result["memory"], before["memory"])
                self.assertEqual(result["scopes"], before["scopes"])
                self.assertEqual(result["_evidence_bindings"], before["_evidence_bindings"])
                self.assertEqual(result["update_memory_id"], "mem-alpha")
                self.assertEqual(result["type"], "project")

    def test_same_scope_new_topic_is_allowed_to_create_with_original_type(self):
        original, executor, result = self.run_reconciliation(
            json.dumps({"decision": "CREATE"}),
            related=[memory("mem-alpha", title="alpha access review", body="Review access quarterly.")],
        )

        self.assertEqual(result, original)
        self.assertNotIn("update_memory_id", result)
        self.assertNotIn("duplicate_memory_id", result)
        self.assertNotIn("_defer_reason", result)
        self.assertEqual(result["type"], "todo")
        self.assertEqual(len(executor.calls), 1)

    def test_multiple_same_scope_targets_are_explicitly_deferred(self):
        original, executor, result = self.run_reconciliation(
            json.dumps({"decision": "DEFERRED", "reason": "target_ambiguous"}),
            related=[memory("mem-one"), memory("mem-two", title="alpha deployment policy")],
        )

        self.assertEqual(result["_defer_reason"], "target_ambiguous")
        self.assertNotIn("update_memory_id", result)
        self.assertNotIn("duplicate_memory_id", result)
        self.assertEqual(result["memory"], original["memory"])
        self.assertEqual(result["scopes"], original["scopes"])
        self.assertEqual(result["evidence_event_ids"], original["evidence_event_ids"])
        self.assertEqual(len(executor.calls), 1)

    def test_foreign_native_and_unknown_targets_are_rejected_without_create(self):
        related = [
            memory("mem-alpha"),
            memory("mem-foreign", scopes=["project:beta"]),
            memory("native-alpha", native=True),
        ]
        for target in ("mem-foreign", "native-alpha", "mem-unknown"):
            with self.subTest(target=target):
                original, executor, result = self.run_reconciliation(
                    json.dumps({
                        "decision": "UPDATE",
                        "target_memory_id": target,
                        "type": "project",
                    }),
                    related=related,
                )
                self.assertEqual(result["_defer_reason"], "target_reconciliation_failed")
                self.assertNotIn("update_memory_id", result)
                self.assertNotIn("duplicate_memory_id", result)
                self.assertEqual(result["memory"], original["memory"])
                self.assertEqual(result["scopes"], original["scopes"])
                self.assertEqual(result["evidence_event_ids"], original["evidence_event_ids"])
                self.assertIn('"mem-alpha"', executor.calls[0]["prompt"])
                self.assertNotIn('"mem-foreign"', executor.calls[0]["prompt"])
                self.assertNotIn('"native-alpha"', executor.calls[0]["prompt"])

    def test_model_error_is_preserved_as_hard_failure(self):
        class FailingExecutor:
            def _complete_json_stage(self, *args, **kwargs):
                raise ModelError("bounded stage failed")

        original = candidate()
        with self.assertRaises(ModelError):
            reconcile_candidate_target(
                FailingExecutor(),
                "backend",
                original,
                related_memories=[memory("mem-alpha")],
                validated_bindings=original["_evidence_bindings"],
            )

    def test_update_requires_explicit_matching_target_type(self):
        for raw in (
            {"decision": "UPDATE", "target_memory_id": "mem-alpha"},
            {"decision": "UPDATE", "target_memory_id": "mem-alpha", "type": "todo"},
        ):
            with self.subTest(raw=raw):
                original, executor, result = self.run_reconciliation(json.dumps(raw))
                self.assertEqual(result["_defer_reason"], "target_reconciliation_failed")
                self.assertNotIn("update_memory_id", result)
                self.assertEqual(original["type"], "todo")
                self.assertEqual(executor.calls[0]["purpose"], "gate")

    def test_non_string_or_list_decision_is_a_safe_schema_failure(self):
        for decision in (None, [], ["UPDATE"], {}):
            with self.subTest(decision=decision):
                _, _, result = self.run_reconciliation(
                    json.dumps({"decision": decision}, ensure_ascii=False),
                )
                self.assertEqual(result["_defer_reason"], "target_reconciliation_failed")

    def test_only_validated_proposal_evidence_can_authorize_reconciliation(self):
        original = candidate()
        original.pop("_evidence_bindings")

        class UnexpectedExecutor:
            def _complete_json_stage(self, *args, **kwargs):
                raise AssertionError("candidate proposal alone is not evidence")

        result = reconcile_candidate_target(
            UnexpectedExecutor(),
            "backend",
            original,
            related_memories=[memory("mem-alpha")],
        )
        self.assertEqual(result["_defer_reason"], "target_reconciliation_no_evidence")

    def test_complete_related_body_is_kept_when_prompt_is_within_budget(self):
        body = "alpha evidence " * 500
        original, executor, result = self.run_reconciliation(
            json.dumps({"decision": "CREATE"}),
            related=[memory("mem-alpha", body=body)],
        )

        self.assertEqual(result, original)
        self.assertIn(body, executor.calls[0]["prompt"])

    def test_oversized_prompt_fails_closed_without_dropping_evidence(self):
        original = candidate(memory="proposal " + "x" * (64 * 1024))
        executor = RawExecutor(json.dumps({"decision": "CREATE"}))
        result = reconcile_candidate_target(
            executor,
            "backend",
            original,
            related_memories=[memory("mem-alpha")],
            validated_bindings=original["_evidence_bindings"],
        )

        self.assertEqual(result["_defer_reason"], "target_reconciliation_failed")
        self.assertEqual(executor.calls, [])

    def test_related_candidate_count_overflow_fails_closed(self):
        related = [memory(f"mem-{index}") for index in range(9)]
        original = candidate()
        executor = RawExecutor(json.dumps({"decision": "CREATE"}))
        result = reconcile_candidate_target(
            executor,
            "backend",
            original,
            related_memories=related,
            validated_bindings=original["_evidence_bindings"],
        )

        self.assertEqual(result["_defer_reason"], "target_reconciliation_context_too_large")
        self.assertEqual(executor.calls, [])

    def test_precondition_without_same_scope_local_memory_does_not_call_model(self):
        class UnexpectedExecutor:
            def _complete_json_stage(self, *args, **kwargs):
                raise AssertionError("no reconciliation call expected")

        original = candidate()
        result = reconcile_candidate_target(
            UnexpectedExecutor(),
            "backend",
            original,
            related_memories=[memory("foreign", scopes=["project:beta"]), memory("native", native=True)],
        )
        self.assertEqual(result, original)


if __name__ == "__main__":
    unittest.main()
