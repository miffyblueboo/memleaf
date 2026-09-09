"""Model-created project scopes must be named by the candidate's own source."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.admission import analyze_turn_evidence
from memleaf.index import event_key
from memleaf.memory_planner import (
    _explicit_project_scope_authorizations,
    _model_project_scope_is_source_grounded,
)

from tests.semantic_fixtures import semantic_fixture


@semantic_fixture
class QueueBackend:
    provider = "fake"
    model = "new-scope-source-grounding"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, str]] = []

    def complete(self, prompt: str, *, system: str = "", purpose: str = "", temperature: float = 0.0) -> str:
        del system, temperature
        self.calls.append({"prompt": prompt, "purpose": purpose})
        if not self.responses:
            raise AssertionError("new-scope source grounding model queue exhausted")
        return self.responses.pop(0)


def _candidate(candidate_id: str, event_id: str, memory: str, scopes: list[str]) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "memory": memory,
        "evidence_event_ids": [event_id],
        "duplicate": False,
        "worth": True,
        "type": "fact",
        "scopes": scopes,
        "scope_source": "model",
    }


def _gate(candidates: list[dict[str, object]]) -> str:
    return json.dumps({"candidates": candidates}, ensure_ascii=False)


def _summary(event_id: str, *, body: str, scopes: list[str]) -> str:
    return json.dumps(
        {
            "title": body,
            "body": body,
            "tags": ["scope-grounding"],
            "type": "fact",
            "scopes": scopes,
            "scope_source": "model",
            "sources": [{"event_key": event_id}],
        },
        ensure_ascii=False,
    )


class NewScopeSourceGroundingTests(unittest.TestCase):
    def test_unregistered_scope_retries_then_defers_only_unsupported_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            source_event = "source-user"
            source_event_key = event_key(source_event)
            source_text = "Orion release plan is pending."
            service = Memleaf(vault)
            service.capture("codex", "session", "turn", "user", source_text, event_id=source_event)
            service.capture("codex", "session", "turn", "assistant", "Acknowledged.", event_id="source-assistant")

            unsupported = _candidate(
                "invented-scope", source_event_key,
                "Alpha release plan is pending.", ["project:Alpha"],
            )
            supported = _candidate(
                "supported-fact", source_event_key,
                source_text, ["global"],
            )
            gate = _gate([unsupported, supported])
            backend = QueueBackend([
                gate,
                gate,
                gate,
                _summary(event_key(source_event), body=source_text, scopes=["global"]),
            ])

            result = service.process(source="codex", session_id="session", model=backend)

            self.assertEqual(result["memories_written"], 1)
            self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "gate", "summarize"])
            self.assertIn("scope_not_grounded", backend.calls[1]["prompt"])
            self.assertIn("scope_not_grounded", backend.calls[2]["prompt"])
            memories = service._read_memories_unlocked("knowledge")
            self.assertEqual([record.memory.body for record in memories], [source_text])

            state = json.loads(service.vault.processed_state_path.read_text(encoding="utf-8"))
            entry = state["sessions"]["codex/session"]["processed_turns"][0]
            self.assertEqual(len(entry["deferred_candidates"]), 1)
            self.assertEqual(entry["deferred_candidates"][0]["candidate_id"], "invented-scope")
            self.assertEqual(entry["deferred_candidates"][0]["reason"], "scope_conflict")

    def test_source_matching_allows_other_project_mentions_in_the_same_bound_unit(self) -> None:
        units = analyze_turn_evidence([{
            "role": "user",
            "content": "Alpha release plan is pending; Orion is also referenced.",
            "event_key": "source",
        }])
        candidate = {
            "scope_source": "model",
            "worth": True,
            "scopes": ["project:Alpha"],
            "_evidence_bindings": [{
                "unit_id": units[0].unit_id,
                "quote": units[0].text,
                "role": "source_excerpt",
            }],
        }

        self.assertTrue(_model_project_scope_is_source_grounded(candidate, units, {
            "project:Orion": {},
        }))

    def test_source_matching_uses_bound_units_only(self) -> None:
        units = analyze_turn_evidence([
            {"role": "user", "content": "Orion release plan is pending.", "event_key": "orion"},
            {"role": "user", "content": "Alpha release plan is pending.", "event_key": "alpha"},
        ])
        candidate = {
            "scope_source": "model",
            "worth": True,
            "scopes": ["project:Alpha"],
            "_evidence_bindings": [{
                "unit_id": units[0].unit_id,
                "quote": units[0].text,
                "role": "source_excerpt",
            }],
        }

        self.assertFalse(_model_project_scope_is_source_grounded(candidate, units, {}))
        candidate["_evidence_bindings"] = [{
            "unit_id": units[1].unit_id,
            "quote": units[1].text,
            "role": "source_excerpt",
        }]
        self.assertTrue(_model_project_scope_is_source_grounded(candidate, units, {}))

    def test_legacy_exact_whole_support_uses_frozen_unit_ids(self) -> None:
        units = analyze_turn_evidence([{
            "role": "user",
            "content": "Orion release plan is pending.",
            "event_key": "source",
        }])
        candidate = {
            "scope_source": "model",
            "worth": True,
            "scopes": ["project:Alpha"],
            "_evidence_unit_ids": [units[0].unit_id],
        }

        self.assertFalse(_model_project_scope_is_source_grounded(candidate, units, {}))

    def test_explicit_process_scope_authorizes_only_the_normalized_requested_name(self) -> None:
        units = analyze_turn_evidence([{
            "role": "user",
            "content": "The source does not name the requested project.",
            "event_key": "source",
        }])
        candidate = {
            "scope_source": "model",
            "worth": True,
            "scopes": ["project:Alpha"],
            "_evidence_bindings": [{
                "unit_id": units[0].unit_id,
                "quote": units[0].text,
                "role": "source_excerpt",
            }],
        }

        self.assertFalse(_model_project_scope_is_source_grounded(candidate, units, {}))
        self.assertTrue(_model_project_scope_is_source_grounded(
            candidate,
            units,
            {},
            _explicit_project_scope_authorizations(["project:Alpha", "global"]),
        ))
        self.assertEqual(_explicit_project_scope_authorizations("project:Alpha"), ("project:Alpha",))
        self.assertEqual(_explicit_project_scope_authorizations("not-a-scope"), ())

    def test_registered_project_alias_requires_alias_in_bound_source(self) -> None:
        units = analyze_turn_evidence([{
            "role": "user",
            "content": "OR-1 release plan is pending.",
            "event_key": "source",
        }])
        candidate = {
            "scope_source": "model",
            "worth": True,
            "scopes": ["project:Orion"],
            "_evidence_bindings": [{
                "unit_id": units[0].unit_id,
                "quote": units[0].text,
                "role": "source_excerpt",
            }],
        }
        self.assertTrue(_model_project_scope_is_source_grounded(
            candidate, units, {"project:Orion": {"aliases": ["OR-1"]}},
        ))
        other = analyze_turn_evidence([{
            "role": "user",
            "content": "A source without the configured name or alias.",
            "event_key": "other",
        }])
        candidate["_evidence_bindings"] = [{
            "unit_id": other[0].unit_id,
            "quote": other[0].text,
            "role": "source_excerpt",
        }]
        self.assertFalse(_model_project_scope_is_source_grounded(
            candidate, other, {"project:Orion": {"aliases": ["OR-1"]}},
        ))


if __name__ == "__main__":
    unittest.main()
