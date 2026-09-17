from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf, Memory
from memleaf.mcp_server import _read_page_result
from memleaf.models import MemoryVersionError
from memleaf.retention import RetentionManager
from memleaf.turn_plan import content_digest


class G1MemoryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.service = Memleaf.initialize(Path(self.temporary_directory.name) / "vault")

    def test_legacy_memory_defaults_to_valid_and_round_trips(self) -> None:
        memory = Memory.from_mapping(
            {
                "memory_id": "mem-legacy",
                "title": "Legacy",
                "body": "still current",
            }
        )

        self.assertEqual(memory.validity, "valid")
        self.assertEqual(Memory.from_markdown(memory.to_markdown()).validity, "valid")
        with self.assertRaises(ValueError):
            Memory.from_mapping(
                {
                    "memory_id": "mem-invalid-retraction",
                    "title": "Invalid",
                    "body": "must not remain current",
                    "validity": "retracted",
                }
            )

    def test_retraction_keeps_identity_and_hides_assertion_from_normal_reads(self) -> None:
        self.service.create_memory(
            memory_id="mem-fact",
            title="Current fact",
            body="This assertion is current.",
            type="fact",
            scopes=["global"],
        )
        page = self.service.read_page("mem-fact")
        assert page is not None

        retracted = self.service.retract_memory(
            "mem-fact",
            expected_revision=page["revision"],
            reason="source was explicitly withdrawn",
        )

        self.assertEqual(retracted.validity, "retracted")
        self.assertEqual(retracted.body, "")
        self.assertIsNone(self.service.read("mem-fact"))
        self.assertIsNone(self.service.read_page("mem-fact"))
        audit = self.service.read_page("mem-fact", include_history=True)
        assert audit is not None
        self.assertEqual(audit["validity"], "retracted")
        self.assertEqual(audit["body"], "")
        projected = _read_page_result(audit)
        assert projected is not None
        self.assertEqual(projected["validity"], "retracted")
        self.assertEqual(projected["revision"], audit["revision"])
        self.assertEqual(
            self.service.search_candidates("Current fact")["status"],
            "no_match",
        )
        histories = self.service.vault.list_markdown("history")
        self.assertEqual(len(histories), 1)
        old = Memory.from_markdown(histories[0].read_text(encoding="utf-8"), histories[0])
        self.assertEqual(old.body, "This assertion is current.")
        self.assertEqual(old.extra["invalidated_reason"], "retracted")

    def test_retraction_requires_current_revision_and_is_idempotent(self) -> None:
        self.service.create_memory(
            memory_id="mem-cas",
            title="CAS",
            body="protected",
        )
        with self.assertRaises(MemoryVersionError):
            self.service.retract_memory("mem-cas", expected_revision="stale")
        revision = self.service.memory_revision("mem-cas")
        assert revision is not None
        first = self.service.retract_memory("mem-cas", expected_revision=revision)
        history_count = len(self.service.vault.list_markdown("history"))
        second = self.service.retract_memory(
            "mem-cas",
            expected_revision=self.service.memory_revision("mem-cas") or "",
        )
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(len(self.service.vault.list_markdown("history")), history_count)

    def test_retracted_todo_is_not_a_cancelled_or_completed_todo(self) -> None:
        self.service.create_memory(
            memory_id="mem-todo-retracted",
            title="Withdrawn task assertion",
            body="Track this task.",
            type="todo",
            status="active",
        )
        revision = self.service.memory_revision("mem-todo-retracted")
        assert revision is not None
        self.service.retract_memory("mem-todo-retracted", expected_revision=revision)

        self.assertEqual(self.service.list_todos(status="all")["status"], "no_match")

    def test_structured_todo_fields_reach_read_projection_and_content_digest(self) -> None:
        self.service.create_memory(
            memory_id="mem-structured-todo",
            title="Structured task",
            body="Deliver the result.",
            type="todo",
            status="active",
            assignee="agent:primary",
            waiting_on=None,
            due_text="before the release window",
            due_anchor={"source_time": "2026-09-17T09:00:00+08:00"},
        )

        page = self.service.read_page("mem-structured-todo")
        assert page is not None
        self.assertEqual(page["assignee"], "agent:primary")
        self.assertIn("waiting_on", page)
        self.assertIsNone(page["waiting_on"])
        self.assertEqual(page["due_text"], "before the release window")
        self.assertEqual(
            page["due_anchor"],
            {"source_time": "2026-09-17T09:00:00+08:00"},
        )
        self.assertEqual(_read_page_result(page)["assignee"], "agent:primary")

        base = {
            "body": "Deliver the result.",
            "type": "todo",
            "scopes": ["global"],
            "status": "active",
        }
        changed = dict(base, assignee="agent:primary")
        self.assertNotEqual(content_digest(base), content_digest(changed))

    def test_closed_todo_identity_is_not_retired_by_age(self) -> None:
        self.service.create_memory(
            memory_id="mem-closed",
            title="Closed task",
            body="The task is complete.",
            type="todo",
            status="completed",
            completed_at="2020-01-01T00:00:00Z",
        )

        result = RetentionManager(self.service).maintain("2030-01-01T00:00:00Z")

        self.assertEqual(result["closed_todos_retired"], 0)
        current = self.service.read("mem-closed")
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.status, "completed")
        self.assertEqual(
            self.service.list_todos(status="completed")["results"][0]["memory_id"],
            "mem-closed",
        )


if __name__ == "__main__":
    unittest.main()
