from __future__ import annotations

import json
import unittest
from contextlib import nullcontext
from types import SimpleNamespace

from memleaf.inbox import InboxEvent, InboxTurn
from memleaf.models import Memory
from memleaf.single_pass_memory_planner import SinglePassMemoryPlanner
from memleaf.turn_audit import TurnAudit
from memleaf.turn_plan import TurnPlan, revision_digest
from memleaf.validation import ModelOutputError
from memleaf.single_pass_plan import PROTOCOL_VERSION


TURN_KEY = "a" * 64
SAFE_BACKEND = SimpleNamespace(single_pass_safe=True)


def turn(user: str, assistant: str = "Noted.") -> InboxTurn:
    events = (
        InboxEvent("hermes", "s", TURN_KEY, 1, "user", "b" * 64, user, turn_id="u"),
        InboxEvent("hermes", "s", TURN_KEY, 1, "assistant", "c" * 64, assistant, turn_id="a"),
    )
    return InboxTurn("hermes", "s", TURN_KEY, 1, events)


def active(memory_id: str, body: str, *, scope="global", memory_type="fact") -> Memory:
    return Memory(
        memory_id=memory_id,
        title="Existing",
        body=body,
        tags=[],
        type=memory_type,
        scopes=[scope],
        aliases=[],
        keywords=[],
        scope_source="model",
        sources=[],
        created="2026-09-10T00:00:00Z",
        updated="2026-09-10T00:00:00Z",
    )


class Vault:
    processed_state_path = None
    def __init__(self, scopes=None): self.scopes = scopes or {}
    def config(self): return {"scopes": self.scopes, "capture": {"tool_evidence_mode": "off"}}
    def lock(self): return nullcontext()


class Service:
    def __init__(self, scopes=None): self.vault = Vault(scopes)


class Inputs:
    def __init__(self, related=(), target=None, *, lookup_complete=True, scope_background=None):
        self.related = [dict(item) for item in related]
        self.target = target
        self.lookup_complete = lookup_complete
        self.scope_background = scope_background or []
        self._planned_related = []

    def _single_pass_related(self, *args, **kwargs):
        native_refs = [
            {"source_id": row.get("native_source_id", "native"), "native_id": row["native_id"]}
            for row in self.related if row.get("native") is True and isinstance(row.get("native_id"), str)
        ]
        return list(self.related), self.scope_background, native_refs, None, self.lookup_complete

    def _scope_registry_projection(self): return []
    def _conversation_title(self, turn): return "Conversation"
    def _active_memory_by_id(self, memory_id):
        if self.target is not None and isinstance(memory_id, str) and memory_id.casefold() == self.target.memory_id.casefold():
            return self.target
        return None
    def _scope_correction_plan(self, candidate, turn, config):
        if self.target is None or candidate.get("update_memory_id") != self.target.memory_id:
            return None
        scopes = candidate.get("scopes", [])
        if scopes == list(self.target.scopes):
            return None
        return {
            "target_memory_id": self.target.memory_id,
            "old_scope": self.target.scopes[0],
            "new_scope": scopes[0],
            "survivor_memory_id": None,
            "ambiguous": False,
            "unresolved": False,
        }


class Model:
    def __init__(self, response): self.response = response; self.calls = []
    def _complete_json_stage(self, backend, prompt, *, system, purpose, parser, diagnostic_context=None, max_attempts=None):
        self.calls.append((purpose, prompt, system))
        return parser(json.dumps(self.response, ensure_ascii=False))


def item_claim(prompt: str, text: str) -> dict:
    payload = json.loads(prompt.split("B3_INPUT\n", 1)[1].split("\nReturn", 1)[0])
    unit = next(row for row in payload["current_evidence"] if text in row["content"])
    return {"unit_id": unit["unit_id"], "quote": text, "role": "assertion"}


class DynamicModel:
    def __init__(self, build): self.build=build; self.calls=[]
    def _complete_json_stage(self, backend, prompt, *, system, purpose, parser, diagnostic_context=None, max_attempts=None):
        self.calls.append((purpose,prompt,system))
        return parser(json.dumps(self.build(prompt), ensure_ascii=False))


