from __future__ import annotations

import json
import unittest
from contextlib import nullcontext
from types import SimpleNamespace

from memleaf.inbox import InboxEvent, InboxTurn
from memleaf.single_pass_memory_planner import SinglePassMemoryPlanner
from memleaf.single_pass_plan import PROTOCOL_VERSION
from memleaf.turn_audit import TurnAudit
from memleaf.validation import ModelOutputError


TURN_KEY = "9" * 64
SAFE_BACKEND = SimpleNamespace(single_pass_safe=True)
PROJECT_SCOPE = "project:Alpha"


def make_turn() -> InboxTurn:
    events = (
        InboxEvent(
            "hermes",
            "scope-provenance",
            TURN_KEY,
            1,
            "user",
            "a" * 64,
            "Keep the current database setting.",
            turn_id="u",
        ),
        InboxEvent(
            "hermes",
            "scope-provenance",
            TURN_KEY,
            1,
            "assistant",
            "b" * 64,
            "Acknowledged.",
            turn_id="a",
        ),
    )
    return InboxTurn("hermes", "scope-provenance", TURN_KEY, 1, events)


def prompt_payload(prompt: str) -> dict:
    return json.loads(prompt.split("B3_INPUT\n", 1)[1].split("\nReturn", 1)[0])


def create_response(prompt: str, *, scope_source: str | None = None) -> dict:
    payload = prompt_payload(prompt)
    user = next(row for row in payload["current_evidence"] if row["role"] == "user")
    assistant = next(row for row in payload["current_evidence"] if row["role"] == "assistant")
    item = {
        "candidate_id": "c1",
        "decision": "CREATE",
        "type": "fact",
        "scopes": [PROJECT_SCOPE],
        "evidence": [{
            "unit_id": user["unit_id"],
            "whole_unit": True,
            "role": "assertion",
        }],
        "memory": {
            "title": "Database setting",
            "body": "Keep the current database setting.",
        },
    }
    if scope_source is not None:
        item["scope_source"] = scope_source
    return {
        "protocol_version": PROTOCOL_VERSION,
        "items": [item],
        "no_memory": [{
            "unit_id": assistant["unit_id"],
            "reason": "assistant_restatement",
        }],
    }


class Vault:
    processed_state_path = None

    def config(self):
        return {
            "scopes": {PROJECT_SCOPE: {"aliases": []}},
            "capture": {"tool_evidence_mode": "off"},
        }

    def lock(self):
        return nullcontext()


class Service:
    def __init__(self):
        self.vault = Vault()


class Inputs:
    def __init__(self, scope_background=()):
        self.scope_background = list(scope_background)
        self._planned_related = []

    def _single_pass_related(self, *args, **kwargs):
        return [], list(self.scope_background), [], None, True

    def _scope_registry_projection(self):
        return [{
            "scope": PROJECT_SCOPE,
            "aliases": [],
            "parent": None,
            "children": [],
        }]

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


def planner(builder, *, scope_background=()):
    audit = TurnAudit()
    audit._planned_related = []
    model = DynamicModel(builder)
    return SinglePassMemoryPlanner(Service(), audit, Inputs(scope_background), model), model


class B3ScopeProvenanceTests(unittest.TestCase):
    def test_model_cannot_spoof_session_context_for_ungrounded_project(self):
        instance, model = planner(
            lambda prompt: create_response(prompt, scope_source="session_context")
        )
        with self.assertRaises(ModelOutputError) as raised:
            instance._collect_turn_outputs(SAFE_BACKEND, make_turn(), {})
        self.assertEqual(raised.exception.validation_detail, "scope_not_grounded")
        self.assertEqual(len(model.calls), 1)

    def test_session_scope_is_core_derived_and_model_field_is_not_required(self):
        instance, model = planner(
            lambda prompt: create_response(prompt),
            scope_background=[PROJECT_SCOPE],
        )
        requests, scopes = instance._collect_turn_outputs(SAFE_BACKEND, make_turn(), {})
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["summary"]["scope_source"], "session_context")
        self.assertEqual(requests[0]["summary"]["scopes"], [PROJECT_SCOPE])
        self.assertIn(PROJECT_SCOPE, scopes)

    def test_explicit_scope_is_core_derived_as_user_even_with_legacy_model_field(self):
        instance, _ = planner(
            lambda prompt: create_response(prompt, scope_source="model")
        )
        requests, _ = instance._collect_turn_outputs(
            SAFE_BACKEND,
            make_turn(),
            {},
            scope=[PROJECT_SCOPE],
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["summary"]["scope_source"], "user")
        self.assertEqual(requests[0]["summary"]["scopes"], [PROJECT_SCOPE])


if __name__ == "__main__":
    unittest.main()
