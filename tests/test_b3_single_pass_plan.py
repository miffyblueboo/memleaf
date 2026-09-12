from __future__ import annotations

import json
import unittest

from memleaf.admission import EvidenceUnit
from memleaf.single_pass_plan import (
    PROTOCOL_VERSION,
    SINGLE_PASS_SYSTEM,
    build_single_pass_prompt,
    parse_single_pass_output,
    run_single_pass_stage,
)
from memleaf.validation import ModelOutputError


def unit(unit_id: str, text: str, *, event_key: str | None = None) -> EvidenceUnit:
    return EvidenceUnit(
        unit_id=unit_id,
        event_key=event_key or f"event-{unit_id}",
        origin="user_assertion",
        text=text,
        source_role="user",
        start=0,
        end=len(text),
    )


def local(memory_id: str, *, body: str = "Old state.", memory_type: str = "fact") -> dict:
    return {
        "memory_id": memory_id,
        "title": f"Old {memory_id}",
        "body": body,
        "type": memory_type,
        "scopes": ["global"],
    }


def memory(title: str = "Title", body: str = "Body") -> dict:
    return {"title": title, "body": body, "tags": []}


def claim(unit_id: str, quote: str) -> dict:
    return {"unit_id": unit_id, "quote": quote, "role": "assertion"}


class FakeExecutor:
    def __init__(self, raw: str):
        self.raw = raw
        self.calls: list[dict] = []

    def _complete_json_stage(self, backend, prompt, *, system, purpose, parser, diagnostic_context=None, max_attempts=None):
        self.calls.append({
            "backend": backend,
            "prompt": prompt,
            "system": system,
            "purpose": purpose,
            "diagnostic_context": diagnostic_context,
        })
        return parser(self.raw)


def validator(candidate_id, decision, target, target_record, proposed, evidence, context):
    if not isinstance(proposed.get("title"), str) or not isinstance(proposed.get("body"), str):
        raise ModelOutputError("invalid test memory", validation_detail="candidate_shape")
    result = dict(proposed)
    result["validated_candidate_id"] = candidate_id
    result["validated_decision"] = decision
    result["validated_target"] = target
    result["validated_target_type"] = target_record.get("type") if target_record else None
    result["validated_evidence_count"] = len(evidence)
    return result


