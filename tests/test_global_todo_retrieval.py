from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.mcp_server import _invoke_tool
from memleaf.retrieval_gate import begin_turn, observe_search, observe_todo_list, todo_filter_key, validate_turn


class GlobalTodoRetrievalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="memleaf-global-todo-")
        self.addCleanup(temporary.cleanup)
        self.service = Memleaf(Path(temporary.name) / "vault")

    def todo(self, memory_id, title, *, due_date=None, status="active", scope="global", source="hermes", session="a"):
        return self.service.create_memory(
            memory_id=memory_id,
            title=title,
            body=f"{title} 正文",
            type="todo",
            scopes=[scope],
            status=status,
            due_date=due_date,
            sources=[{"session_id": session, "turn_id": "turn-1", "source": source}],
        )

    def test_global_list_ignores_source_session_and_scope_by_default(self):
        expected = set()
        for index, (scope, source, session) in enumerate([
            ("project:摩根基金", "hermes", "a"),
            ("project:金元顺安", "codex", "b"),
            ("project:鑫元基金", "other", "c"),
            ("project:百年保险", "hermes", "d"),
            ("global", "codex", "e"),
        ]):
            memory = self.todo(f"todo-{index}", f"任务 {index}", scope=scope, source=source, session=session)
            expected.add(memory.memory_id)
        result = self.service.list_todos()
        self.assertEqual(result["status"], "found")
        self.assertEqual({item["memory_id"] for item in result["results"]}, expected)

    def test_status_latest_active_and_history_are_separate(self):
        active = self.todo("todo-state", "状态任务", due_date="2026-09-03")
        self.service.create_memory(memory_id="history-copy", title="旧状态", body="旧 active", type="todo", status="active", due_date="2026-09-02", area="history")
        result = self.service.list_todos()
        self.assertIn(active.memory_id, {item["memory_id"] for item in result["results"]})
        self.assertNotIn("history-copy", {item["memory_id"] for item in result["results"]})
        active.status = "completed"
        active.completed_at = "2026-09-03T01:00:00Z"
        self.service.write_memory(active)
        self.assertNotIn(active.memory_id, {item["memory_id"] for item in self.service.list_todos()["results"]})

    def test_pagination_is_complete_and_stale_when_active_changes(self):
        for index in range(45):
            self.todo(f"todo-page-{index:02}", f"分页任务 {index:02}")
        seen = set()
        page = self.service.list_todos(limit=20)
        first_cursor = page["next_cursor"]
        while True:
            for item in page["results"]:
                self.assertNotIn(item["memory_id"], seen)
                seen.add(item["memory_id"])
            if not page["has_more"]:
                break
            page = self.service.list_todos(limit=20, cursor=page["next_cursor"])
        self.assertEqual(len(seen), 45)
        self.todo("todo-page-new", "新增任务")
        with self.assertRaises(Exception) as raised:
            self.service.list_todos(limit=20, cursor=first_cursor)
        self.assertIn(getattr(raised.exception, "code", ""), {"stale_cursor", "invalid_cursor"})

    def test_mcp_list_todos_allows_read_and_read_metadata(self):
        memory = self.todo("todo-mcp", "MCP 待办", due_date="2026-09-03")
        retrieval_id = begin_turn(self.service.vault, "codex", "session", "turn")
        arguments = {"status": "active", "retrieval_id": retrieval_id}
        result = _invoke_tool(self.service, "list_todos", arguments, request_id=1)
        self.assertFalse(result["isError"], result)
        # Codex normally records PostToolUse; emulate that host observation here.
        value = result["structuredContent"]
        observe_todo_list(
            self.service.vault,
            retrieval_id,
            value["status"],
            "todo-call",
            filter_key=todo_filter_key(arguments),
            cursor=None,
            has_more=value["has_more"],
            next_cursor=value["next_cursor"],
            current_source="codex",
        )
        read = _invoke_tool(self.service, "read", {"memory_id": memory.memory_id, "retrieval_id": retrieval_id})
        self.assertFalse(read["isError"], read)
        self.assertEqual(read["structuredContent"]["type"], "todo")
        self.assertEqual(read["structuredContent"]["status"], "active")
        self.assertEqual(read["structuredContent"]["due_date"], "2026-09-03")

    def test_due_date_round_trip_and_legacy_todo(self):
        memory = self.todo("todo-date", "日期任务", due_date="2026-09-03")
        loaded = self.service.read(memory.memory_id)
        self.assertEqual(loaded.due_date, "2026-09-03")
        legacy = self.service.create_memory(memory_id="todo-legacy", title="旧待办", body="旧格式", type="todo")
        self.assertIsNone(self.service.read(legacy.memory_id).due_date)
        self.assertEqual(self.service.read(legacy.memory_id).status, None)
        self.assertIn("todo-legacy", {item["memory_id"] for item in self.service.list_todos()["results"]})


if __name__ == "__main__":
    unittest.main()
