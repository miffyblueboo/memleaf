"""Focused contracts for the final automatic UPDATE semantic review."""

from __future__ import annotations

import json
import unittest

from memleaf.admission import EvidenceUnit
from memleaf.inbox import InboxEvent, InboxTurn
from memleaf.llm import ModelError
from memleaf.models import Memory
from memleaf.prompts import GATE_SYSTEM, SUMMARIZE_SYSTEM, gate_prompt, summarize_prompt
from memleaf.turn_audit import TurnAudit
from memleaf.turn_plan import revision_digest
from memleaf.update_coordinator import UpdateCoordinator
from memleaf.update_review import (
    CREATE_SEMANTIC_REVIEW_SYSTEM,
    UPDATE_SEMANTIC_REVIEW_SYSTEM,
    build_create_review_prompt,
    build_update_review_prompt,
    parse_update_review_output,
    review_create,
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
    def test_prompt_boundary_allows_visible_reports_and_excludes_external_payloads(self):
        gate_text = " ".join(GATE_SYSTEM.split()).casefold()
        summarize_text = " ".join(SUMMARIZE_SYSTEM.split()).casefold()
        for text in (gate_text, summarize_text):
            self.assertIn("current turn's visible user input", text)
            self.assertIn("final assistant reply", text)
            self.assertIn("assistant report", text)
            self.assertIn("raw tool results", text)
            self.assertIn("non-conversation payloads", text)
        gate = gate_prompt([{"event_key": "assistant-event", "role": "assistant", "content": "confirmed"}])
        self.assertIn("Turn event metadata", gate)
        self.assertNotIn("confirmed", gate)
        self.assertIn("Relevant existing memleaf/native memories", gate)
        self.assertNotIn("Candidate decomposition check", gate)
        summary = summarize_prompt(
            {"candidate_id": "c", "memory": "confirmed", "type": "fact", "scopes": ["global"], "scope_source": "model"},
            [{"event_key": "assistant-event", "role": "assistant", "content": "confirmed", "evidence_origin": "assistant_report", "unit_id": "assistant-unit"}],
        )
        self.assertIn("Evidence (the only conversation content visible to this call):", summary)
        self.assertNotIn("Final evidence re-check", summary)
    def test_gate_contract_requires_atomic_topics_and_candidate_scoped_bindings(self):
        gate_text = " ".join(GATE_SYSTEM.split()).casefold()
        for phrase in (
            "candidate count follows the independent future uses",
            "separate items that can be completed, tracked, or updated independently",
            "keep shared coordination details with the deliverable they govern",
            "candidate semantic completeness is mandatory",
            "a coverage row for a unit cited by several candidates must list every such candidate_id",
            "use whole_unit only when the complete unit supports that one candidate topic",
            "a negative, completed, hypothetical, or third-party clause limits only the candidate",
            "do not replace independently trackable requested deliverables with only their umbrella coordination request",
        ):
            self.assertIn(phrase.casefold(), gate_text)
        prompt = gate_prompt([{"event_key": "assistant-event", "role": "assistant", "content": "A and B"}])
        self.assertIn("Turn event metadata", prompt)
        self.assertNotIn("A and B", prompt)
        self.assertNotIn("Atomicity test", prompt)
    def test_automatic_summary_and_semantic_review_keep_one_topic(self):
        summary_text = " ".join(SUMMARIZE_SYSTEM.split()).casefold()
        self.assertIn("candidate atomicity is decided at the gate", summary_text)
        self.assertIn('return exactly {"decision":"no_change"}', summary_text)
        self.assertIn("do not add sibling deliverables", summary_text)
        for system in (UPDATE_SEMANTIC_REVIEW_SYSTEM, CREATE_SEMANTIC_REVIEW_SYSTEM):
            review_text = " ".join(system.split()).casefold()
            self.assertIn("this review is for one candidate topic", review_text)
            self.assertIn("a negative or completed clause for one sibling does not suppress or alter another", review_text)
            self.assertIn("do not perform candidate discovery", review_text)
    def test_pure_restatement_is_no_change_but_new_report_remains_eligible(self):
        gate_text = " ".join(GATE_SYSTEM.split()).casefold()
        summarize_text = " ".join(SUMMARIZE_SYSTEM.split()).casefold()
        self.assertIn("a query and a mere restatement of existing memory add no new memory", gate_text)
        self.assertIn("restatements do not create new information by themselves", summarize_text)
        self.assertIn("assistant report", summarize_text)
        for system in (UPDATE_SEMANTIC_REVIEW_SYSTEM, CREATE_SEMANTIC_REVIEW_SYSTEM):
            review_text = " ".join(system.split()).casefold()
            self.assertIn("pure restatements", review_text)
            self.assertIn("no_change", review_text)
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

    def test_semantic_review_source_projection_drops_tools_and_keeps_report(self):
        admitted = [
            dict(source()[0], source_context="The full visible user message adds bounded context."),
            {
                "event_key": "assistant-event",
                "timestamp": "2026-09-08T00:00:00Z",
                "role": "assistant",
                "content": "The visible report confirms the setting.",
                "evidence_origin": "assistant_report",
                "unit_id": "assistant-unit",
                "section_path": [],
            },
            {
                "event_key": "tool-event",
                "timestamp": "2026-09-08T00:00:00Z",
                "role": "tool",
                "content": "RAW_TOOL_SECRET",
                "evidence_origin": "external_observation",
                "unit_id": "tool-unit",
                "source_context": {"should": "drop"},
            },
            {
                "event_key": "assistant-tool-event",
                "timestamp": "2026-09-08T00:00:00Z",
                "role": "assistant",
                "content": "RAW_ASSISTANT_TOOL_SECRET",
                "evidence_origin": "external_observation",
                "unit_id": "assistant-tool-unit",
            },
        ]
        prompt = build_update_review_prompt(target(), admitted, proposal())
        payload = json.loads(prompt.split("\n", 1)[1].split("\n\nThe three top-level values", 1)[0])
        projected = payload["admitted_source"]
        self.assertEqual([item["unit_id"] for item in projected], ["unit-new", "assistant-unit"])
        self.assertEqual(projected[0]["source_context"], "The full visible user message adds bounded context.")
        self.assertNotIn("source_context", projected[1])
        self.assertNotIn("RAW_TOOL_SECRET", prompt)
        self.assertNotIn("RAW_ASSISTANT_TOOL_SECRET", prompt)

    def test_create_prompt_has_only_admitted_source_and_proposal(self):
        proposed = dict(proposal())
        proposed.pop("update_memory_id")
        prompt = build_create_review_prompt(source(), proposed)
        self.assertTrue(prompt.startswith("CREATE_SEMANTIC_REVIEW\n"))
        self.assertIn('"admitted_source"', prompt)
        self.assertIn('"proposed_summary"', prompt)
        self.assertNotIn('"active_target"', prompt)
        self.assertNotIn('"untrusted":"dropped"', prompt)

    def test_source_context_is_string_only_and_bounded_to_admitted_roles(self):
        admitted = [dict(source()[0], source_context={"not": "text"})]
        prompt = build_update_review_prompt(target(), admitted, proposal())
        payload = json.loads(prompt.split("\n", 1)[1].split("\n\nThe three top-level values", 1)[0])
        self.assertNotIn("source_context", payload["admitted_source"][0])

        admitted[0]["source_context"] = "Use this only to interpret the bound span."
        prompt = build_update_review_prompt(target(), admitted, proposal())
        payload = json.loads(prompt.split("\n", 1)[1].split("\n\nThe three top-level values", 1)[0])
        self.assertEqual(
            payload["admitted_source"][0]["source_context"],
            "Use this only to interpret the bound span.",
        )

    def test_create_review_contract_preserves_schema_and_explicit_roles(self):
        review_text = " ".join(CREATE_SEMANTIC_REVIEW_SYSTEM.split())
        self.assertIn("Semantic completeness is as important as non-invention", review_text)
        self.assertIn("meaning-defining", review_text)
        self.assertIn("important numbers/codes with their stated meaning", review_text)
        self.assertIn("project Scope is itself a claimed project affiliation", review_text)
        self.assertIn("complete source-supported revision", review_text)
        self.assertIn("must not carry memory_id or update_memory_id", review_text)
    def test_create_review_accept_uses_the_same_executor_contract(self):
        proposed = dict(proposal())
        proposed.pop("update_memory_id")
        executor = RawExecutor(json.dumps({"decision": "ACCEPT"}))
        result = review_create(
            executor,
            "backend",
            admitted_source=source(),
            proposed_summary=proposed,
            parse_summary=lambda value: value,
        )
        self.assertEqual(result, {"decision": "ACCEPT"})
        self.assertIs(executor.calls[0]["system"], CREATE_SEMANTIC_REVIEW_SYSTEM)
        self.assertEqual(executor.calls[0]["purpose"], "summarize")

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

    def test_final_create_is_reviewed_and_revise_keeps_request_accounting(self):
        turn = InboxTurn(
            "src",
            "session",
            "create-turn",
            1,
            (
                InboxEvent(
                    "src",
                    "session",
                    "create-turn",
                    1,
                    "user",
                    "create-event",
                    "The invoice is due after the scan.",
                    timestamp="2026-09-08T00:00:00Z",
                ),
            ),
        )
        unit = EvidenceUnit(
            "create-unit",
            "create-event",
            "user_assertion",
            "The invoice is due after the scan.",
            source_role="user",
        )
        candidate = {
            "candidate_id": "create-candidate",
            "memory": "The invoice is due after the scan.",
            "evidence_event_ids": ["create-event"],
            "evidence_unit_ids": ["create-unit"],
            "_evidence_bindings": [{
                "unit_id": "create-unit",
                "quote": "The invoice is due after the scan.",
                "role": "assertion",
            }],
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
        }
        original_summary = {
            "title": "Invoice fact",
            "body": "The invoice is due after the scan, and it was issued on 2026-09-01.",
            "tags": [],
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
            "sources": [{"event_key": "create-event"}],
            "scope_operations": [{
                "op": "upsert",
                "scope": "project:invoice",
                "parent": "global",
                "aliases": [],
            }],
            "shadow_native_ids": ["native-invoice"],
        }
        revised_summary = dict(
            original_summary,
            body="The invoice is due after the scan.",
        )
        revised_summary.pop("scope_operations")
        revised_summary.pop("shadow_native_ids")
        request = {
            "summary": original_summary,
            "turn": turn,
            "candidate_id": "create-candidate",
            "memory_id": "mem-created",
            "evidence_unit_ids": ["create-unit"],
        }
        executor = RawExecutor(json.dumps({"decision": "REVISE", "summary": revised_summary}))
        result = UpdateCoordinator(
            executor,
            TurnAudit(),
            lambda memory_id: None,
        ).resolve(
            [request],
            candidates={"create-candidate": candidate},
            evidence_units=[unit],
            events=[{"event_key": "create-event", "timestamp": "2026-09-08T00:00:00Z"}],
            backend="backend",
            scope_registry={},
            validation_scope_registry={},
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["summary"]["body"], revised_summary["body"])
        self.assertNotIn("update_memory_id", result[0]["summary"])
        self.assertEqual(result[0]["memory_id"], "mem-created")
        self.assertEqual(result[0]["candidate_id"], "create-candidate")
        self.assertEqual(result[0]["evidence_unit_ids"], ["create-unit"])
        self.assertEqual(
            result[0]["summary"]["scope_operations"],
            original_summary["scope_operations"],
        )
        self.assertEqual(
            result[0]["summary"]["shadow_native_ids"],
            original_summary["shadow_native_ids"],
        )
        self.assertNotIn("scope_operations", executor.calls[0]["prompt"])
        self.assertNotIn("shadow_native_ids", executor.calls[0]["prompt"])
        self.assertTrue(executor.calls[0]["prompt"].startswith("CREATE_SEMANTIC_REVIEW\n"))

    def test_create_revise_target_or_authorization_expansion_defers(self):
        turn = InboxTurn("src", "session", "create-boundary", 1, ())
        unit = EvidenceUnit("create-unit", "create-event", "user_assertion", "A fact.", source_role="user")
        candidate = {
            "candidate_id": "create-candidate",
            "memory": "A fact.",
            "evidence_event_ids": ["create-event"],
            "evidence_unit_ids": ["create-unit"],
            "_evidence_bindings": [{"unit_id": "create-unit", "quote": "A fact.", "role": "assertion"}],
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
        }
        base = {
            "title": "Fact",
            "body": "A fact.",
            "tags": [],
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
            "sources": [{"event_key": "create-event"}],
        }
        request = {
            "summary": base,
            "turn": turn,
            "candidate_id": "create-candidate",
            "memory_id": "mem-created",
            "evidence_unit_ids": ["create-unit"],
        }

        def run(summary):
            audit = TurnAudit()
            turn_ref = (turn.source, turn.session_id, turn.turn_key)
            audit._deferred_by_turn[turn_ref] = []
            executor = RawExecutor(json.dumps({"decision": "REVISE", "summary": summary}))
            result = UpdateCoordinator(executor, audit, lambda memory_id: None).resolve(
                [request],
                candidates={"create-candidate": candidate},
                evidence_units=[unit],
                events=[{"event_key": "create-event", "timestamp": "2026-09-08T00:00:00Z"}],
                backend="backend",
                scope_registry={},
                validation_scope_registry={},
            )
            return result, audit._dispositions_by_turn[turn_ref]

        cases = {
            "target": dict(base, update_memory_id="mem-other"),
            "native": dict(base, shadow_native_ids=["native-1"]),
            "scope": dict(base, scope_operations=[{
                "op": "upsert",
                "scope": "project:new",
                "parent": "global",
                "aliases": [],
            }]),
        }
        for name, summary in cases.items():
            with self.subTest(name=name):
                result, rows = run(summary)
                self.assertEqual(result, [])
                self.assertEqual(rows[0]["disposition"], "DEFERRED")
                self.assertEqual(rows[0]["reason"], "semantic_review_failed")

    def test_explicit_create_skips_automatic_review_call(self):
        class UnexpectedExecutor:
            def _complete_json_stage(self, *args, **kwargs):
                raise AssertionError("explicit create must not use automatic review")

        turn = InboxTurn("src", "session", "explicit-create", 1, ())
        request = {
            "summary": {
                "title": "Explicit",
                "body": "User supplied.",
                "tags": [],
                "type": "fact",
                "scopes": ["global"],
                "scope_source": "user",
                "sources": [{"event_key": "event"}],
            },
            "turn": turn,
            "candidate_id": "explicit-candidate",
            "memory_id": "mem-explicit",
            "explicit_remember": True,
            "evidence_unit_ids": [],
        }
        result = UpdateCoordinator(UnexpectedExecutor(), TurnAudit(), lambda memory_id: None).resolve(
            [request],
            candidates={},
            evidence_units=[],
            events=[],
            backend="backend",
            scope_registry={},
            validation_scope_registry={},
        )
        self.assertEqual(result, [request])

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
