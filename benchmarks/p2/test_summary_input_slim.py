from __future__ import annotations

import unittest

from benchmarks.p2.summary_prompt_audit import (
    build_report,
    sample_candidate,
    sample_evidence,
    sample_related_memories,
)
from memleaf.prompts import summarize_prompt


class P2SummaryInputSlimTests(unittest.TestCase):
    def test_automatic_create_keeps_native_and_drops_memleaf_comparison_bodies(self):
        prompt = summarize_prompt(
            sample_candidate(update=False),
            sample_evidence(),
            related_memories=sample_related_memories(),
            scope_background={"marker": "P2_SCOPE_BACKGROUND"},
            scope_registry=[{"scope": "project:synthetic", "marker": "P2_SCOPE_REGISTRY"}],
        )
        self.assertNotIn("P2_UNRELATED_A", prompt)
        self.assertNotIn("P2_UNRELATED_B", prompt)
        self.assertNotIn("P2_FIXED_TARGET", prompt)
        self.assertIn("P2_NATIVE_CONTEXT", prompt)
        self.assertIn("P2_SCOPE_BACKGROUND", prompt)
        self.assertIn("P2_SCOPE_REGISTRY", prompt)

    def test_automatic_update_keeps_only_fixed_memleaf_target_plus_native(self):
        prompt = summarize_prompt(
            sample_candidate(update=True),
            sample_evidence(),
            related_memories=sample_related_memories(),
        )
        self.assertNotIn("P2_UNRELATED_A", prompt)
        self.assertNotIn("P2_UNRELATED_B", prompt)
        self.assertIn("P2_FIXED_TARGET", prompt)
        self.assertIn("P2_NATIVE_CONTEXT", prompt)

    def test_explicit_remember_preserves_full_related_context(self):
        prompt = summarize_prompt(
            sample_candidate(update=False),
            sample_evidence(),
            explicit=True,
            related_memories=sample_related_memories(),
        )
        self.assertIn("P2_UNRELATED_A", prompt)
        self.assertIn("P2_UNRELATED_B", prompt)
        self.assertIn("P2_FIXED_TARGET", prompt)
        self.assertIn("P2_NATIVE_CONTEXT", prompt)

    def test_static_summary_reports_remove_bytes_without_dropping_required_context(self):
        report = build_report()
        create = report["automatic_create"]
        update = report["automatic_update"]
        self.assertGreater(create["bytes_removed"], 0)
        self.assertGreater(update["bytes_removed"], 0)
        self.assertEqual(create["unrelated_a_occurrences"], 0)
        self.assertEqual(create["fixed_target_occurrences"], 0)
        self.assertEqual(create["native_occurrences"], 1)
        self.assertEqual(update["unrelated_a_occurrences"], 0)
        self.assertEqual(update["fixed_target_occurrences"], 1)
        self.assertEqual(update["native_occurrences"], 1)
        self.assertEqual(create["scope_background_occurrences"], 1)
        self.assertEqual(create["scope_registry_occurrences"], 1)


if __name__ == "__main__":
    unittest.main()