class B3SinglePassMemoryPlannerTests(unittest.TestCase):
    def planner(self, response_builder, *, related=(), target=None, lookup_complete=True, scope_background=None):
        audit = TurnAudit()
        audit._planned_related = []
        model = DynamicModel(response_builder)
        planner = SinglePassMemoryPlanner(
            Service(), audit, Inputs(related, target, lookup_complete=lookup_complete, scope_background=scope_background), model
        )
        return planner, audit, model

    def test_create_is_one_model_call_and_writer_compatible_request(self):
        def response(prompt):
            return {
                "protocol_version": PROTOCOL_VERSION,
                "items": [{
                    "candidate_id": "c1",
                    "decision": "CREATE",
                    "type": "fact",
                    "scopes": ["global"],
                    "scope_source": "model",
                    "evidence": [item_claim(prompt, "Alpha uses PostgreSQL.")],
                    "memory": {"title": "Alpha database", "body": "Alpha uses PostgreSQL."},
                }],
                "no_memory": [
                    {"unit_id": json.loads(prompt.split("B3_INPUT\n",1)[1].split("\nReturn",1)[0])["current_evidence"][1]["unit_id"], "reason": "assistant_restatement"}
                ],
            }
        planner, audit, model = self.planner(response)
        requests, scopes = planner._collect_turn_outputs(SAFE_BACKEND, turn("Alpha uses PostgreSQL."), {})
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0][0], "single_pass")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["summary"]["body"], "Alpha uses PostgreSQL.")
        self.assertEqual(requests[0]["summary"]["tags"], [])
        self.assertTrue(requests[0]["evidence_unit_ids"])
        self.assertEqual(len(TurnPlan.from_requests(requests).candidates), 1)
        self.assertIn("global", scopes)
        ref = ("hermes", "s", TURN_KEY)
        self.assertEqual(audit._dispositions_by_turn[ref][0]["disposition"], "CREATE")

    def test_update_is_one_call_and_freezes_revision(self):
        target = active("m-db", "Alpha uses MySQL.")
        related = [target.to_dict()]
        def response(prompt):
            payload = json.loads(prompt.split("B3_INPUT\n",1)[1].split("\nReturn",1)[0])
            assistant_uid = payload["current_evidence"][1]["unit_id"]
            return {
                "protocol_version": PROTOCOL_VERSION,
                "items": [{
                    "candidate_id": "c1",
                    "decision": "UPDATE",
                    "target_memory_id": "M-DB",
                    "evidence": [item_claim(prompt, "Alpha now uses PostgreSQL instead of MySQL.")],
                    "memory": {"title": "Existing", "body": "Alpha now uses PostgreSQL instead of MySQL."},
                }],
                "no_memory": [{"unit_id": assistant_uid, "reason": "assistant_restatement"}],
            }
        planner, audit, model = self.planner(response, related=related, target=target)
        requests, _ = planner._collect_turn_outputs(SAFE_BACKEND, turn("Alpha now uses PostgreSQL instead of MySQL."), {})
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(requests[0]["summary"]["update_memory_id"], "m-db")
        self.assertEqual(requests[0]["expected_revision"], revision_digest(target))
        self.assertEqual(requests[0]["summary"]["type"], "fact")
        self.assertEqual(requests[0]["summary"]["scopes"], ["global"])

    def test_update_may_apply_core_authorized_scope_correction_in_same_call(self):
        target = active("m-old", "双人复核", scope="project:Old", memory_type="project")
        related = [target.to_dict()]
        def response(prompt):
            payload = json.loads(prompt.split("B3_INPUT\n",1)[1].split("\nReturn",1)[0])
            assistant_uid = next(row["unit_id"] for row in payload["current_evidence"] if row["role"] == "assistant")
            return {
                "protocol_version": PROTOCOL_VERSION,
                "items": [{
                    "candidate_id": "c1",
                    "decision": "UPDATE",
                    "target_memory_id": "m-old",
                    "scopes": ["project:New"],
                    "scope_source": "model",
                    "evidence": [{"unit_id": row["unit_id"], "whole_unit": True, "role": "assertion"} for row in payload["current_evidence"] if row["role"] == "user"],
                    "memory": {"title": "流程", "body": "New流程要求仍是双人复核。"},
                }],
                "no_memory": [{"unit_id": assistant_uid, "reason": "assistant_restatement"}],
            }
        planner, _, model = self.planner(response, related=related, target=target)
        requests, scopes = planner._collect_turn_outputs(
            SAFE_BACKEND,
            turn("New流程要求仍是双人复核，之前归错到Old。"),
            {},
            scope=["project:New"],
        )
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(requests[0]["summary"]["scopes"], ["project:New"])
        self.assertEqual(requests[0]["summary"]["update_memory_id"], "m-old")
        self.assertIn("project:New", scopes)

    def test_create_keeps_native_shadow_compatibility_without_scope_maintenance(self):
        native_id = "native-1"
        related = [{
            "native": True,
            "native_id": native_id,
            "native_source_id": "source-1",
            "source": "native",
            "content": "legacy state",
            "title": "Legacy",
            "body": "legacy state",
            "scopes": ["global"],
        }]
        def response(prompt):
            payload = json.loads(prompt.split("B3_INPUT\n",1)[1].split("\nReturn",1)[0])
            assistant_uid = payload["current_evidence"][1]["unit_id"]
            return {
                "protocol_version": PROTOCOL_VERSION,
                "items": [{
                    "candidate_id": "c1",
                    "decision": "CREATE",
                    "type": "fact",
                    "scopes": ["global"],
                    "scope_source": "model",
                    "evidence": [item_claim(prompt, "legacy state is replaced")],
                    "memory": {
                        "title": "Current state",
                        "body": "legacy state is replaced",
                        "shadow_native_ids": [native_id],
                    },
                }],
                "no_memory": [{"unit_id": assistant_uid, "reason": "assistant_restatement"}],
            }
        planner, _, model = self.planner(response, related=related)
        requests, _ = planner._collect_turn_outputs(SAFE_BACKEND, turn("legacy state is replaced"), {})
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(requests[0]["summary"]["shadow_native_ids"], [native_id])
        # scope_operations is forbidden in the model-owned B3 memory object,
        # but Core may canonicalize the writer-facing summary with an empty
        # maintenance list after successful validation.
        self.assertEqual(requests[0]["summary"]["scope_operations"], [])
        self.assertEqual(requests[0]["native_refs"], [{"source_id": "source-1", "native_id": native_id}])

    def test_no_change_and_query_make_no_requests(self):
        target = active("m-db", "Alpha uses PostgreSQL.")
        def response(prompt):
            payload = json.loads(prompt.split("B3_INPUT\n",1)[1].split("\nReturn",1)[0])
            user = payload["current_evidence"][0]
            assistant = payload["current_evidence"][1]
            return {
                "protocol_version": PROTOCOL_VERSION,
                "items": [{
                    "candidate_id": "c1",
                    "decision": "NO_CHANGE",
                    "target_memory_id": "m-db",
                    "evidence": [{"unit_id": user["unit_id"], "whole_unit": True, "role": "assertion"}],
                }],
                "no_memory": [{"unit_id": assistant["unit_id"], "reason": "assistant_restatement"}],
            }
        planner, audit, model = self.planner(response, related=[target.to_dict()], target=target)
        requests, _ = planner._collect_turn_outputs(SAFE_BACKEND, turn("Alpha uses PostgreSQL."), {})
        self.assertEqual(requests, [])
        self.assertEqual(len(model.calls), 1)
        ref=("hermes","s",TURN_KEY)
        self.assertEqual(audit._dispositions_by_turn[ref][0]["disposition"], "NO_CHANGE")

    def test_incomplete_lookup_cannot_write(self):
        def response(prompt):
            payload=json.loads(prompt.split("B3_INPUT\n",1)[1].split("\nReturn",1)[0])
            user=payload["current_evidence"][0]; assistant=payload["current_evidence"][1]
            return {
                "protocol_version": PROTOCOL_VERSION,
                "items": [{
                    "candidate_id":"c1","decision":"CREATE","type":"fact","scopes":["global"],"scope_source":"model",
                    "evidence":[{"unit_id":user["unit_id"],"whole_unit":True,"role":"assertion"}],
                    "memory":{"title":"x","body":"x"},
                }],
                "no_memory":[{"unit_id":assistant["unit_id"],"reason":"assistant_restatement"}],
            }
        planner, _, model = self.planner(response, lookup_complete=False)
        with self.assertRaises(ModelOutputError):
            planner._collect_turn_outputs(SAFE_BACKEND, turn("Remember x."), {})
        self.assertEqual(len(model.calls), 1)

    def test_deferred_keeps_turn_retryable_without_request(self):
        def response(prompt):
            payload=json.loads(prompt.split("B3_INPUT\n",1)[1].split("\nReturn",1)[0])
            user=payload["current_evidence"][0]; assistant=payload["current_evidence"][1]
            return {
                "protocol_version":PROTOCOL_VERSION,
                "items":[{
                    "candidate_id":"c1","decision":"DEFERRED","reason":"scope_ambiguous",
                    "evidence":[{"unit_id":user["unit_id"],"whole_unit":True,"role":"assertion"}],
                }],
                "no_memory":[{"unit_id":assistant["unit_id"],"reason":"assistant_restatement"}],
            }
        planner,audit,model=self.planner(response, scope_background=[])
        requests,_=planner._collect_turn_outputs(SAFE_BACKEND,turn("Project ownership is unclear."),{})
        self.assertEqual(requests,[]); self.assertEqual(len(model.calls),1)
        ref=("hermes","s",TURN_KEY)
        self.assertEqual(audit._dispositions_by_turn[ref][0]["disposition"],"DEFERRED")
        self.assertTrue(audit._deferred_by_turn[ref])
        self.assertTrue(any(row["decision"]=="DEFERRED" for row in audit._evidence_by_turn[ref]))


if __name__ == "__main__":
    unittest.main()
