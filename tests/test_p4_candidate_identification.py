from __future__ import annotations

import json
import unittest

from memleaf.candidate_identification import (
    IDENTIFICATION_SYSTEM,
    parse_identification_candidates,
    run_identification_stage,
)
from memleaf.validation import ModelOutputError


def semantic(candidate):
    return json.dumps({"candidates": [candidate]})


def candidate(**changes):
    value = {
        "candidate_id": "c1",
        "memory": "Alpha applies.",
        "evidence_event_ids": ["e1"],
        "duplicate": False,
        "worth": True,
        "type": "fact",
        "scopes": ["global"],
        "scope_source": "model",
    }
    value.update(changes)
    return value


class FakeExecutor:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def _complete_json_stage(self, backend, prompt, *, system, purpose, parser, diagnostic_context=None):
        self.calls.append({
            "backend": backend,
            "prompt": prompt,
            "system": system,
            "purpose": purpose,
            "diagnostic_context": diagnostic_context,
        })
        return parser(self.raw)


class CandidateIdentificationTests(unittest.TestCase):
    def test_worthy_targetless_candidate_reuses_existing_gate_validation(self):
        result = parse_identification_candidates(
            semantic(candidate()),
            current_event_keys=["e1"],
        )
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["candidate_id"], "c1")
        self.assertNotIn("update_memory_id", result["candidates"][0])
        self.assertNotIn("duplicate_memory_id", result["candidates"][0])

    def test_empty_candidate_set_is_valid_identification_result(self):
        result = parse_identification_candidates(
            json.dumps({"candidates": []}),
            current_event_keys=["e1"],
        )
        self.assertEqual(result, {"candidates": []})

    def test_stage_one_rejects_nonworthy_or_duplicate_candidate_rows(self):
        for changes in (
            {"worth": False},
            {"duplicate": True, "worth": False},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ModelOutputError):
                    parse_identification_candidates(
                        semantic(candidate(**changes)),
                        current_event_keys=["e1"],
                    )

    def test_stage_one_rejects_reason_as_final_decision_leakage(self):
        with self.assertRaises(ModelOutputError):
            parse_identification_candidates(
                semantic(candidate(reason="duplicate")),
                current_event_keys=["e1"],
            )

    def test_stage_one_rejects_update_and_duplicate_targets_even_if_caller_supplies_context(self):
        for field in ("update_memory_id", "duplicate_memory_id"):
            changes = {field: "m1"}
            if field == "duplicate_memory_id":
                changes.update({"duplicate": True, "worth": False})
            with self.subTest(field=field):
                with self.assertRaises(ModelOutputError):
                    parse_identification_candidates(
                        semantic(candidate(**changes)),
                        current_event_keys=["e1"],
                        related_memory_ids=["m1"],
                        related_memory_types={"m1": "fact"},
                    )

    def test_identification_system_explicitly_defers_target_work(self):
        self.assertIn("do NOT decide CREATE versus UPDATE versus NO_CHANGE", IDENTIFICATION_SYSTEM)
        self.assertIn("Do not return duplicate_memory_id or update_memory_id", IDENTIFICATION_SYSTEM)
        self.assertIn("Core will retrieve relevant memory after this stage", IDENTIFICATION_SYSTEM)

    def test_run_identification_stage_uses_one_gate_call_and_supplied_parser(self):
        raw = semantic(candidate())
        executor = FakeExecutor(raw)
        result = run_identification_stage(
            executor,
            "backend",
            "IDENTIFY\n{}",
            parser=lambda value: parse_identification_candidates(
                value,
                current_event_keys=["e1"],
            ),
            diagnostic_context={"session_id": "s"},
        )
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(executor.calls[0]["system"], IDENTIFICATION_SYSTEM)
        self.assertEqual(executor.calls[0]["purpose"], "gate")
        self.assertEqual(result["candidates"][0]["memory"], "Alpha applies.")


if __name__ == "__main__":
    unittest.main()
