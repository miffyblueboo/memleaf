from __future__ import annotations

import unittest

from memleaf.config import DEFAULT_THINKING
from memleaf.prompts import GATE_OUTPUT_PROTOCOL, GATE_SYSTEM, SUMMARIZE_SYSTEM


class P2SystemPromptSlimTests(unittest.TestCase):
    def test_system_prompts_are_smaller_than_phase2_baseline(self):
        self.assertLess(len(GATE_SYSTEM.encode("utf-8")), 8508)
        self.assertLess(len(SUMMARIZE_SYSTEM.encode("utf-8")), 4347)

    def test_gate_keeps_protocol_and_safety_contracts_without_stepwise_reasoning_cues(self):
        gate = " ".join(GATE_SYSTEM.split()).casefold()
        for phrase in (
            "candidate count follows the independent future uses",
            "do not impose a zero-or-one default",
            "candidate semantic completeness is mandatory",
            "coverage row for a unit cited by several candidates must list every such candidate_id",
            "update/no_change takes precedence over create",
            "core validates exact quotes",
            "implementation context is not project ownership by name alone",
            "do not invent dates",
        ):
            self.assertIn(phrase, gate)
        self.assertNotIn("first enumerate", gate)
        self.assertIn(GATE_OUTPUT_PROTOCOL, GATE_SYSTEM)

    def test_summary_keeps_update_safety_without_stepwise_reasoning_cues(self):
        summary = " ".join(SUMMARIZE_SYSTEM.split()).casefold()
        for phrase in (
            "the gate has already decided",
            "treat the supplied target as prior state and current admitted evidence as the only authority for change",
            "retain still-valid information",
            "no new confirmed state, fact, deadline, or obligation change",
            "only when current evidence confirms a real semantic change",
            "do not add sibling deliverables",
            "do not infer a target",
        ):
            self.assertIn(phrase, summary)
        self.assertNotIn("first compare", summary)

    def test_product_thinking_defaults_remain_low_not_disabled(self):
        self.assertEqual(DEFAULT_THINKING, {"gate": "low", "summarize": "low", "compact": "low"})


if __name__ == "__main__":
    unittest.main()
