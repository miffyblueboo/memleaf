from __future__ import annotations

import json
import unittest

from memleaf.admission import EvidenceUnit, parse_coverage, validate_bindings
from memleaf.prompts import GATE_PROTOCOL_EXAMPLE
from memleaf.validation import ModelOutputError


def _unit() -> EvidenceUnit:
    return EvidenceUnit(
        unit_id="u1",
        event_key="e1",
        origin="user_assertion",
        text="Alpha applies.",
        source_role="user",
    )


class GateOutputReferenceV041Tests(unittest.TestCase):
    def test_unknown_binding_unit_id_is_rejected(self):
        candidates = json.loads(GATE_PROTOCOL_EXAMPLE)["candidates"]
        bindings = [{
            "candidate_id": "c1",
            "claims": [{
                "unit_id": "u-missing",
                "quote": "Alpha applies.",
                "role": "assertion",
            }],
        }]
        with self.assertRaises(ModelOutputError) as raised:
            validate_bindings(bindings, (_unit(),), candidates)
        self.assertEqual(raised.exception.evidence_check, "unknown_unit")

    def test_dangling_coverage_candidate_id_is_rejected(self):
        candidates = json.loads(GATE_PROTOCOL_EXAMPLE)["candidates"]
        coverage = [{
            "unit_id": "u1",
            "decision": "CANDIDATE",
            "candidate_ids": ["c-missing"],
        }]
        with self.assertRaises(ModelOutputError) as raised:
            parse_coverage(coverage, (_unit(),), candidates)
        self.assertEqual(raised.exception.evidence_check, "coverage_candidate")


if __name__ == "__main__":
    unittest.main()