class SinglePassPlanTests(unittest.TestCase):
    def test_prompt_contains_each_source_and_old_memory_once(self):
        evidence = [unit("u1", "Orion uses PostgreSQL now."), unit("u2", "Keep JDK 17.")]
        old = [local("m-db", body="Orion uses MySQL."), local("m-jdk", body="Orion uses JDK 17.")]
        prompt, _, _ = build_single_pass_prompt(
            evidence_units=evidence,
            related_memories=old,
            native_memories=[{"native_id": "n1", "content": "Native comparison."}],
            scope_background=["project:Orion"],
            scope_registry=[{"scope": "project:Orion"}],
        )
        self.assertEqual(prompt.count("Orion uses PostgreSQL now."), 1)
        self.assertEqual(prompt.count("Orion uses MySQL."), 1)
        self.assertEqual(prompt.count("Native comparison."), 1)
        self.assertNotIn("Turn event metadata", prompt)
        self.assertIn(PROTOCOL_VERSION, prompt)

    def test_create_update_nochange_and_nomemory_parse_in_one_envelope(self):
        evidence = [
            unit("u1", "Use PostgreSQL for Alpha."),
            unit("u2", "JDK remains 17."),
            unit("u3", "What is the weather?"),
        ]
        raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [
                {
                    "candidate_id": "c1",
                    "decision": "CREATE",
                    "type": "fact",
                    "scopes": ["project:Alpha"],
                    "scope_source": "model",
                    "evidence": [claim("u1", "Use PostgreSQL for Alpha.")],
                    "memory": memory("Alpha database", "Alpha uses PostgreSQL."),
                },
                {
                    "candidate_id": "c2",
                    "decision": "NO_CHANGE",
                    "target_memory_id": "M-JDK",
                    "evidence": [claim("u2", "JDK remains 17.")],
                },
            ],
            "no_memory": [{"unit_id": "u3", "reason": "query_only"}],
        })
        result = parse_single_pass_output(
            raw,
            evidence_units=evidence,
            local_memories=[local("m-jdk", body="JDK is 17.")],
            lookup_complete=True,
            validate_memory=validator,
        )
        self.assertEqual([item["decision"] for item in result["items"]], ["CREATE", "NO_CHANGE"])
        self.assertEqual(result["items"][1]["target_memory_id"], "m-jdk")
        self.assertEqual(result["no_memory"], [{"unit_id": "u3", "reason": "query_only"}])
        self.assertEqual(result["items"][0]["memory"]["validated_evidence_count"], 1)

    def test_update_inherits_target_context_via_validator(self):
        evidence = [unit("u1", "Alpha now uses PostgreSQL instead of MySQL.")]
        raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [{
                "candidate_id": "c1",
                "decision": "UPDATE",
                "target_memory_id": "M-DB",
                "evidence": [claim("u1", "Alpha now uses PostgreSQL instead of MySQL.")],
                "memory": memory("Alpha database", "Alpha uses PostgreSQL instead of MySQL."),
            }],
            "no_memory": [],
        })
        result = parse_single_pass_output(
            raw,
            evidence_units=evidence,
            local_memories=[local("m-db", body="Alpha uses MySQL.")],
            lookup_complete=True,
            validate_memory=validator,
        )
        item = result["items"][0]
        self.assertEqual(item["target_memory_id"], "m-db")
        self.assertEqual(item["memory"]["validated_target"], "m-db")
        self.assertEqual(item["memory"]["validated_target_type"], "fact")

    def test_native_memory_never_authorizes_target(self):
        evidence = [unit("u1", "Alpha changed.")]
        raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [{
                "candidate_id": "c1",
                "decision": "UPDATE",
                "target_memory_id": "native-1",
                "evidence": [claim("u1", "Alpha changed.")],
                "memory": memory(),
            }],
            "no_memory": [],
        })
        with self.assertRaises(ModelOutputError):
            parse_single_pass_output(
                raw,
                evidence_units=evidence,
                local_memories=[],
                lookup_complete=True,
                validate_memory=validator,
            )

    def test_incomplete_lookup_forbids_create(self):
        evidence = [unit("u1", "Remember this new fact.")]
        raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [{
                "candidate_id": "c1",
                "decision": "CREATE",
                "type": "fact",
                "scopes": ["global"],
                "scope_source": "model",
                "evidence": [claim("u1", "Remember this new fact.")],
                "memory": memory(),
            }],
            "no_memory": [],
        })
        with self.assertRaises(ModelOutputError):
            parse_single_pass_output(
                raw,
                evidence_units=evidence,
                local_memories=[],
                lookup_complete=False,
                validate_memory=validator,
            )

    def test_incomplete_lookup_forbids_update_and_nochange_too(self):
        evidence = [unit("u1", "Alpha changed.")]
        for decision, extra in (
            ("UPDATE", {"target_memory_id": "m1", "memory": memory()}),
            ("NO_CHANGE", {"target_memory_id": "m1"}),
        ):
            with self.subTest(decision=decision):
                raw = json.dumps({
                    "protocol_version": PROTOCOL_VERSION,
                    "items": [{
                        "candidate_id": "c1",
                        "decision": decision,
                        "evidence": [claim("u1", "Alpha changed.")],
                        **extra,
                    }],
                    "no_memory": [],
                })
                with self.assertRaises(ModelOutputError):
                    parse_single_pass_output(
                        raw,
                        evidence_units=evidence,
                        local_memories=[local("m1")],
                        lookup_complete=False,
                        validate_memory=validator,
                    )

    def test_parser_requires_real_evidence_units_for_authority_validation(self):
        raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [],
            "no_memory": [{"unit_id": "u1", "reason": "no_future_value"}],
        })
        with self.assertRaises(TypeError):
            parse_single_pass_output(
                raw,
                evidence_units=[{"unit_id": "u1", "event_key": "e", "role": "user", "content": "x"}],
                local_memories=[],
                lookup_complete=True,
                validate_memory=validator,
            )

    def test_update_scope_correction_requires_scopes_not_model_provenance(self):
        evidence = [unit("u1", "Move Alpha memory to Beta.")]
        raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [{
                "candidate_id": "c1",
                "decision": "UPDATE",
                "target_memory_id": "m1",
                "scopes": ["project:Beta"],
                "evidence": [claim("u1", "Move Alpha memory to Beta.")],
                "memory": memory(),
            }],
            "no_memory": [],
        })
        parsed = parse_single_pass_output(
            raw,
            evidence_units=evidence,
            local_memories=[local("m1")],
            lookup_complete=True,
            validate_memory=validator,
        )
        self.assertEqual(parsed["items"][0]["target_memory_id"], "m1")

        legacy_provenance_only = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [{
                "candidate_id": "c1",
                "decision": "UPDATE",
                "target_memory_id": "m1",
                "scope_source": "model",
                "evidence": [claim("u1", "Move Alpha memory to Beta.")],
                "memory": memory(),
            }],
            "no_memory": [],
        })
        with self.assertRaises(ModelOutputError):
            parse_single_pass_output(
                legacy_provenance_only,
                evidence_units=evidence,
                local_memories=[local("m1")],
                lookup_complete=True,
                validate_memory=validator,
            )

    def test_memory_keeps_native_shadow_compatibility_but_rejects_scope_operations(self):
        evidence = [unit("u1", "New current state.")]
        compatible = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [{
                "candidate_id": "c1",
                "decision": "CREATE",
                "type": "fact",
                "scopes": ["global"],
                "scope_source": "model",
                "evidence": [claim("u1", "New current state.")],
                "memory": {
                    "title": "Current state",
                    "body": "New current state.",
                    "shadow_native_ids": ["native-1"],
                },
            }],
            "no_memory": [],
        })
        result = parse_single_pass_output(
            compatible,
            evidence_units=evidence,
            local_memories=[],
            lookup_complete=True,
            validate_memory=validator,
        )
        self.assertEqual(result["items"][0]["memory"]["shadow_native_ids"], ["native-1"])

        maintenance_operation = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [{
                "candidate_id": "c1",
                "decision": "CREATE",
                "type": "fact",
                "scopes": ["global"],
                "scope_source": "model",
                "evidence": [claim("u1", "New current state.")],
                "memory": {
                    "title": "Current state",
                    "body": "New current state.",
                    "scope_operations": [],
                },
            }],
            "no_memory": [],
        })
        with self.assertRaises(ModelOutputError) as caught:
            parse_single_pass_output(
                maintenance_operation,
                evidence_units=evidence,
                local_memories=[],
                lookup_complete=True,
                validate_memory=validator,
            )
        self.assertEqual(caught.exception.validation_detail, "unknown_fields")

    def test_evidence_coverage_must_be_complete_and_disjoint(self):
        evidence = [unit("u1", "A."), unit("u2", "B.")]
        missing = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [{
                "candidate_id": "c1",
                "decision": "DEFERRED",
                "reason": "maintenance_uncertain",
                "evidence": [claim("u1", "A.")],
            }],
            "no_memory": [],
        })
        with self.assertRaises(ModelOutputError):
            parse_single_pass_output(
                missing,
                evidence_units=evidence,
                local_memories=[],
                lookup_complete=True,
                validate_memory=validator,
            )
        overlap = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [{
                "candidate_id": "c1",
                "decision": "DEFERRED",
                "reason": "maintenance_uncertain",
                "evidence": [claim("u1", "A.")],
            }],
            "no_memory": [
                {"unit_id": "u1", "reason": "no_future_value"},
                {"unit_id": "u2", "reason": "no_future_value"},
            ],
        })
        with self.assertRaises(ModelOutputError):
            parse_single_pass_output(
                overlap,
                evidence_units=evidence,
                local_memories=[],
                lookup_complete=True,
                validate_memory=validator,
            )

    def test_shared_evidence_can_support_multiple_atomic_items(self):
        evidence = [unit("u1", "Alpha uses PostgreSQL and JDK 17.")]
        raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [
                {
                    "candidate_id": "c1",
                    "decision": "CREATE",
                    "type": "fact",
                    "scopes": ["project:Alpha"],
                    "scope_source": "model",
                    "evidence": [claim("u1", "Alpha uses PostgreSQL")],
                    "memory": memory("DB", "Alpha uses PostgreSQL."),
                },
                {
                    "candidate_id": "c2",
                    "decision": "CREATE",
                    "type": "fact",
                    "scopes": ["project:Alpha"],
                    "scope_source": "model",
                    "evidence": [claim("u1", "JDK 17")],
                    "memory": memory("JDK", "Alpha uses JDK 17."),
                },
            ],
            "no_memory": [],
        })
        result = parse_single_pass_output(
            raw,
            evidence_units=evidence,
            local_memories=[],
            lookup_complete=True,
            validate_memory=validator,
        )
        self.assertEqual(len(result["items"]), 2)

    def test_same_target_cannot_be_used_twice(self):
        evidence = [unit("u1", "A."), unit("u2", "B.")]
        raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [
                {"candidate_id": "c1", "decision": "NO_CHANGE", "target_memory_id": "m1", "evidence": [claim("u1", "A.")]},
                {"candidate_id": "c2", "decision": "NO_CHANGE", "target_memory_id": "M1", "evidence": [claim("u2", "B.")]},
            ],
            "no_memory": [],
        })
        with self.assertRaises(ModelOutputError):
            parse_single_pass_output(
                raw,
                evidence_units=evidence,
                local_memories=[local("m1")],
                lookup_complete=True,
                validate_memory=validator,
            )

    def test_run_stage_is_one_model_call(self):
        evidence = [unit("u1", "No durable fact here.")]
        raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [],
            "no_memory": [{"unit_id": "u1", "reason": "no_future_value"}],
        })
        executor = FakeExecutor(raw)
        result = run_single_pass_stage(
            executor,
            "backend",
            evidence_units=evidence,
            lookup_complete=True,
            validate_memory=validator,
            diagnostic_context={"session_id": "s"},
        )
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(executor.calls[0]["system"], SINGLE_PASS_SYSTEM)
        self.assertEqual(executor.calls[0]["purpose"], "single_pass")
        self.assertEqual(result["items"], [])


if __name__ == "__main__":
    unittest.main()
