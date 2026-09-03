from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.host_runtime import HostRuntime
from memleaf.index import event_key
from memleaf.mcp_server import _invoke_tool
from memleaf.retrieval_gate import todo_filter_key, validate_turn


class QueueBackend:
    provider = "fake"
    model = "global-todo-acceptance"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def complete(self, prompt: str, *, system: str = "", purpose: str = "", temperature: float = 0.0) -> str:
        del prompt, system, purpose, temperature
        if not self.responses:
            raise AssertionError("global todo acceptance model queue exhausted")
        return self.responses.pop(0)


def _write_todos(vault: Path, host: str, session_id: str, titles: list[str]) -> list[str]:
    turn_id = "turn-1"
    evidence = event_key(f"{host}/{session_id}/{turn_id}/user")
    candidates = []
    summaries = []
    for index, title in enumerate(titles):
        candidates.append(
            {
                "candidate_id": f"todo-{index}",
                "memory": f"{title}需要完成。",
                "evidence_event_ids": [evidence],
                "duplicate": False,
                "worth": True,
                "type": "todo",
                "scopes": ["global"],
                "scope_source": "model",
            }
        )
        summaries.append(
            json.dumps(
                {
                    "title": title,
                    "body": f"{title}需要完成。",
                    "tags": ["acceptance"],
                    "type": "todo",
                    "scopes": ["global"],
                    "scope_source": "model",
                    "sources": [{"event_key": evidence}],
                    "status": "active",
                },
                ensure_ascii=False,
            )
        )
    service = Memleaf(vault, model=QueueBackend([json.dumps({"candidates": candidates}, ensure_ascii=False), *summaries]))
    runtime = HostRuntime(service, host)
    opened = runtime.open_turn(
        session_id=session_id,
        turn_id=turn_id,
        user_content="；".join(f"{title}需要完成" for title in titles),
    )
    assert runtime.observe_search(
        session_id=session_id,
        turn_id=turn_id,
        status="no_match",
        call_id=f"{host}-writer-search",
        supplied_retrieval_id=opened.retrieval_id,
    )
    completed = runtime.complete_turn(
        session_id=session_id,
        turn_id=turn_id,
        assistant_content="已确认这些未完成事项。",
        auto_process=True,
    )
    assert not completed.retry_required
    assert not completed.process_failed
    results = service.list_todos()
    ids_by_title = {item["title"]: item["memory_id"] for item in results["results"]}
    return [ids_by_title[title] for title in titles]


class GlobalTodoAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="memleaf-global-todo-acceptance-")
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name) / "vault"

    def test_fresh_hermes_session_reads_all_five_todos_created_by_session_a(self) -> None:
        titles = [f"跨会话待办 {index}" for index in range(5)]
        expected_ids = _write_todos(self.vault, "hermes", "session-a", titles)

        reader_service = Memleaf(self.vault)
        reader = HostRuntime(reader_service, "hermes")
        opened = reader.open_turn(
            session_id="session-b",
            turn_id="turn-1",
            user_content="我现在有什么待办？",
        )
        listed = _invoke_tool(
            reader_service,
            "list_todos",
            {"retrieval_id": opened.retrieval_id},
            request_id=100,
        )
        self.assertFalse(listed["isError"], listed)
        directory = listed["structuredContent"]
        self.assertEqual(directory["status"], "found")
        self.assertFalse(directory["has_more"])
        self.assertEqual({item["memory_id"] for item in directory["results"]}, set(expected_ids))

        bodies = []
        for index, memory_id in enumerate(expected_ids, start=101):
            result = _invoke_tool(
                reader_service,
                "read",
                {"memory_id": memory_id, "retrieval_id": opened.retrieval_id},
                request_id=index,
            )
            self.assertFalse(result["isError"], result)
            bodies.append(result["structuredContent"]["body"])
        self.assertEqual(len(bodies), 5)
        self.assertEqual(validate_turn(self.vault, opened.retrieval_id)["read_count"], 5)

    def test_hermes_and_codex_immediately_share_same_todo_memory_ids(self) -> None:
        hermes_ids = _write_todos(self.vault, "hermes", "hermes-writer", ["Hermes 写入待办"])
        codex_reader = Memleaf(self.vault)
        codex_results = codex_reader.list_todos()
        self.assertIn(hermes_ids[0], {item["memory_id"] for item in codex_results["results"]})
        self.assertEqual(codex_reader.read(hermes_ids[0]).memory_id, hermes_ids[0])

        codex_ids = _write_todos(self.vault, "codex", "codex-writer", ["Codex 写入待办"])
        hermes_reader = Memleaf(self.vault)
        hermes_results = hermes_reader.list_todos()
        visible_ids = {item["memory_id"] for item in hermes_results["results"]}
        self.assertIn(hermes_ids[0], visible_ids)
        self.assertIn(codex_ids[0], visible_ids)
        self.assertEqual(hermes_reader.read(codex_ids[0]).memory_id, codex_ids[0])

    def test_completed_update_reuses_id_and_disappears_from_active_todos(self) -> None:
        title = "完成状态待办"
        memory_id = _write_todos(self.vault, "hermes", "session-create", [title])[0]

        session_id = "session-complete"
        turn_id = "turn-1"
        evidence = event_key(f"hermes/{session_id}/{turn_id}/user")
        gate = json.dumps(
            {
                "candidates": [
                    {
                        "candidate_id": "complete-existing",
                        "memory": f"{title}已完成。",
                        "evidence_event_ids": [evidence],
                        "duplicate": False,
                        "worth": True,
                        "type": "todo",
                        "scopes": ["global"],
                        "scope_source": "model",
                        "update_memory_id": memory_id,
                    }
                ]
            },
            ensure_ascii=False,
        )
        summary = json.dumps(
            {
                "title": title,
                "body": f"{title}已完成。",
                "tags": ["acceptance"],
                "type": "todo",
                "scopes": ["global"],
                "scope_source": "model",
                "sources": [{"event_key": evidence}],
                "update_memory_id": memory_id,
                "status": "completed",
                "completed_at": "2026-09-03T02:00:00Z",
            },
            ensure_ascii=False,
        )
        service = Memleaf(self.vault, model=QueueBackend([gate, summary]))
        runtime = HostRuntime(service, "hermes")
        opened = runtime.open_turn(
            session_id=session_id,
            turn_id=turn_id,
            user_content=f"{title}已经完成了。",
        )
        self.assertTrue(
            runtime.observe_search(
                session_id=session_id,
                turn_id=turn_id,
                status="found",
                call_id="completion-search",
                supplied_retrieval_id=opened.retrieval_id,
            )
        )
        completed = runtime.complete_turn(
            session_id=session_id,
            turn_id=turn_id,
            assistant_content="已更新完成状态。",
            auto_process=True,
        )
        self.assertFalse(completed.process_failed)
        current = service.read(memory_id)
        self.assertIsNotNone(current)
        self.assertEqual(current.memory_id, memory_id)
        self.assertEqual(current.status, "completed")
        self.assertNotIn(memory_id, {item["memory_id"] for item in service.list_todos()["results"]})
        history = [record.memory for record in service._read_memories_unlocked("history")]
        self.assertTrue(any(item.extra.get("active_memory_id") == memory_id and (item.status or "active") == "active" for item in history))

    def test_codex_completion_gate_requires_all_todo_pages(self) -> None:
        service = Memleaf(self.vault)
        for index in range(25):
            service.create_memory(
                memory_id=f"paged-todo-{index:02}",
                title=f"分页待办 {index:02}",
                body=f"分页待办 {index:02} 正文",
                type="todo",
                status="active",
                scopes=["global"],
            )
        runtime = HostRuntime(service, "codex")
        opened = runtime.open_turn(
            session_id="codex-reader",
            turn_id="turn-1",
            user_content="当前所有未完成工作是什么？",
        )
        arguments = {"status": "active", "limit": 20}
        first = service.list_todos(**arguments)
        self.assertTrue(first["has_more"])
        self.assertTrue(
            runtime.observe_todo_list(
                session_id="codex-reader",
                turn_id="turn-1",
                status=first["status"],
                call_id="todos-page-1",
                supplied_retrieval_id=opened.retrieval_id,
                filter_key=todo_filter_key(arguments),
                cursor=None,
                has_more=first["has_more"],
                next_cursor=first["next_cursor"],
            )
        )
        blocked = runtime.complete_turn(
            session_id="codex-reader",
            turn_id="turn-1",
            assistant_content=None,
            auto_process=False,
        )
        self.assertTrue(blocked.retry_required)
        self.assertIn("next_cursor", blocked.retry_reason or "")

        second_args = {**arguments, "cursor": first["next_cursor"]}
        second = service.list_todos(**second_args)
        self.assertFalse(second["has_more"])
        self.assertTrue(
            runtime.observe_todo_list(
                session_id="codex-reader",
                turn_id="turn-1",
                status=second["status"],
                call_id="todos-page-2",
                supplied_retrieval_id=opened.retrieval_id,
                filter_key=todo_filter_key(second_args),
                cursor=first["next_cursor"],
                has_more=second["has_more"],
                next_cursor=second["next_cursor"],
            )
        )
        allowed = runtime.complete_turn(
            session_id="codex-reader",
            turn_id="turn-1",
            assistant_content=None,
            auto_process=False,
        )
        self.assertFalse(allowed.retry_required)
        state = validate_turn(self.vault, opened.retrieval_id)
        self.assertFalse(state["todo_list_pending"])
        self.assertEqual(state["todo_list_pages"], 2)


if __name__ == "__main__":
    unittest.main()
