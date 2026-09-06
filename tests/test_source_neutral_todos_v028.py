from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memleaf.models import Memory
from memleaf.service import Memleaf


class SourceNeutralTodoOrderingV028Tests(unittest.TestCase):
    def test_unscheduled_todo_order_does_not_parse_urgency_words(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-source-neutral-todos-") as temporary:
            service = Memleaf(Path(temporary) / "vault")
            memories = [
                Memory.new(
                    memory_id="todo-z-urgent",
                    title="Z urgent cleanup",
                    body="Please handle this ASAP and 紧急处理",
                    type="todo",
                    scopes=["global"],
                    status="active",
                    sources=[],
                ),
                Memory.new(
                    memory_id="todo-a-regular",
                    title="A regular cleanup",
                    body="Ordinary unscheduled todo",
                    type="todo",
                    scopes=["global"],
                    status="active",
                    sources=[],
                ),
            ]
            for memory in memories:
                service.vault.memory_path(memory.memory_id, "knowledge").write_text(
                    memory.to_markdown(), encoding="utf-8"
                )
            service.rebuild_index()
            result = service.list_todos(status="active", include_unscheduled=True, limit=20)
            self.assertEqual(result["status"], "found")
            self.assertEqual(
                [item["memory_id"] for item in result["results"]],
                ["todo-a-regular", "todo-z-urgent"],
            )


if __name__ == "__main__":
    unittest.main()
