from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf
from memleaf.index import event_key
from memleaf.validation import is_mixed_future_use_text


class QueueBackend:
    provider = "fake"
    model = "cross-turn-dedupe-regression"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        del temperature
        self.calls.append({"prompt": prompt, "system": system, "purpose": purpose})
        if not self.responses:
            raise AssertionError("test backend response queue exhausted")
        return self.responses.pop(0)


def gate(candidates):
    return json.dumps({"candidates": candidates}, ensure_ascii=False)


def candidate(candidate_id, evidence, memory, *, type="project", worth=True, scopes=None):
    return {
        "candidate_id": candidate_id,
        "memory": memory,
        "evidence_event_ids": list(evidence),
        "duplicate": False,
        "worth": worth,
        "type": type if worth else None,
        "scopes": list(scopes or ["project:alpha"]),
        "scope_source": "model",
    }


def summary(event, *, title, body, type="project", scopes=None, update_memory_id=None):
    value = {
        "title": title,
        "body": body,
        "tags": ["regression"],
        "type": type,
        "scopes": list(scopes or ["project:alpha"]),
        "scope_source": "model",
        "sources": [{"event_key": event}],
    }
    if update_memory_id is not None:
        value["update_memory_id"] = update_memory_id
    return json.dumps(value, ensure_ascii=False)


class CrossTurnDedupeRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="memleaf-cross-turn-")
        self.service = Memleaf(Path(self.tempdir.name) / "vault")

    def tearDown(self):
        self.tempdir.cleanup()

    def capture(self, session, user, assistant="已确认。"):
        user_id = f"{session}-user"
        assistant_id = f"{session}-assistant"
        self.service.capture("hermes", session, "turn-1", "user", user, event_id=user_id)
        self.service.capture("hermes", session, "turn-1", "assistant", assistant, event_id=assistant_id)
        return event_key(user_id), event_key(assistant_id)

    def active(self):
        return [record.memory for record in self.service._read_memories_unlocked("knowledge")]

    def test_dated_todo_plus_future_document_rule_is_mixed_future_use(self):
        text = (
            "alpha 项目架构评审文档修改仍需在2026-09-03前完成，"
            "后续需求文档直接发送联系人甲、联系人乙，抄送联系人丙，并确认附件。"
        )
        self.assertTrue(is_mixed_future_use_text(text))

    def test_existing_todo_no_change_and_new_document_flow_create_separately(self):
        existing = self.service.create_memory(
            memory_id="mem-existing-todo",
            title="alpha 项目架构评审文档修改",
            body="alpha 项目架构评审文档需在2026-09-03前修改完成。",
            tags=["alpha", "todo"],
            type="todo",
            scopes=["project:alpha"],
        )
        user, assistant = self.capture(
            "split-old-new",
            (
                "alpha 项目架构评审文档修改仍按2026-09-03截止，没有变化，"
                "后续需求文档直接发送联系人甲、联系人乙，抄送联系人丙，并确认附件。"
            ),
        )
        mixed = candidate(
            "mixed",
            [user, assistant],
            (
                "alpha 项目架构评审文档修改仍按2026-09-03截止；"
                "后续需求文档直接发送联系人甲、联系人乙，抄送联系人丙，并确认附件。"
            ),
            type="project",
        )
        old_no_change = candidate(
            "old-no-change",
            [user],
            "alpha 项目架构评审文档修改仍按2026-09-03截止，没有变化。",
            worth=False,
        )
        new_flow = candidate(
            "new-document-flow",
            [user],
            "alpha 项目后续需求文档直接发送联系人甲、联系人乙，抄送联系人丙，并确认附件。",
            type="project",
        )
        backend = QueueBackend(
            [
                gate([mixed]),
                gate([old_no_change, new_flow]),
                summary(
                    user,
                    title="alpha 项目需求文档收发流程",
                    body="alpha 项目后续需求文档直接发送联系人甲、联系人乙，抄送联系人丙，并确认附件。",
                    type="project",
                ),
            ]
        )

        result = self.service.process(source="hermes", session_id="split-old-new", model=backend)

        self.assertEqual(result["memories_written"], 1)
        memories = self.active()
        self.assertEqual(len(memories), 2)
        self.assertIn(existing.memory_id, {memory.memory_id for memory in memories})
        new = next(memory for memory in memories if memory.memory_id != existing.memory_id)
        self.assertIn("需求文档", new.body)
        self.assertNotIn("2026-09-03", new.body)
        self.assertEqual([], self.service._read_memories_unlocked("history"))
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "summarize"])

    def test_create_is_deferred_when_related_active_memory_already_covers_same_use(self):
        existing = self.service.create_memory(
            memory_id="mem-canonical-project",
            title="alpha 项目迁移要求",
            body="alpha 项目需逐项确认历史需求，提前部署测试环境，并迁移历史数据库和全部附件。",
            tags=["alpha", "migration"],
            type="project",
            scopes=["project:alpha"],
        )
        user, assistant = self.capture(
            "duplicate-create-guard",
            "alpha 项目仍需逐项确认历史需求，提前部署测试环境，并迁移历史数据库和全部附件。",
        )
        duplicate_candidate = candidate(
            "duplicate-project",
            [user, assistant],
            "alpha 项目需逐项确认历史需求，提前部署测试环境，并迁移历史数据库和全部附件。",
            type="project",
        )
        backend = QueueBackend(
            [
                gate([duplicate_candidate]),
                # Simulate a non-converging summarizer that incorrectly tries
                # to CREATE a sibling instead of returning NO_CHANGE.
                summary(
                    user,
                    title="alpha 项目历史迁移工作",
                    body="alpha 项目需逐项确认历史需求，提前部署测试环境，并迁移历史数据库和全部附件。",
                    type="project",
                ),
            ]
        )

        result = self.service.process(
            source="hermes",
            session_id="duplicate-create-guard",
            model=backend,
            scope="project:alpha",
        )

        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["deferred_candidates"], 0)
        self.assertEqual([memory.memory_id for memory in self.active()], [existing.memory_id])
        self.assertEqual([], self.service._read_memories_unlocked("history"))


if __name__ == "__main__":
    unittest.main()
