"""Regression coverage for global todo queries staying read-only.

The global todo list is shared across sessions.  A later session may repeat
the list in its answer, but that repetition is not new evidence and must not
append sources, create a near-duplicate, or update an existing todo.  Actual
user state changes remain eligible for an in-place update.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.index import event_key


from tests.semantic_fixtures import semantic_fixture

@semantic_fixture
class QueueBackend:
    """Deterministic model queue for gate and summarize stages."""

    provider = "fake"
    model = "global-todo-query-no-write"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, str]] = []

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        purpose: str = "",
        temperature: float = 0.0,
    ) -> str:
        del prompt, system, temperature
        self.calls.append({"purpose": purpose})
        if not self.responses:
            raise AssertionError("global todo query model queue exhausted")
        return self.responses.pop(0)


def gate(candidates: list[dict[str, object]]) -> str:
    return json.dumps({"candidates": candidates}, ensure_ascii=False)


def candidate(
    candidate_id: str,
    evidence: list[str],
    memory: str,
    *,
    scope: str,
    duplicate: bool = False,
    worth: bool = True,
    duplicate_memory_id: str | None = None,
    update_memory_id: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "candidate_id": candidate_id,
        "memory": memory,
        "evidence_event_ids": list(evidence),
        "duplicate": duplicate,
        "worth": worth,
        "type": "todo",
        "scopes": [scope],
        "scope_source": "model",
    }
    if duplicate_memory_id is not None:
        value["duplicate_memory_id"] = duplicate_memory_id
    if update_memory_id is not None:
        value["update_memory_id"] = update_memory_id
    return value


def summary(
    event: str,
    *,
    title: str,
    body: str,
    scope: str,
    update_memory_id: str | None = None,
    status: str | None = None,
) -> str:
    value: dict[str, object] = {
        "title": title,
        "body": body,
        "tags": ["global-todo-query"],
        "type": "todo",
        "scopes": [scope],
        "scope_source": "model",
        "sources": [{"event_key": event}],
    }
    if update_memory_id is not None:
        value["update_memory_id"] = update_memory_id
    if status is not None:
        value["status"] = status
    return json.dumps(value, ensure_ascii=False)


class GlobalTodoQueryNoWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="memleaf-global-todo-query-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def service(self, name: str) -> Memleaf:
        service = Memleaf(self.root / name)
        config = service.vault.config()
        config["scopes"] = {
            "project:鑫元基金": {"aliases": ["鑫元"]},
            "project:中银国际": {},
            "project:金元顺安": {},
        }
        save_config(service.vault.config_path, config)
        return service

    @staticmethod
    def capture(
        service: Memleaf,
        *,
        session: str,
        user: str,
        assistant: str,
        turn: str = "turn-1",
    ) -> tuple[str, str]:
        user_id = f"{session}/{turn}/user"
        assistant_id = f"{session}/{turn}/assistant"
        service.capture("hermes", session, turn, "user", user, event_id=user_id)
        service.capture("hermes", session, turn, "assistant", assistant, event_id=assistant_id)
        return event_key(user_id), event_key(assistant_id)

    @staticmethod
    def seed_todo(
        service: Memleaf,
        memory_id: str,
        title: str,
        body: str,
        scope: str,
        *,
        session: str = "session-a",
    ):
        return service.create_memory(
            memory_id=memory_id,
            title=title,
            body=body,
            tags=["todo"],
            type="todo",
            scopes=[scope],
            status="active",
            sources=[{"source": "hermes", "session_id": session, "turn_id": "turn-1"}],
        )

    @staticmethod
    def markdown_snapshot(service: Memleaf) -> dict[str, bytes]:
        snapshot: dict[str, bytes] = {}
        for area in ("knowledge", "history"):
            for path in service.vault.list_markdown(area):
                snapshot[f"{area}/{path.name}"] = path.read_bytes()
        return snapshot

    @staticmethod
    def source_snapshot(service: Memleaf) -> dict[str, list[dict[str, object]]]:
        return {
            record.memory.memory_id: [dict(source) for source in record.memory.sources]
            for record in service._read_memories_unlocked("knowledge")
        }

    def test_global_query_does_not_write_when_gate_returns_update_duplicate_and_create(self) -> None:
        """A todo query is read-only even when the model proposes writes."""

        service = self.service("mixed-gate")
        xinyuan = self.seed_todo(
            service,
            "todo-xinyuan",
            "鑫元基金架构评审文档修改",
            "鑫元基金架构评审文档需在2026-09-03前修改完成。",
            "project:鑫元基金",
        )
        zhongyin = self.seed_todo(
            service,
            "todo-zhongyin",
            "中银国际干系人信息表",
            "中银国际干系人信息表待确认名单。",
            "project:中银国际",
        )
        self.seed_todo(
            service,
            "todo-jinyuan",
            "金元顺安实施计划重排",
            "金元顺安实施计划需要重排后回复客户。",
            "project:金元顺安",
        )
        user_key, assistant_key = self.capture(
            service,
            session="session-b",
            user="有什么是我最近必须完成的么",
            assistant=(
                "我查了一下待办：鑫元基金架构评审文档、中银国际干系人信息表、"
                "金元顺安实施计划重排都需要跟进。"
            ),
        )
        gate_candidates = [
            candidate(
                "query-update",
                [user_key, assistant_key],
                "鑫元基金架构评审文档修改仍需在2026-09-03前完成。",
                scope="project:鑫元基金",
                update_memory_id=xinyuan.memory_id,
            ),
            candidate(
                "query-duplicate",
                [user_key, assistant_key],
                zhongyin.body,
                scope="project:中银国际",
                duplicate=True,
                worth=False,
                duplicate_memory_id=zhongyin.memory_id,
            ),
            candidate(
                "query-create",
                [user_key, assistant_key],
                "金元顺安实施计划重排需要尽快回复客户。",
                scope="project:金元顺安",
            ),
        ]
        backend = QueueBackend(
            [
                gate(gate_candidates),
                summary(
                    user_key,
                    title="鑫元基金架构评审文档修改",
                    body="鑫元基金架构评审文档修改仍需在2026-09-03前完成，待反馈。",
                    scope="project:鑫元基金",
                    update_memory_id=xinyuan.memory_id,
                ),
                summary(
                    user_key,
                    title="金元顺安实施计划重排",
                    body="金元顺安实施计划重排需要尽快回复客户。",
                    scope="project:金元顺安",
                ),
            ]
        )
        before_bytes = self.markdown_snapshot(service)
        before_sources = self.source_snapshot(service)

        result = service.process(source="hermes", session_id="session-b", model=backend)

        self.assertEqual(result["memory_ids"], [])
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["metadata_merged"], 0)
        self.assertEqual(self.markdown_snapshot(service), before_bytes)
        self.assertEqual(self.source_snapshot(service), before_sources)
        self.assertEqual(
            {record.memory.memory_id for record in service._read_memories_unlocked("knowledge")},
            {"todo-xinyuan", "todo-zhongyin", "todo-jinyuan"},
        )
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate"])
        processed = json.loads(service.vault.processed_index_path.read_text(encoding="utf-8"))
        entry = processed["sessions"]["hermes/session-b"]["processed_turns"][0]
        self.assertEqual(
            {
                item["candidate_id"]: (item["disposition"], item.get("reason"))
                for item in entry["candidate_dispositions"]
            },
            {
                "query-update": ("NO_CHANGE", "read_only_query"),
                "query-duplicate": ("NO_CHANGE", "read_only_query"),
                "query-create": ("NO_CHANGE", "read_only_query"),
            },
        )

    def test_cross_session_xinyuan_restatement_near_duplicate_is_noop(self) -> None:
        """A new session cannot turn a shared todo recap into a new version."""

        service = self.service("cross-session")
        existing = self.seed_todo(
            service,
            "todo-xinyuan-cross-session",
            "鑫元基金架构评审文档修改",
            "鑫元基金架构评审文档需在2026-09-03前修改完成。",
            "project:鑫元基金",
            session="session-a",
        )
        user_key, assistant_key = self.capture(
            service,
            session="session-b",
            user="我最近必须完成什么？",
            assistant="鑫元基金架构评审文档修改仍需在2026-09-03前完成。",
        )
        near_duplicate = candidate(
            "cross-session-xinyuan-recap",
            [user_key, assistant_key],
            "鑫元基金架构评审文档修改与反馈仍需在2026-09-03前完成。",
            scope="project:鑫元基金",
        )
        backend = QueueBackend(
            [
                gate([near_duplicate]),
                summary(
                    user_key,
                    title="鑫元基金架构评审文档修改与反馈",
                    body="鑫元基金架构评审文档修改与反馈仍需在2026-09-03前完成。",
                    scope="project:鑫元基金",
                    update_memory_id=existing.memory_id,
                ),
            ]
        )
        before_bytes = self.markdown_snapshot(service)
        before_sources = self.source_snapshot(service)

        result = service.process(source="hermes", session_id="session-b", model=backend)

        self.assertEqual(result["memory_ids"], [])
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(self.markdown_snapshot(service), before_bytes)
        self.assertEqual(self.source_snapshot(service), before_sources)
        self.assertEqual(
            [record.memory.memory_id for record in service._read_memories_unlocked("knowledge")],
            [existing.memory_id],
        )
        self.assertEqual(service._read_memories_unlocked("history"), [])
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate"])

    def test_new_requirement_before_a_question_remains_write_eligible(self) -> None:
        """A real assertion is not suppressed by a later question clause."""

        service = self.service("assertion-before-question")
        user_key, assistant_key = self.capture(
            service,
            session="assertion-before-question",
            user="鑫元基金架构评审文档必须补充数据架构图，什么时候开始？",
            assistant="已记录新增要求。",
        )
        gate_candidate = candidate(
            "new-requirement",
            [user_key, assistant_key],
            "鑫元基金架构评审文档必须补充数据架构图。",
            scope="project:鑫元基金",
        )
        backend = QueueBackend(
            [
                gate([gate_candidate]),
                summary(
                    user_key,
                    title="鑫元基金架构评审文档要求",
                    body="鑫元基金架构评审文档必须补充数据架构图。",
                    scope="project:鑫元基金",
                ),
            ]
        )

        result = service.process(
            source="hermes",
            session_id="assertion-before-question",
            model=backend,
        )

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(len(result["memory_ids"]), 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize"])

    def test_explicit_complete_cancel_and_modify_still_update_same_todo_id(self) -> None:
        """Read-only detection must not suppress explicit state changes."""

        cases = (
            (
                "completed",
                "鑫元基金架构评审文档修改已经完成了。",
                "鑫元基金架构评审文档修改已完成。",
                "completed",
            ),
            (
                "cancelled",
                "鑫元基金架构评审文档修改不用做了。",
                "鑫元基金架构评审文档修改已取消。",
                "cancelled",
            ),
            (
                "modified",
                "鑫元基金架构评审文档修改内容改为补充数据架构图。",
                "鑫元基金架构评审文档修改内容改为补充数据架构图。",
                "active",
            ),
        )
        for name, user, body, expected_status in cases:
            with self.subTest(name=name):
                service = self.service(f"state-{name}")
                existing = self.seed_todo(
                    service,
                    f"todo-state-{name}",
                    "鑫元基金架构评审文档修改",
                    "鑫元基金架构评审文档需按要求修改并反馈。",
                    "project:鑫元基金",
                )
                user_key, assistant_key = self.capture(
                    service,
                    session=f"state-{name}",
                    user=user,
                    assistant="已收到。",
                )
                gate_candidate = candidate(
                    f"state-{name}",
                    [user_key, assistant_key],
                    body,
                    scope="project:鑫元基金",
                    update_memory_id=existing.memory_id,
                )
                backend = QueueBackend(
                    [
                        gate([gate_candidate]),
                        summary(
                            user_key,
                            title="鑫元基金架构评审文档修改",
                            body=body,
                            scope="project:鑫元基金",
                            update_memory_id=existing.memory_id,
                            status=expected_status,
                        ),
                    ]
                )

                result = service.process(
                    source="hermes",
                    session_id=f"state-{name}",
                    model=backend,
                )

                self.assertEqual(result["memory_ids"], [existing.memory_id])
                current = service.read(existing.memory_id)
                self.assertIsNotNone(current)
                self.assertEqual(current.status, expected_status)
                self.assertEqual(
                    [record.memory.memory_id for record in service._read_memories_unlocked("knowledge")],
                    [existing.memory_id],
                )
                self.assertEqual(len(service._read_memories_unlocked("history")), 1)


if __name__ == "__main__":
    unittest.main()
