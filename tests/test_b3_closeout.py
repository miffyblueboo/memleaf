from __future__ import annotations

import json
import unittest
from contextlib import nullcontext
from types import SimpleNamespace

from memleaf.admission import EvidenceUnit
from memleaf.inbox import InboxEvent, InboxTurn
from memleaf.single_pass_memory_planner import SinglePassMemoryPlanner
from memleaf.single_pass_plan import PROTOCOL_VERSION, build_single_pass_prompt, parse_single_pass_output
from memleaf.turn_audit import TurnAudit
from memleaf.validation import ModelOutputError


TURN_KEY = "d" * 64
SAFE_BACKEND = SimpleNamespace(single_pass_safe=True)


def unit(unit_id: str, text: str) -> EvidenceUnit:
    return EvidenceUnit(
        unit_id=unit_id,
        event_key=f"event-{unit_id}",
        origin="user_assertion",
        text=text,
        source_role="user",
        start=0,
        end=len(text),
    )


def local(memory_id: str) -> dict:
    return {
        "memory_id": memory_id,
        "title": "Existing title",
        "body": "Existing body.",
        "type": "fact",
        "scopes": ["global"],
    }


def claim(unit_id: str, quote: str) -> dict:
    return {"unit_id": unit_id, "quote": quote, "role": "assertion"}


def passthrough_validator(candidate_id, decision, target, target_record, proposed, evidence, context):
    result = dict(proposed)
    if target_record is not None:
        result.setdefault("title", target_record["title"])
    return result


class Vault:
    processed_state_path = None

    def config(self):
        return {"scopes": {}, "capture": {"tool_evidence_mode": "off"}}

    def lock(self):
        return nullcontext()


class Service:
    def __init__(self):
        self.vault = Vault()


class Inputs:
    def _single_pass_related(self, *args, **kwargs):
        return [], [], [], None, True

    def _scope_registry_projection(self):
        return []

    def _conversation_title(self, turn):
        return "Conversation"

    def _active_memory_by_id(self, memory_id):
        return None


class DynamicModel:
    def __init__(self, builder):
        self.builder = builder
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
        max_attempts=None,
    ):
        self.calls.append((purpose, prompt, system, max_attempts))
        return parser(json.dumps(self.builder(prompt), ensure_ascii=False))


def prompt_payload(prompt: str) -> dict:
    return json.loads(prompt.split("B3_INPUT\n", 1)[1].split("\nReturn", 1)[0])


