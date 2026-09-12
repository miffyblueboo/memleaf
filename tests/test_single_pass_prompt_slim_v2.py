from __future__ import annotations

import json
import unittest

from memleaf.admission import EvidenceUnit
from memleaf.single_pass_plan import (
    PROTOCOL_VERSION,
    SINGLE_PASS_SYSTEM,
    build_single_pass_prompt,
)


class SinglePassPromptSlimV2Tests(unittest.TestCase):
    def test_system_prompt_stays_slim_without_dropping_core_contract(self):
        self.assertLessEqual(len(SINGLE_PASS_SYSTEM.encode("utf-8")), 2700)
        for required in (
            "current_evidence",
            "local_memory_catalog",
            "native_memory_catalog",
            "lookup_complete=true",
            "CREATE",
            "UPDATE",
            "NO_CHANGE",
            "DEFERRED",
            "Omission is not retraction/completion",
            "protocol_version=b3-single-pass-v1",
        ):
            self.assertIn(required, SINGLE_PASS_SYSTEM)
        self.assertIn("native memory is never an UPDATE/NO_CHANGE target", SINGLE_PASS_SYSTEM)
        self.assertIn("cover all current_evidence", SINGLE_PASS_SYSTEM)

    def test_dynamic_prompt_contains_each_body_once_and_no_hidden_reasoning_request(self):
        unit = EvidenceUnit(
            unit_id="u1",
            event_key="event-u1",
            origin="user_assertion",
            text="Alpha now uses PostgreSQL.",
            source_role="user",
            start=0,
            end=len("Alpha now uses PostgreSQL."),
        )
        prompt, _, _ = build_single_pass_prompt(
            evidence_units=[unit],
            related_memories=[{
                "memory_id": "m1",
                "title": "Alpha database",
                "body": "Alpha used MySQL.",
                "type": "fact",
                "scopes": ["global"],
            }],
            native_memories=[{
                "native_id": "n1",
                "title": "Native database note",
                "body": "Native comparison body.",
                "scopes": ["global"],
            }],
        )

        self.assertEqual(prompt.count("Alpha now uses PostgreSQL."), 1)
        self.assertEqual(prompt.count("Alpha used MySQL."), 1)
        self.assertEqual(prompt.count("Native comparison body."), 1)
        payload = json.loads(prompt.split("B3_INPUT\n", 1)[1].split("\nReturn", 1)[0])
        self.assertEqual(payload["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(payload["current_evidence"][0]["content"], "Alpha now uses PostgreSQL.")
        self.assertNotIn("reasoning", payload)
        self.assertNotIn("analysis", payload)


if __name__ == "__main__":
    unittest.main()
