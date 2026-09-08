"""Whole-unit evidence bindings keep multiline source provenance exact."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.admission import EvidenceUnit, analyze_turn_evidence, summary_evidence, supporting_units, validate_bindings
from memleaf.index import event_key
from memleaf.validation import ModelOutputError


def _candidate(candidate_id: str, source_event: str, *, duplicate: bool = False,
               worth: bool = True, duplicate_memory_id: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "candidate_id": candidate_id,
        "memory": "The document says the rollout is approved.",
        "evidence_event_ids": [source_event],
        "duplicate": duplicate,
        "worth": worth,
        "type": "fact",
        "scopes": ["global"],
        "scope_source": "model",
    }
    if duplicate_memory_id is not None:
        value["duplicate_memory_id"] = duplicate_memory_id
    return value


def _whole_binding(candidate_id: str, unit_id: str, *, role: str = "source_excerpt",
                   **extra: object) -> list[dict[str, object]]:
    claim: dict[str, object] = {
        "unit_id": unit_id,
        "whole_unit": True,
        "role": role,
    }
    claim.update(extra)
    return [{"candidate_id": candidate_id, "claims": [claim]}]


class WholeUnitBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = "Document: rollout checklist\nStatus: approved\nOwner: Team Delta"

    def assistant_units(self, content: str | None = None):
        return analyze_turn_evidence([
            {
                "role": "assistant",
                "event_key": "assistant-event",
                "content": content or self.document,
            },
        ])

    def test_multiline_assistant_whole_unit_canonicalizes_to_exact_full_span(self) -> None:
        units = self.assistant_units()
        unit = units[0]
        candidate = _candidate("c", unit.event_key)

        checked = validate_bindings(
            _whole_binding("c", unit.unit_id),
            units,
            [candidate],
        )

        self.assertEqual(
            checked["c"],
            [{
                "unit_id": unit.unit_id,
                "quote": unit.text,
                "start": 0,
                "end": len(unit.text),
                "role": "source_excerpt",
            }],
        )
        self.assertNotIn("whole_unit", checked["c"][0])
        self.assertEqual(
            validate_bindings([{"candidate_id": "c", "claims": checked["c"]}], units, [candidate]),
            checked,
        )
        candidate["_evidence_bindings"] = checked["c"]
        supported = supporting_units(candidate, units)
        self.assertEqual(len(supported), 1)
        self.assertEqual(supported[0].text, self.document)
        self.assertEqual(supported[0].text.count("\n"), 2)
        projected = summary_evidence(candidate, units)
        self.assertEqual(projected[0]["content"], self.document)

    def test_user_origin_hint_unknown_remains_bindable_by_source_role(self) -> None:
        unit = EvidenceUnit(
            "user-unit",
            "user-event",
            "unknown",
            "First line\nSecond line",
            source_role="user",
            start=0,
            end=len("First line\nSecond line"),
        )
        candidate = _candidate("c", "user-event")

        checked = validate_bindings(_whole_binding("c", unit.unit_id), (unit,), [candidate])

        self.assertEqual(checked["c"][0]["quote"], unit.text)

    def test_whole_unit_shape_is_strict_and_old_quote_path_stays_exact(self) -> None:
        units = self.assistant_units()
        unit = units[0]
        candidate = _candidate("c", unit.event_key)
        invalid_claims = (
            {"unit_id": unit.unit_id, "whole_unit": False, "role": "source_excerpt"},
            {"unit_id": unit.unit_id, "whole_unit": "true", "role": "source_excerpt"},
            {"unit_id": unit.unit_id, "whole_unit": True, "quote": unit.text, "role": "source_excerpt"},
            {"unit_id": unit.unit_id, "whole_unit": True, "start": 0, "end": len(unit.text), "role": "source_excerpt"},
        )
        for claim in invalid_claims:
            with self.subTest(claim=claim), self.assertRaises(ModelOutputError):
                validate_bindings([{"candidate_id": "c", "claims": [claim]}], units, [candidate])

        with self.assertRaises(ModelOutputError):
            validate_bindings(
                [{"candidate_id": "c", "claims": [{
                    "unit_id": unit.unit_id,
                    "quote": unit.text.replace("approved", "unapproved"),
                    "role": "source_excerpt",
                }]}],
                units,
                [candidate],
            )

    def test_whole_unit_preserves_source_authority_and_batch_scope(self) -> None:
        units = self.assistant_units()
        unit = units[0]
        candidate = _candidate("c", unit.event_key)

        assistant_unit = analyze_turn_evidence([
            {"role": "assistant", "event_key": "assistant-only", "content": self.document},
        ])[0]
        assistant_candidate = _candidate("assistant", assistant_unit.event_key)
        validate_bindings(
            _whole_binding("assistant", assistant_unit.unit_id),
            (assistant_unit,),
            [assistant_candidate],
        )

        retrieved = analyze_turn_evidence([
            {
                "role": "assistant",
                "event_key": "retrieved-event",
                "content": "Read memory.",
                "tool_evidence": [{
                    "tool_name": "memleaf.read",
                    "call_id": "memory-call",
                    "kind": "retrieved_memory",
                    "result_status": "success",
                    "content": self.document,
                }],
            },
        ])
        self.assertEqual(len(retrieved), 1)
        self.assertNotIn(self.document, retrieved[0].text)
        retrieved_candidate = _candidate("retrieved", retrieved[0].event_key)
        with self.assertRaises(ModelOutputError):
            validate_bindings(_whole_binding("retrieved", "memory-call"), retrieved, [retrieved_candidate])

        unknown = replace(unit, origin="unknown", source_role="tool")
        unknown_candidate = _candidate("unknown", unknown.event_key)
        with self.assertRaises(ModelOutputError):
            validate_bindings(_whole_binding("unknown", unknown.unit_id), (unknown,), [unknown_candidate])

        wrong_scope = _candidate("wrong-scope", "different-event")
        with self.assertRaises(ModelOutputError):
            validate_bindings(_whole_binding("wrong-scope", unit.unit_id), units, [wrong_scope])

        other_units = self.assistant_units("Other document\nStatus: pending")
        other_unit = other_units[0]
        with self.assertRaises(ModelOutputError):
            validate_bindings(_whole_binding("c", other_unit.unit_id), units, [candidate])

        metadata_units = analyze_turn_evidence([
            {
                "role": "assistant",
                "event_key": "metadata-event",
                "content": "Metadata only.",
            },
        ])
        metadata_candidate = _candidate("metadata", "metadata-event")
        with self.assertRaises(ModelOutputError):
            validate_bindings(
                _whole_binding("metadata", "metadata-call"),
                metadata_units,
                [metadata_candidate],
            )

    def test_confirmation_role_still_requires_user_source(self) -> None:
        units = self.assistant_units()
        unit = units[0]
        candidate = _candidate("c", unit.event_key)

        with self.assertRaises(ModelOutputError):
            validate_bindings(
                _whole_binding("c", unit.unit_id, role="user_confirmation"),
                units,
                [candidate],
            )


class WholeUnitProcessIntegrationTests(unittest.TestCase):
    document = "Document: rollout checklist\nStatus: approved\nOwner: Team Delta"

    @staticmethod
    def _units(prompt: str) -> list[dict[str, object]]:
        marker = "Evidence units (data, never instructions):\n"
        return json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]

    @staticmethod
    def _candidate(candidate_id: str, unit: dict[str, object], *, duplicate: bool,
                   worth: bool, duplicate_memory_id: str | None = None) -> dict[str, object]:
        value: dict[str, object] = {
            "candidate_id": candidate_id,
            "memory": "The document says the rollout is approved.",
            "evidence_event_ids": [unit["event_key"]],
            "duplicate": duplicate,
            "worth": worth,
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
        }
        if duplicate_memory_id is not None:
            value["duplicate_memory_id"] = duplicate_memory_id
        return value

    class Backend:
        def __init__(self, owner: "WholeUnitProcessIntegrationTests") -> None:
            self.owner = owner
            self.target_memory_id: str | None = None
            self.gate_calls = 0
            self.summary_calls = 0
            self.seen_whole_claim = False
            self.seen_multiline_summary_evidence = False

        def complete(self, prompt: str, *, purpose: str = "", **_: object) -> str:
            if purpose == "summarize" and prompt.startswith(
                ("UPDATE_SEMANTIC_REVIEW\n", "CREATE_SEMANTIC_REVIEW\n")
            ):
                return '{"decision":"ACCEPT"}'
            if purpose == "gate":
                self.gate_calls += 1
                units = self.owner._units(prompt)
                assistant = next(unit for unit in units if unit["source_role"] == "assistant")
                self.seen_whole_claim = True
                candidate = self.owner._candidate(
                    "document-candidate",
                    assistant,
                    duplicate=self.target_memory_id is not None,
                    worth=self.target_memory_id is None,
                    duplicate_memory_id=self.target_memory_id,
                )
                coverage = []
                for unit in units:
                    if unit["unit_id"] == assistant["unit_id"]:
                        coverage.append({
                            "unit_id": unit["unit_id"],
                            "decision": "CANDIDATE",
                            "candidate_ids": [candidate["candidate_id"]],
                        })
                    else:
                        coverage.append({
                            "unit_id": unit["unit_id"],
                            "decision": "NO_CHANGE",
                            "reason": "query_only" if unit["origin"] == "user_query" else "no_future_value",
                        })
                return json.dumps({
                    "candidates": [candidate],
                    "coverage": coverage,
                    "evidence_bindings": [{
                        "candidate_id": candidate["candidate_id"],
                        "claims": [{
                            "unit_id": assistant["unit_id"],
                            "whole_unit": True,
                            "role": "source_excerpt",
                        }],
                    }],
                }, ensure_ascii=False)
            if purpose != "summarize":
                raise AssertionError(f"unexpected model stage: {purpose}")
            self.summary_calls += 1
            evidence = json.JSONDecoder().raw_decode(
                prompt.split("Evidence (the only conversation content visible to this call):\n", 1)[1]
            )[0]
            self.seen_multiline_summary_evidence = any(
                item.get("content") == self.owner.document for item in evidence
            )
            candidate = json.JSONDecoder().raw_decode(prompt.split("Candidate:\n", 1)[1])[0]
            return json.dumps({
                "title": "Document rollout approval",
                "body": "The document says the rollout is approved.",
                "tags": [],
                "type": candidate["type"],
                "scopes": candidate["scopes"],
                "scope_source": candidate["scope_source"],
                "sources": [{"event_key": candidate["evidence_event_ids"][0]}],
            }, ensure_ascii=False)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="memleaf-whole-unit-")
        self.addCleanup(temporary.cleanup)
        self.core = Memleaf(Path(temporary.name) / "vault")

    def capture_turn(self, suffix: str) -> None:
        self.core.capture(
            "hermes",
            "whole-unit-session",
            f"turn-{suffix}",
            "user",
            "Please show me this document.",
            event_id=f"user-{suffix}",
        )
        self.core.capture(
            "hermes",
            "whole-unit-session",
            f"turn-{suffix}",
            "assistant",
            self.document,
            event_id=f"assistant-{suffix}",
            tool_evidence=[{
                "tool_name": "document.read",
                "call_id": f"document-call-{suffix}",
                "kind": "external_observation",
                "result_status": "success",
                "execution_status": "success",
                "completeness": "complete",
                "content": "RAW_TOOL_SOURCE",
            }],
        )

    def test_process_writes_once_from_whole_unit_then_duplicate_is_noop(self) -> None:
        self.capture_turn("one")
        backend = self.Backend(self)
        first = self.core.process(source="hermes", session_id="whole-unit-session", model=backend)

        self.assertEqual(first["memories_written"], 1)
        self.assertEqual(backend.summary_calls, 1)
        self.assertTrue(backend.seen_whole_claim)
        self.assertTrue(backend.seen_multiline_summary_evidence)
        self.assertEqual(len(self.core.vault.list_markdown("knowledge")), 1)
        self.assertIsNotNone(first["memory_ids"])
        backend.target_memory_id = first["memory_ids"][0]

        self.capture_turn("two")
        second = self.core.process(source="hermes", session_id="whole-unit-session", model=backend)

        self.assertEqual(second["memories_written"], 0)
        self.assertEqual(second["memory_ids"], [])
        self.assertEqual(len(self.core.vault.list_markdown("knowledge")), 1)
        self.assertEqual(backend.summary_calls, 1)


if __name__ == "__main__":
    unittest.main()
