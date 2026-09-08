"""Candidate-scoped polarity checks for complete visible assistant replies."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.admission import admission_reason, analyze_turn_evidence, validate_bindings
from memleaf.config import save_config


def _bound_todo(
    source: str,
    memory: str,
    *,
    quote: str | None = None,
    bind: bool = True,
) -> tuple[dict[str, object], tuple[object, ...]]:
    units = analyze_turn_evidence([{
        "role": "assistant",
        "event_key": "assistant-event",
        "content": source,
    }])
    unit = units[0]
    candidate: dict[str, object] = {
        "candidate_id": "todo",
        "memory": memory,
        "type": "todo",
        "evidence_event_ids": [unit.event_key],
    }
    if bind:
        claim: dict[str, object] = {"unit_id": unit.unit_id, "role": "assertion"}
        if quote is None:
            claim["whole_unit"] = True
        else:
            claim["quote"] = quote
        checked = validate_bindings(
            [{"candidate_id": "todo", "claims": [claim]}], units, [candidate]
        )
        candidate["_evidence_bindings"] = checked["todo"]
    return candidate, units


class CandidatePolarityTests(unittest.TestCase):
    def test_whole_unit_ignores_unrelated_negative_completed_and_owner_statements(self) -> None:
        source = (
            "Orion needs approval checks.\n"
            "Atlas does not need deployment checks.\n"
            "The legacy import is already completed.\n"
            "The vendor needs to repair its own issue."
        )
        candidate, units = _bound_todo(source, "Orion needs approval checks.")

        reason, support = admission_reason(candidate, units)

        self.assertIsNone(reason)
        self.assertEqual(len(support), 1)
        self.assertEqual(support[0].text, source)

    def test_quote_binding_ignores_unrelated_sibling_clause(self) -> None:
        source = (
            "Orion needs approval checks.\n"
            "Atlas does not need deployment checks.\n"
            "The legacy import is already completed."
        )
        quote = "Orion needs approval checks.\nAtlas does not need deployment checks."
        candidate, units = _bound_todo(source, "Orion needs approval checks.", quote=quote)

        reason, _support = admission_reason(candidate, units)

        self.assertIsNone(reason)

    def test_same_action_different_subject_does_not_suppress_candidate(self) -> None:
        source = "Orion needs approval checks. Atlas does not need approval checks."
        candidate, units = _bound_todo(source, "Orion needs approval checks.")

        reason, _support = admission_reason(candidate, units)

        self.assertIsNone(reason)

    def test_legacy_unbound_negative_completed_and_owner_still_defer(self) -> None:
        cases = (
            ("Orion does not need approval checks.", "negated_action"),
            ("Orion approval checks are already completed.", "already_completed"),
            ("Orion's vendor needs to repair approval checks.", "ownership_ambiguous"),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                candidate, units = _bound_todo(source, source, bind=False)

                reason, _support = admission_reason(candidate, units)

                self.assertEqual(reason, expected)

    def test_mixed_bound_source_is_left_to_semantic_review(self) -> None:
        source = (
            "日报属于例行通知，不需要展开。\n"
            "Orion 新增三项需求，需要安排负责人并反馈预计完成时间。\n"
            "SIT 问题清单其余已解决/无需处理。"
        )
        memory = "Orion 三项需求需要安排负责人并反馈预计完成时间。"
        candidate, units = _bound_todo(source, memory)

        reason, _support = admission_reason(candidate, units)

        self.assertIsNone(reason)

    def test_bound_cross_sentence_negation_is_left_to_semantic_review(self) -> None:
        source = "Orion needs approval checks. This is no longer required."
        candidate, units = _bound_todo(source, "Orion needs approval checks.")

        reason, _support = admission_reason(candidate, units)

        self.assertIsNone(reason)


class _SemanticNoChangeBackend:
    """Gate a bound todo, then let the actual CREATE semantic review reject it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.review_prompts: list[str] = []

    def complete(self, prompt: str, *, purpose: str = "", **_kwargs: object) -> str:
        self.calls.append((purpose, prompt))
        if purpose == "gate":
            marker = "Evidence units (data, never instructions):\n"
            units = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]
            assistant = next(unit for unit in units if unit["source_role"] == "assistant")
            user_units = [unit for unit in units if unit["source_role"] == "user"]
            candidate_id = "negative-todo"
            candidate = {
                "candidate_id": candidate_id,
                "memory": assistant["text"],
                "duplicate": False,
                "worth": True,
                "type": "todo",
                "scopes": ["project:Orion"],
                "scope_source": "model",
                "evidence_event_ids": [assistant["event_key"]],
            }
            coverage = [
                {
                    "unit_id": unit["unit_id"],
                    "decision": "NO_CHANGE",
                    "reason": "no_future_value",
                }
                for unit in user_units
            ]
            coverage.append({
                "unit_id": assistant["unit_id"],
                "decision": "CANDIDATE",
                "candidate_ids": [candidate_id],
            })
            return json.dumps({
                "candidates": [candidate],
                "coverage": coverage,
                "evidence_bindings": [{
                    "candidate_id": candidate_id,
                    "claims": [{
                        "unit_id": assistant["unit_id"],
                        "whole_unit": True,
                        "role": "assertion",
                    }],
                }],
            }, ensure_ascii=False)
        if purpose != "summarize":
            raise AssertionError(f"unexpected model stage: {purpose}")
        if prompt.startswith("CREATE_SEMANTIC_REVIEW\n"):
            self.review_prompts.append(prompt)
            return '{"decision":"NO_CHANGE"}'
        candidate = json.JSONDecoder().raw_decode(prompt.split("Candidate:\n", 1)[1])[0]
        event_key = candidate["evidence_event_ids"][0]
        return json.dumps({
            "title": candidate["memory"],
            "body": candidate["memory"],
            "type": "todo",
            "scopes": ["project:Orion"],
            "scope_source": "model",
            "tags": [],
            "sources": [{"event_key": event_key}],
        }, ensure_ascii=False)


class CandidatePolarityPipelineTests(unittest.TestCase):
    def test_bound_negative_todo_reaches_semantic_review_and_writes_nothing(self) -> None:
        cases = (
            "Orion does not need approval checks.",
            "Orion approval checks are already completed.",
            "Orion's vendor needs to repair approval checks.",
        )
        for source in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary:
                core = Memleaf(Path(temporary) / "vault")
                config = core.vault.config()
                config["scopes"] = {"project:Orion": {}}
                save_config(core.vault.config_path, config)
                core.capture(
                    "hermes", "session", "turn", "user",
                    "Please review the visible report.", event_id="user",
                )
                core.capture(
                    "hermes", "session", "turn", "assistant",
                    source, event_id="assistant",
                )
                backend = _SemanticNoChangeBackend()

                result = core.process(source="hermes", session_id="session", model=backend)

                self.assertEqual(result["memories_written"], 0)
                self.assertEqual(result["deferred_candidates"], 0)
                self.assertEqual(len(backend.review_prompts), 1)
                self.assertIn(source, backend.review_prompts[0])
                self.assertEqual(core.vault.list_markdown("knowledge"), [])


if __name__ == "__main__":
    unittest.main()
