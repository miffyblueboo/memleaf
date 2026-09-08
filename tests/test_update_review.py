"""Focused contracts for the final automatic UPDATE semantic review."""

from __future__ import annotations

import json
import unittest

from memleaf.admission import EvidenceUnit
from memleaf.inbox import InboxEvent, InboxTurn
from memleaf.llm import ModelError
from memleaf.models import Memory
from memleaf.turn_audit import TurnAudit
from memleaf.turn_plan import revision_digest
from memleaf.update_coordinator import UpdateCoordinator
from memleaf.update_review import (
    UPDATE_SEMANTIC_REVIEW_SYSTEM,
    build_update_review_prompt,
    parse_update_review_output,
    review_update,
)
from memleaf.validation import ModelOutputError


class RawExecutor:
    def __init__(self, raw=None, error=None):
        self.raw = raw
        self.error = error
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
        if self.error is not None:
            raise self.error
        return parser(self.raw)


class GroupThenReviewExecutor:
    """Return real group and semantic-review JSON through the supplied parsers."""

    def __init__(self, group_raw, review_raw):
        self.group_raw = group_raw
        self.review_raw = review_raw
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
        self.calls.append({"prompt": prompt, "system": system, "purpose": purpose})
        raw = self.review_raw if prompt.startswith("UPDATE_SEMANTIC_REVIEW\n") else self.group_raw
        return parser(raw)


def target():
    return {
        "memory_id": "mem-target",
        "title": "Release obligations",
        "body": "Keep security review, rollback plan, and release date current.",
        "type": "todo",
        "scopes": ["global"],
        "status": "active",
        "completed_at": None,
        "due_date": "2026-09-30",
        "sources": [{"event_key": "old"}],
        "native": True,
        "created": "should-not-be-sent",
    }


def source():
    return [{
        "event_key": "new-event",
        "timestamp": "2026-09-08T00:00:00Z",
        "role": "user",
        "content": "Add the dependency scan before release.",
        "evidence_origin": "user_assertion",
        "unit_id": "unit-new",
        "section_path": [],
        "untrusted": "dropped",
    }]


def proposal():
    return {
        "update_memory_id": "mem-target",
        "title": "Release obligations",
        "body": "Keep security review, rollback plan, release date, and dependency scan.",
        "tags": [],
        "type": "todo",
        "scopes": ["global"],
        "sources": [{"event_key": "new-event"}],
        "status": "active",
        "due_date": "2026-09-30",
    }