class B3CloseoutTests(unittest.TestCase):
    def test_prompt_drops_core_only_event_key_and_redundant_context(self):
        prompt, _, _ = build_single_pass_prompt(
            evidence_units=[unit("u1", "Alpha uses PostgreSQL.")],
            native_memories=[{
                "native_id": "native-1",
                "source": "native",
                "title": "Alpha",
                "body": "Same native body.",
                "content": "Same native body.",
                "scopes": ["global"],
            }],
            scope_registry=[{
                "scope": "project:Alpha",
                "aliases": ["alpha"],
                "parent": "portfolio:apps",
                "children": ["project:Alpha-child"],
            }],
        )
        payload = prompt_payload(prompt)
        evidence = payload["current_evidence"][0]
        self.assertNotIn("event_key", evidence)
        self.assertEqual(evidence["content"], "Alpha uses PostgreSQL.")

        native = payload["native_memory_catalog"][0]
        self.assertEqual(native["body"], "Same native body.")
        self.assertNotIn("content", native)
        self.assertEqual(prompt.count("Same native body."), 1)

        scope = payload["scope_registry"][0]
        self.assertEqual(set(scope), {"scope", "aliases", "parent"})
        self.assertNotIn("children", scope)

    def test_update_may_omit_unchanged_title(self):
        evidence = [unit("u1", "Alpha now uses PostgreSQL.")]
        raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [{
                "candidate_id": "c1",
                "decision": "UPDATE",
                "target_memory_id": "m-db",
                "evidence": [claim("u1", "Alpha now uses PostgreSQL.")],
                "memory": {"body": "Alpha now uses PostgreSQL."},
            }],
            "no_memory": [],
        })
        result = parse_single_pass_output(
            raw,
            evidence_units=evidence,
            local_memories=[local("m-db")],
            lookup_complete=True,
            validate_memory=passthrough_validator,
        )
        self.assertEqual(result["items"][0]["memory"]["title"], "Existing title")

    def test_update_scope_correction_does_not_require_scope_source(self):
        evidence = [unit("u1", "This belongs to project:New, not project:Old.")]
        raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [{
                "candidate_id": "c1",
                "decision": "UPDATE",
                "target_memory_id": "m-old",
                "scopes": ["project:New"],
                "evidence": [claim("u1", "This belongs to project:New, not project:Old.")],
                "memory": {"body": "This belongs to project:New."},
            }],
            "no_memory": [],
        })
        captured_context = {}

        def validator(candidate_id, decision, target, target_record, proposed, claims, context):
            captured_context.update(context)
            return passthrough_validator(
                candidate_id, decision, target, target_record, proposed, claims, context
            )

        target = local("m-old")
        target["scopes"] = ["project:Old"]
        result = parse_single_pass_output(
            raw,
            evidence_units=evidence,
            local_memories=[target],
            lookup_complete=True,
            validate_memory=validator,
        )
        self.assertEqual(result["items"][0]["target_memory_id"], "m-old")
        self.assertEqual(captured_context["scopes"], ["project:New"])
        self.assertNotIn("scope_source", captured_context)

    def test_candidate_ids_are_unique_case_insensitively(self):
        evidence = [unit("u1", "A."), unit("u2", "B.")]
        raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [
                {
                    "candidate_id": "c1",
                    "decision": "DEFERRED",
                    "reason": "maintenance_uncertain",
                    "evidence": [claim("u1", "A.")],
                },
                {
                    "candidate_id": "C1",
                    "decision": "DEFERRED",
                    "reason": "maintenance_uncertain",
                    "evidence": [claim("u2", "B.")],
                },
            ],
            "no_memory": [],
        })
        with self.assertRaises(ModelOutputError):
            parse_single_pass_output(
                raw,
                evidence_units=evidence,
                local_memories=[],
                lookup_complete=True,
                validate_memory=passthrough_validator,
            )

    def test_relative_todo_due_date_is_grounded_by_core(self):
        user_text = "请在明天完成 Alpha 迁移。"
        assistant_text = "收到。"
        turn = InboxTurn(
            "hermes",
            "s",
            TURN_KEY,
            1,
            (
                InboxEvent(
                    "hermes",
                    "s",
                    TURN_KEY,
                    1,
                    "user",
                    "e" * 64,
                    user_text,
                    turn_id="u",
                    timestamp="2026-09-10T12:00:00Z",
                ),
                InboxEvent(
                    "hermes",
                    "s",
                    TURN_KEY,
                    1,
                    "assistant",
                    "f" * 64,
                    assistant_text,
                    turn_id="a",
                    timestamp="2026-09-10T12:00:01Z",
                ),
            ),
        )

        def response(prompt: str) -> dict:
            payload = prompt_payload(prompt)
            user = next(row for row in payload["current_evidence"] if row["role"] == "user")
            assistant = next(row for row in payload["current_evidence"] if row["role"] == "assistant")
            return {
                "protocol_version": PROTOCOL_VERSION,
                "items": [{
                    "candidate_id": "todo-1",
                    "decision": "CREATE",
                    "type": "todo",
                    "scopes": ["global"],
                    "evidence": [{
                        "unit_id": user["unit_id"],
                        "whole_unit": True,
                        "role": "assertion",
                    }],
                    "memory": {
                        "title": "完成 Alpha 迁移",
                        "body": "明天完成 Alpha 迁移。",
                        "status": "active",
                        "due_date": "明天",
                    },
                }],
                "no_memory": [{
                    "unit_id": assistant["unit_id"],
                    "reason": "assistant_restatement",
                }],
            }

        model = DynamicModel(response)
        audit = TurnAudit()
        audit._planned_related = []
        planner = SinglePassMemoryPlanner(Service(), audit, Inputs(), model)

        requests, _ = planner._collect_turn_outputs(SAFE_BACKEND, turn, {})
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0][0], "single_pass")
        self.assertEqual(model.calls[0][3], 2)
        self.assertEqual(requests[0]["summary"]["due_date"], "2026-09-11")
        self.assertNotIn("明天", requests[0]["summary"]["body"])


if __name__ == "__main__":
    unittest.main()
