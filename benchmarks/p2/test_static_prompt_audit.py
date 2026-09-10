from __future__ import annotations

import unittest

from benchmarks.p2.static_prompt_audit import build_report, sample_events
from memleaf.prompts import gate_prompt


class P2GateInputSlimTests(unittest.TestCase):
    def test_gate_event_envelope_contains_metadata_but_not_conversation_body(self):
        prompt = gate_prompt(sample_events())
        self.assertIn("p2/e1", prompt)
        self.assertIn("2026-09-10T06:00:00Z", prompt)
        self.assertNotIn("P2_UNIQUE_USER", prompt)
        self.assertNotIn("P2_UNIQUE_ASSISTANT", prompt)
        self.assertNotIn("P2_TOOL_BODY_MUST_NOT_APPEAR", prompt)

    def test_combined_gate_input_contains_each_conversation_body_once(self):
        report = build_report()["gate_user_prompt"]
        self.assertEqual(report["legacy_user_marker_occurrences"], 2)
        self.assertEqual(report["p2_user_marker_occurrences"], 1)
        self.assertEqual(report["legacy_assistant_marker_occurrences"], 2)
        self.assertEqual(report["p2_assistant_marker_occurrences"], 1)
        self.assertEqual(report["tool_body_occurrences_p2"], 0)
        self.assertGreater(report["bytes_removed"], 0)
        self.assertGreater(report["reduction_ratio"], 0.25)


if __name__ == "__main__":
    unittest.main()