class UpdateReviewTests(unittest.TestCase):
    def test_prompt_separates_safe_target_source_and_proposal(self):
        prompt = build_update_review_prompt(target(), source(), proposal())
        self.assertTrue(prompt.startswith("UPDATE_SEMANTIC_REVIEW\n"))
        self.assertIn('"active_target"', prompt)
        self.assertIn('"admitted_source"', prompt)
        self.assertIn('"proposed_summary"', prompt)
        self.assertIn("completed_at", prompt)
        self.assertNotIn('"native":true', prompt)
        self.assertNotIn("should-not-be-sent", prompt)
        self.assertNotIn('"untrusted":"dropped"', prompt)

    def test_accept_is_a_strict_real_review_response(self):
        executor = RawExecutor(json.dumps({"decision": "ACCEPT"}))
        result = review_update(
            executor,
            "backend",
            target=target(),
            admitted_source=source(),
            proposed_summary=proposal(),
            parse_summary=lambda value: value,
        )
        self.assertEqual(result, {"decision": "ACCEPT"})
        self.assertEqual(len(executor.calls), 1)
        self.assertIs(executor.calls[0]["system"], UPDATE_SEMANTIC_REVIEW_SYSTEM)
        self.assertEqual(executor.calls[0]["purpose"], "summarize")

    def test_revise_uses_the_supplied_summary_parser(self):
        revised = {"update_memory_id": "mem-target", "body": "complete"}
        seen = []
        parsed = {"update_memory_id": "mem-target", "body": "parsed"}
        executor = RawExecutor(json.dumps({"decision": "REVISE", "summary": revised}))

        def parse_summary(value):
            seen.append(value)
            return parsed

        result = review_update(
            executor,
            "backend",
            target=target(),
            admitted_source=source(),
            proposed_summary=proposal(),
            parse_summary=parse_summary,
        )
        self.assertEqual(result, {"decision": "REVISE", "summary": parsed})
        self.assertEqual(seen, [revised])

    def test_no_change_and_deferred_are_minimal(self):
        for raw in (
            {"decision": "NO_CHANGE"},
            {"decision": "DEFERRED", "reason": "target_preservation_uncertain"},
        ):
            with self.subTest(raw=raw):
                executor = RawExecutor(json.dumps(raw))
                result = review_update(
                    executor,
                    "backend",
                    target=target(),
                    admitted_source=source(),
                    proposed_summary=proposal(),
                    parse_summary=lambda value: value,
                )
                self.assertEqual(result, raw)

    def test_model_or_schema_failure_fails_closed_without_write_shape(self):
        for executor in (
            RawExecutor(error=ModelError("review unavailable")),
            RawExecutor(raw=json.dumps({"decision": "REVISE", "summary": {}})),
        ):
            with self.subTest(executor=executor):
                result = review_update(
                    executor,
                    "backend",
                    target=target(),
                    admitted_source=source(),
                    proposed_summary=proposal(),
                    parse_summary=lambda value: (_ for _ in ()).throw(
                        ModelOutputError("invalid revised summary")
                    ),
                )
                self.assertEqual(result, {
                    "decision": "DEFERRED",
                    "reason": "semantic_review_failed",
                })

    def test_strict_parser_rejects_extra_fields(self):
        with self.assertRaises(ModelOutputError):
            parse_update_review_output(
                {"decision": "ACCEPT", "summary": {}},
                parse_summary=lambda value: value,
            )
        with self.assertRaises(ModelOutputError):
            parse_update_review_output(
                {"decision": "DEFERRED", "reason": "free-form"},
                parse_summary=lambda value: value,
            )

    def test_oversized_review_defers_before_executor_call(self):
        executor = RawExecutor(json.dumps({"decision": "ACCEPT"}))
        oversized = dict(proposal(), body="x" * (128 * 1024))
        result = review_update(
            executor,
            "backend",
            target=target(),
            admitted_source=source(),
            proposed_summary=oversized,
            parse_summary=lambda value: value,
        )
        self.assertEqual(result, {
            "decision": "DEFERRED",
            "reason": "semantic_review_failed",
        })
        self.assertEqual(executor.calls, [])

    def test_final_group_request_is_reviewed_after_consolidation(self):
        target_memory = Memory.new(
            memory_id="mem-target",
            title="Existing obligations",
            body="Keep security review and rollback plan.",
            type="fact",
            scopes=["global"],
        )
        turn = InboxTurn("src", "session", "turn", 1, ())
        audit = TurnAudit()
        audit._deferred_by_turn[(turn.source, turn.session_id, turn.turn_key)] = []
        units = [
            EvidenceUnit("unit-1", "event-1", "user_assertion", "Add scan.", source_role="user"),
            EvidenceUnit("unit-2", "event-2", "user_assertion", "Add signoff.", source_role="user"),
        ]

        def candidate(candidate_id, unit_id, event_key, text):
            return {
                "candidate_id": candidate_id,
                "memory": text,
                "evidence_event_ids": [event_key],
                "evidence_unit_ids": [unit_id],
                "_evidence_bindings": [
                    {"unit_id": unit_id, "quote": text, "role": "assertion"},
                ],
                "type": "fact",
                "scopes": ["global"],
                "scope_source": None,
            }

        candidates = {
            "c1": candidate("c1", "unit-1", "event-1", "Add scan."),
            "c2": candidate("c2", "unit-2", "event-2", "Add signoff."),
        }
        summary_1 = {
            "update_memory_id": "mem-target",
            "title": "Existing obligations",
            "body": "Add scan.",
            "tags": [],
            "type": "fact",
            "scopes": ["global"],
            "sources": [{"event_key": "event-1"}],
        }
        summary_2 = dict(summary_1, body="Add signoff.", sources=[{"event_key": "event-2"}])
        requests = [
            {
                "summary": summary_1,
                "turn": turn,
                "candidate_id": "c1",
                "expected_revision": revision_digest(target_memory),
                "evidence_unit_ids": ["unit-1"],
            },
            {
                "summary": summary_2,
                "turn": turn,
                "candidate_id": "c2",
                "expected_revision": revision_digest(target_memory),
                "evidence_unit_ids": ["unit-2"],
            },
        ]
        merged_summary = {
            "update_memory_id": "mem-target",
            "title": "Existing obligations",
            "body": "Keep security review, rollback plan, scan, and signoff.",
            "tags": [],
            "type": "fact",
            "scopes": ["global"],
            "sources": [{"event_key": "event-1"}, {"event_key": "event-2"}],
        }
        group_raw = json.dumps({
            "decision": "UPDATE",
            "candidate_ids": ["c1", "c2"],
            "summary": merged_summary,
        })
        executor = GroupThenReviewExecutor(group_raw, json.dumps({"decision": "ACCEPT"}))
        result = UpdateCoordinator(
            executor,
            audit,
            lambda memory_id: target_memory if memory_id == "mem-target" else None,
        ).resolve(
            requests,
            candidates=candidates,
            evidence_units=units,
            events=[
                {"event_key": "event-1", "timestamp": "2026-09-08T00:00:00Z"},
                {"event_key": "event-2", "timestamp": "2026-09-08T00:00:00Z"},
            ],
            backend="backend",
            scope_registry={},
            validation_scope_registry={},
        )
        self.assertEqual(len(result), 1)
        for key, value in merged_summary.items():
            self.assertEqual(result[0]["summary"][key], value)
        self.assertEqual(
            result[0]["contributing_candidates"],
            [
                {"candidate_id": "c1", "evidence_unit_ids": ["unit-1"]},
                {"candidate_id": "c2", "evidence_unit_ids": ["unit-2"]},
            ],
        )
        self.assertEqual(result[0]["evidence_unit_ids"], ["unit-1", "unit-2"])
        self.assertTrue(executor.calls[-1]["prompt"].startswith("UPDATE_SEMANTIC_REVIEW\n"))

    def test_invalid_revision_target_or_authorization_is_deferred(self):
        target_memory = Memory.new(
            memory_id="mem-target",
            title="Existing fact",
            body="Keep the existing fact.",
            type="fact",
            scopes=["global"],
        )
        turn = InboxTurn(
            "src",
            "session",
            "turn",
            1,
            (
                InboxEvent(
                    "src",
                    "session",
                    "turn",
                    1,
                    "user",
                    "event-1",
                    "Add scan.",
                    timestamp="2026-09-08T00:00:00Z",
                ),
            ),
        )
        unit = EvidenceUnit("unit-1", "event-1", "user_assertion", "Add scan.", source_role="user")
        candidate = {
            "candidate_id": "c1",
            "memory": "Add scan.",
            "evidence_event_ids": ["event-1"],
            "evidence_unit_ids": ["unit-1"],
            "_evidence_bindings": [{"unit_id": "unit-1", "quote": "Add scan.", "role": "assertion"}],
            "type": "fact",
            "scopes": ["global"],
            "scope_source": None,
        }
        request_summary = {
            "update_memory_id": "mem-target",
            "title": "Existing fact",
            "body": "Keep the existing fact and add scan.",
            "tags": [],
            "type": "fact",
            "scopes": ["global"],
            "sources": [{"event_key": "event-1"}],
        }

        def run(review_summary):
            audit = TurnAudit()
            turn_ref = (turn.source, turn.session_id, turn.turn_key)
            audit._deferred_by_turn[turn_ref] = []
            executor = RawExecutor(json.dumps({"decision": "REVISE", "summary": review_summary}))
            result = UpdateCoordinator(
                executor,
                audit,
                lambda memory_id: target_memory if memory_id == "mem-target" else None,
            ).resolve(
                [{
                    "summary": request_summary,
                    "turn": turn,
                    "candidate_id": "c1",
                    "expected_revision": revision_digest(target_memory),
                    "evidence_unit_ids": ["unit-1"],
                }],
                candidates={"c1": candidate},
                evidence_units=[unit],
                events=[{"event_key": "event-1", "timestamp": "2026-09-08T00:00:00Z"}],
                backend="backend",
                scope_registry={},
                validation_scope_registry={},
            )
            return result, audit._dispositions_by_turn[turn_ref]

        base = dict(request_summary, sources=[{"event_key": "event-1"}])
        cases = {
            "omitted_target": dict(base),
            "different_target": dict(base, update_memory_id="mem-other"),
            "scope_authorization": dict(
                base,
                scope_operations=[{
                    "op": "upsert",
                    "scope": "project:new",
                    "parent": "global",
                    "aliases": [],
                }],
            ),
            "native_authorization": dict(base, shadow_native_ids=["native-1"]),
        }
        cases["omitted_target"].pop("update_memory_id")
        for name, review_summary in cases.items():
            with self.subTest(name=name):
                result, rows = run(review_summary)
                self.assertEqual(result, [])
                self.assertEqual(rows[0]["disposition"], "DEFERRED")
                self.assertEqual(rows[0]["reason"], "semantic_review_failed")


if __name__ == "__main__":
    unittest.main()
