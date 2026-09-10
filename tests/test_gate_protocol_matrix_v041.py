\
"""Executable Gate decision/claim matrix for the strict v0.4.1 protocol."""
from __future__ import annotations

import unittest

from memleaf.admission import analyze_turn_evidence, parse_coverage, validate_bindings
from memleaf.validation import ModelOutputError


def _candidate(candidate_id: str, event_key: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "memory": "Alpha applies.",
        "evidence_event_ids": [event_key],
        "duplicate": False,
        "worth": True,
        "type": "fact",
        "scopes": ["global"],
        "scope_source": "model",
    }


class GateProtocolMatrixV041Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.units = analyze_turn_evidence([{
            "role": "user", "event_key": "user-event", "content": "Alpha applies."
        }])
        self.assertEqual(len(self.units), 1)
        self.unit = self.units[0]
        self.candidate = _candidate("c1", self.unit.event_key)

    def test_claim_variant_matrix_accepts_canonical_legacy_and_whole_unit(self) -> None:
        quote = self.unit.text
        variants = (
            {"unit_id": self.unit.unit_id, "quote": quote, "role": "assertion"},
            {"unit_id": self.unit.unit_id, "quote": quote, "start": 0, "end": len(quote), "role": "assertion"},
            {"unit_id": self.unit.unit_id, "whole_unit": True, "role": "assertion"},
        )
        normalized = []
        for claim in variants:
            checked = validate_bindings(
                [{"candidate_id": "c1", "claims": [claim]}], self.units, [self.candidate]
            )
            self.assertEqual(checked["c1"][0]["quote"], quote)
            self.assertEqual(checked["c1"][0]["start"], 0)
            self.assertEqual(checked["c1"][0]["end"], len(quote))
            normalized.append(checked["c1"])
        self.assertEqual(normalized[0], normalized[1])
        self.assertEqual(normalized[0], normalized[2])

    def test_coverage_decision_matrix_accepts_canonical_shapes(self) -> None:
        uid = self.unit.unit_id
        accepted = (
            ({"unit_id": uid, "decision": "CANDIDATE", "candidate_ids": ["c1"]}, [self.candidate], None, "CANDIDATE"),
            ({"unit_id": uid, "decision": "NO_CHANGE", "reason": "no_future_value"}, [], None, "NO_CHANGE"),
            ({"unit_id": uid, "decision": "DEFERRED", "reason": "coverage_unresolved"}, [], None, "DEFERRED"),
            ({"unit_id": uid, "decision": "NO_CHANGE", "reason": "already_completed", "memory_id": "TODO-DONE"}, [], {"todo-done": {"type": "todo", "status": "completed"}}, "NO_CHANGE"),
        )
        for row, candidates, witnesses, expected in accepted:
            with self.subTest(row=row):
                parsed = parse_coverage([row], self.units, candidates, todo_witnesses=witnesses)
                self.assertEqual(parsed[uid]["decision"], expected)
        terminal = parse_coverage(
            [{"unit_id": uid, "decision": "NO_CHANGE", "reason": "already_completed", "memory_id": "TODO-DONE"}],
            self.units, [], todo_witnesses={"todo-done": {"type": "todo", "status": "completed"}},
        )
        self.assertEqual(terminal[uid]["memory_id"], "todo-done")

    def test_coverage_decision_matrix_rejects_cross_variant_fields(self) -> None:
        uid = self.unit.unit_id
        invalid = (
            ({"unit_id": uid, "decision": "CANDIDATE", "candidate_ids": ["c1"], "reason": "no_future_value"}, [self.candidate], None),
            ({"unit_id": uid, "decision": "NO_CHANGE", "reason": "no_future_value", "candidate_ids": []}, [], None),
            ({"unit_id": uid, "decision": "DEFERRED", "reason": "coverage_unresolved", "candidate_ids": []}, [], None),
            ({"unit_id": uid, "decision": "NO_CHANGE", "reason": "already_completed", "memory_id": "todo-done", "candidate_ids": []}, [], {"todo-done": {"type": "todo", "status": "completed"}}),
        )
        for row, candidates, witnesses in invalid:
            with self.subTest(row=row), self.assertRaises(ModelOutputError) as raised:
                parse_coverage([row], self.units, candidates, todo_witnesses=witnesses)
            self.assertEqual(raised.exception.evidence_check, "coverage_shape")

    def test_terminal_witness_diagnostics_keep_priority(self) -> None:
        uid = self.unit.unit_id
        with self.assertRaises(ModelOutputError) as missing:
            parse_coverage([
                {"unit_id": uid, "decision": "NO_CHANGE", "reason": "already_completed"}
            ], self.units, [])
        self.assertEqual(missing.exception.evidence_check, "coverage_terminal_witness")
        with self.assertRaises(ModelOutputError) as wrong_place:
            parse_coverage([
                {"unit_id": uid, "decision": "CANDIDATE", "candidate_ids": ["c1"], "memory_id": "todo-done"}
            ], self.units, [self.candidate])
        self.assertEqual(wrong_place.exception.evidence_check, "coverage_terminal_witness")

    def test_binding_matrix_rejects_cross_variant_fields(self) -> None:
        quote = self.unit.text
        invalid_claims = (
            {"unit_id": self.unit.unit_id, "quote": quote, "whole_unit": True, "role": "assertion"},
            {"unit_id": self.unit.unit_id, "quote": quote, "start": 0, "role": "assertion"},
        )
        for claim in invalid_claims:
            with self.subTest(claim=claim), self.assertRaises(ModelOutputError):
                validate_bindings([{"candidate_id": "c1", "claims": [claim]}], self.units, [self.candidate])
        with self.assertRaises(ModelOutputError):
            validate_bindings([
                {"candidate_id": "c1", "claims": [{"unit_id": self.unit.unit_id, "quote": quote, "role": "assertion"}], "extra": "forbidden"}
            ], self.units, [self.candidate])


if __name__ == "__main__":
    unittest.main()
