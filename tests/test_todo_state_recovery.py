import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.index import event_key


class QueueBackend:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        self.calls.append({"prompt": prompt, "purpose": purpose})
        return self.responses.pop(0)


class TodoStateRecoveryTest(unittest.TestCase):
    @staticmethod
    def gate(candidates):
        return json.dumps({"candidates": candidates}, ensure_ascii=False)

    @staticmethod
    def summary(event_key_value):
        # Deliberately omit update_memory_id/status/completed_at.  The user
        # declaration is the source of those deterministic state fields.
        return json.dumps(
            {
                "title": "鑫元基金架构评审文档修改与反馈",
                "body": "架构评审文档已完成并发出。",
                "tags": ["todo"],
                "type": "todo",
                "scopes": ["project:鑫元基金"],
                "scope_source": "model",
                "sources": [{"event_key": event_key_value}],
            },
            ensure_ascii=False,
        )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"

    def tearDown(self):
        self.tmp.cleanup()

    def add_todo(self, service, memory_id="todo-1", title="鑫元基金架构评审文档修改与反馈"):
        return service.create_memory(
            memory_id=memory_id,
            title=title,
            body="需按要求修改并反馈。",
            tags=["todo"],
            type="todo",
            scopes=["project:鑫元基金"],
            scope_source="model",
        )

    def add_turn(self, service, user, *, assistant="已收到。"):
        service.capture("hermes", "session", "turn-1", "user", user, event_id="todo-user")
        service.capture("hermes", "session", "turn-1", "assistant", assistant, event_id="todo-assistant")
        return event_key("todo-user")

    def test_gate_empty_recovers_completion_in_place_and_anchors_timestamp(self):
        backend = QueueBackend([])
        service = Memleaf(self.vault, model=backend)
        old = self.add_todo(service)
        with patch("memleaf.capture._timestamp", return_value="2026-09-02T09:21:51Z"):
            user_key = self.add_turn(
                service,
                "鑫元基金的架构评审文档我已经完成了，发给他们了。还有什么事情要完成",
            )
        backend.responses.extend([self.gate([]), self.summary(user_key)])

        result = service.process(source="hermes", session_id="session", model=backend)

        self.assertEqual(result["memory_ids"], [old.memory_id])
        active = service.read(old.memory_id)
        self.assertEqual(active.status, "completed")
        self.assertEqual(active.completed_at, "2026-09-02T09:21:51Z")
        self.assertEqual(len(service._read_memories_unlocked("knowledge")), 1)
        history = service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.extra["active_memory_id"], old.memory_id)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize"])

    def assert_no_transition(self, text, *, assistant="已收到。", name="no-transition"):
        backend = QueueBackend([self.gate([])])
        service = Memleaf(Path(self.tmp.name) / name, model=backend)
        old = self.add_todo(service)
        self.add_turn(service, text, assistant=assistant)
        result = service.process(source="hermes", session_id="session", model=backend)
        active = service.read(old.memory_id)
        self.assertEqual(result["memories_written"], 0)
        self.assertIn(active.status, (None, "active"))
        self.assertEqual(service._read_memories_unlocked("history"), [])
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate"])

    def test_query_does_not_complete(self):
        self.assert_no_transition("鑫元基金的架构评审文档完成了吗？", name="query")

    def test_confirmation_and_uncertain_completion_do_not_complete(self):
        for index, text in enumerate(
            (
                "鑫元基金的架构评审文档已经完成了，对吗？",
                "鑫元基金的架构评审文档应该已经完成了",
                "鑫元基金的架构评审文档可能已经完成了",
                "鑫元基金的架构评审文档是否已经完成？",
            )
        ):
            with self.subTest(text=text):
                self.assert_no_transition(text, name=f"confirmation-{index}")

    def test_negative_and_future_statements_do_not_complete(self):
        for index, text in enumerate(("鑫元基金的架构评审文档还没完成", "鑫元基金的架构评审文档准备完成", "鑫元基金的架构评审文档我会完成")):
            with self.subTest(text=text):
                self.assert_no_transition(text, name=f"future-{index}")

    def test_unfinished_delivery_actions_do_not_complete(self):
        for index, text in enumerate(
            (
                "鑫元基金需要提交架构评审方案",
                "鑫元基金等待反馈",
                "鑫元基金请发送材料",
            )
        ):
            with self.subTest(text=text):
                self.assert_no_transition(text, name=f"unfinished-action-{index}")

    def test_assistant_only_completion_does_not_complete(self):
        self.assert_no_transition(
            "鑫元基金的架构评审文档现在是什么状态？",
            assistant="鑫元基金的架构评审文档已经完成了。",
            name="assistant-only",
        )

    def test_ambiguous_same_topic_todos_do_not_guess(self):
        backend = QueueBackend([self.gate([])])
        service = Memleaf(Path(self.tmp.name) / "ambiguous", model=backend)
        self.add_todo(service, memory_id="todo-a")
        service.create_memory(
            memory_id="todo-b",
            title="鑫元基金架构评审文档修改与反馈",
            body="需按另一份要求修改并反馈。",
            tags=["todo"],
            type="todo",
            scopes=["project:鑫元基金"],
            scope_source="model",
        )
        self.add_turn(service, "鑫元基金的架构评审文档我已经完成了。")

        result = service.process(source="hermes", session_id="session", model=backend)

        self.assertEqual(result["memories_written"], 0)
        self.assertTrue(all(item.memory.status in (None, "active") for item in service._read_memories_unlocked("knowledge")))
        self.assertEqual(service._read_memories_unlocked("history"), [])

    def test_cancelled_state_is_recovered_in_place(self):
        backend = QueueBackend([])
        service = Memleaf(Path(self.tmp.name) / "cancelled", model=backend)
        old = self.add_todo(service)
        with patch("memleaf.capture._timestamp", return_value="2026-09-02T09:21:51Z"):
            user_key = self.add_turn(service, "鑫元基金的架构评审文档不用做了。")
        backend.responses.extend([self.gate([]), self.summary(user_key)])

        result = service.process(source="hermes", session_id="session", model=backend)

        self.assertEqual(result["memory_ids"], [old.memory_id])
        self.assertEqual(service.read(old.memory_id).status, "cancelled")
        self.assertIsNone(service.read(old.memory_id).completed_at)

    def test_completed_delivery_and_customer_rework_are_separate_writes(self):
        backend = QueueBackend([])
        service = Memleaf(Path(self.tmp.name) / "completion-rework", model=backend)
        config = service.vault.config()
        config["scopes"] = {"project:鑫元基金": {"aliases": ["鑫元"]}}
        save_config(service.vault.config_path, config)
        old = self.add_todo(service)
        with patch("memleaf.capture._timestamp", return_value="2026-09-03T09:21:51Z"):
            user_key = self.add_turn(
                service,
                "鑫元基金我已经给了版了，他们上午回复说还是有两个地方要改，"
                "网络拓扑图没画好，数据架构分层之间访问协议要标出来",
            )
        backend.responses.extend(
            [
                self.gate([]),
                self.summary(user_key),
                json.dumps(
                    {
                        "title": "鑫元基金客户返工要求",
                        "body": "客户要求重新绘制网络拓扑图，并标明数据架构分层之间的访问协议。",
                        "tags": ["todo"],
                        "type": "todo",
                        "scopes": ["project:鑫元基金"],
                        "scope_source": "model",
                        "sources": [{"event_key": user_key}],
                    },
                    ensure_ascii=False,
                ),
            ]
        )

        result = service.process(source="hermes", session_id="session", model=backend)

        self.assertEqual(result["memories_written"], 2)
        self.assertEqual(service.read(old.memory_id).status, "completed")
        self.assertEqual(service.read(old.memory_id).completed_at, "2026-09-03T09:21:51Z")
        active = service._read_memories_unlocked("knowledge")
        rework = [record.memory for record in active if record.memory.memory_id != old.memory_id]
        self.assertEqual(len(rework), 1)
        self.assertEqual(rework[0].type, "todo")
        self.assertEqual(rework[0].status, "active")
        self.assertIn("网络拓扑图", rework[0].body)
        self.assertEqual(
            [call["purpose"] for call in backend.calls],
            ["gate", "summarize", "summarize"],
        )

        replay = service.process(source="hermes", session_id="session", model=backend)

        self.assertEqual(replay["processed_turns"], 0)
        self.assertEqual(replay["memories_written"], 0)
        self.assertEqual(service.read(old.memory_id).status, "completed")
        self.assertEqual(
            len(service._read_memories_unlocked("knowledge")),
            2,
        )
        self.assertEqual(
            [call["purpose"] for call in backend.calls],
            ["gate", "summarize", "summarize"],
        )

    def test_generic_customer_rework_candidate_is_reused_without_network_specific_rules(self):
        backend = QueueBackend([])
        service = Memleaf(Path(self.tmp.name) / "generic-rework", model=backend)
        config = service.vault.config()
        config["scopes"] = {"project:鑫元基金": {"aliases": ["鑫元"]}}
        save_config(service.vault.config_path, config)
        old = self.add_todo(service)
        with patch("memleaf.capture._timestamp", return_value="2026-09-03T09:21:51Z"):
            user_key = self.add_turn(
                service,
                "鑫元基金的文档已经交付了，客户要求修改封面并补充审批流程。",
            )
        gate_candidate = {
            "candidate_id": "customer-rework",
            "memory": "鑫元基金客户返工：修改封面并补充审批流程",
            "evidence_event_ids": [user_key],
            "duplicate": False,
            "worth": True,
            "type": "todo",
            "scopes": ["project:鑫元基金"],
            "scope_source": "model",
        }
        backend.responses.extend(
            [
                self.gate([gate_candidate]),
                self.summary(user_key),
                json.dumps(
                    {
                        "title": "鑫元基金客户返工要求",
                        "body": "客户要求修改封面并补充审批流程。",
                        "tags": ["todo"],
                        "type": "todo",
                        "scopes": ["project:鑫元基金"],
                        "scope_source": "model",
                        "sources": [{"event_key": user_key}],
                    },
                    ensure_ascii=False,
                ),
            ]
        )

        result = service.process(source="hermes", session_id="session", model=backend)

        self.assertEqual(result["memories_written"], 2)
        self.assertEqual(service.read(old.memory_id).status, "completed")
        active = service._read_memories_unlocked("knowledge")
        rework = [record.memory for record in active if record.memory.memory_id != old.memory_id]
        self.assertEqual(len(rework), 1)
        self.assertIn("修改封面", rework[0].body)
        self.assertIn("审批流程", rework[0].body)
        self.assertEqual(result["deferred_candidates"], 0)


if __name__ == "__main__":
    unittest.main()
