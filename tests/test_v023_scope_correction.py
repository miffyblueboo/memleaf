from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf
from memleaf.inbox import parse_inbox_text
from memleaf.memory_writer import MemoryWriter
from memleaf.models import Memory
from memleaf.processing import Processor
from memleaf.scope_maintenance import scope_registry_projection
from memleaf.scope_state import project_scopes_for_domains, validate_scope_registry
from memleaf.validation import split_mixed_future_use_text


class V023ScopeCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="memleaf-v023-")
        self.root = Path(self.temp.name)
        self.service = Memleaf.initialize(self.root)
        config = self.service.vault.config()
        config["scopes"] = {
            "project:兴银理财": {"aliases": ["兴银"], "identifiers": ["cibwm.com"]},
            "project:鑫元基金": {"aliases": ["鑫元"], "identifiers": ["xyamc.com"]},
            "project:金元顺安": {"aliases": ["金元"], "identifiers": ["jysa.com"]},
        }
        from memleaf.config import save_config
        save_config(self.service.vault.config_path, config)
        self._turn_counter = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _memory(self, memory_id: str, scope: str, body: str, *, title: str = "流程要求") -> Memory:
        return self.service.create_memory(
            memory_id=memory_id,
            title=title,
            body=body,
            type="project",
            scopes=[scope],
            tags=["流程"],
        )

    def _turn(self, user: str):
        self._turn_counter += 1
        turn_id = f"t-{self._turn_counter}"
        self.service.capture("hermes", "s", turn_id, "user", user, event_id=f"u-{self._turn_counter}")
        self.service.capture("hermes", "s", turn_id, "assistant", "收到", event_id=f"a-{self._turn_counter}")
        text = self.service.vault.session_path("hermes", "s").read_text(encoding="utf-8")
        turns = [turn for turn in parse_inbox_text(text, source="hermes", session_id="s") if turn.complete]
        return turns[-1]

    def test_scope_identifiers_are_private_and_domain_resolves_project(self) -> None:
        config = self.service.vault.config()
        registry = validate_scope_registry(config["scopes"])
        self.assertEqual(["xyamc.com"], registry["project:鑫元基金"]["identifiers"])
        self.assertEqual(["project:鑫元基金"], project_scopes_for_domains(["mail.xyamc.com"], config))
        projection = scope_registry_projection(config)
        self.assertTrue(all("identifiers" not in item for item in projection))

    def test_bounded_tool_evidence_round_trips_without_becoming_content(self) -> None:
        self.service.capture("hermes", "mail", "t1", "user", "看一下邮件", event_id="u1")
        self.service.capture(
            "hermes", "mail", "t1", "assistant", "邮件已检查", event_id="a1",
            tool_evidence=[{"message_id": "42", "subject": "流程", "sender": "x <a@xyamc.com>", "domain": "xyamc.com"}],
        )
        path = self.service.vault.session_path("hermes", "mail")
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("a@xyamc.com", "邮件已检查")
        turn = next(turn for turn in parse_inbox_text(text, source="hermes", session_id="mail") if turn.complete)
        assistant = next(event for event in turn.events if event.role == "assistant")
        self.assertEqual("xyamc.com", assistant.tool_evidence[0]["domain"])
        self.assertEqual("邮件已检查", assistant.content)

    def test_explicit_scope_correction_authorizes_old_target_only(self) -> None:
        old = self._memory("mem-wrong", "project:兴银理财", "流程要求使用双人复核")
        turn = self._turn("之前归错客户了，不是兴银理财，是鑫元基金；流程要求仍是使用双人复核。")
        candidate = {
            "candidate_id": "c1", "memory": "鑫元基金流程要求使用双人复核", "worth": True,
            "duplicate": False, "type": "project", "scopes": ["project:鑫元基金"],
            "scope_source": "model", "update_memory_id": old.memory_id, "evidence_event_ids": list(turn.event_keys),
        }
        processor = Processor(self.service)
        plan = processor._scope_correction_plan(candidate, turn, self.service.vault.config())
        self.assertIsNotNone(plan)
        self.assertEqual(old.memory_id, plan["target_memory_id"])
        self.assertEqual("project:鑫元基金", plan["new_scope"])

        no_correction = self._turn("鑫元基金流程要求使用双人复核。")
        self.assertIsNone(processor._scope_correction_plan(candidate, no_correction, self.service.vault.config()))
        self.assertEqual("NOT_RELATED", processor._target_relation(candidate, turn=no_correction))

    def test_explicit_scope_correction_recovers_unique_old_target_when_model_omits_id(self) -> None:
        old = self._memory("mem-targetless", "project:兴银理财", "流程要求使用双人复核")
        turn = self._turn("之前归错客户了，不是兴银理财，是鑫元基金；流程要求仍是使用双人复核。")
        candidate = {
            "candidate_id": "targetless", "memory": "鑫元基金流程要求使用双人复核", "worth": True,
            "duplicate": False, "type": "project", "scopes": ["project:鑫元基金"],
            "scope_source": "model", "evidence_event_ids": list(turn.event_keys),
        }
        plan = Processor(self.service)._scope_correction_plan(candidate, turn, self.service.vault.config())
        self.assertIsNotNone(plan)
        self.assertEqual(old.memory_id, plan["target_memory_id"])
        self.assertFalse(plan["ambiguous"])
        self.assertFalse(plan["unresolved"])

        self._memory("mem-targetless-2", "project:兴银理财", "流程要求使用双人复核并留痕")
        ambiguous = Processor(self.service)._scope_correction_plan(candidate, turn, self.service.vault.config())
        self.assertIsNotNone(ambiguous)
        self.assertTrue(ambiguous["ambiguous"])
        self.assertTrue(ambiguous["unresolved"])
        self.assertIsNone(ambiguous["target_memory_id"])

    def test_existing_correct_survivor_retires_wrong_active_to_history(self) -> None:
        wrong = self._memory("mem-wrong2", "project:兴银理财", "流程要求使用双人复核")
        correct = self._memory("mem-correct2", "project:鑫元基金", "鑫元基金流程要求使用双人复核")
        turn = self._turn("之前归错客户了，不是兴银理财，是鑫元基金；流程要求使用双人复核。")
        request = {
            "summary": {
                "title": correct.title, "body": correct.body, "tags": list(correct.tags), "type": correct.type,
                "scopes": list(correct.scopes), "scope_source": "model", "sources": [], "scope_operations": [],
            },
            "turn": turn, "candidate_id": "c2", "memory_id": correct.memory_id,
            "event_key": turn.event_keys[0], "turn_id": "", "conversation_title": "test", "explicit_remember": False,
            "native_refs": [],
            "scope_correction": {
                "target_memory_id": wrong.memory_id, "survivor_memory_id": correct.memory_id,
                "old_scope": "project:兴银理财", "new_scope": "project:鑫元基金", "ambiguous": False,
            },
        }
        with self.service.vault.lock():
            written = MemoryWriter(self.service).write_many_unlocked([request], now="2026-09-03T08:00:00Z")
            self.service._rebuild_index_unlocked()
        self.assertEqual(correct.memory_id, written[0].memory_id)
        active_ids = {m.memory_id for m in self.service.search("流程", include_history=False)}
        self.assertNotIn(wrong.memory_id, active_ids)
        self.assertIn(correct.memory_id, active_ids)
        history = [r.memory for r in self.service._read_memories_unlocked("history")]
        archived = next(item for item in history if item.extra.get("active_memory_id") == wrong.memory_id)
        self.assertEqual("scope_correction", archived.extra.get("invalidated_reason"))
        self.assertEqual(correct.memory_id, archived.extra.get("superseded_by"))

    def test_same_project_mixed_future_use_splits_only_when_every_clause_is_classifiable(self) -> None:
        split = split_mixed_future_use_text(
            "金元顺安实施计划要求后续交付都保留回滚方案；同时需要按6点调整实施计划并提交反馈"
        )
        self.assertIsNotNone(split)
        self.assertEqual({"project", "todo"}, {kind for _text, kind in split})
        self.assertIsNone(split_mixed_future_use_text("金元顺安实施计划后续按规范执行；另外还有一些事情"))

    def test_unique_mail_domain_conflict_is_detected_without_scope_map_exposure(self) -> None:
        self.service.capture("hermes", "evidence", "t2", "user", "处理这封邮件", event_id="eu")
        self.service.capture(
            "hermes", "evidence", "t2", "assistant", "已查看", event_id="ea",
            tool_evidence=[{"message_id": "99", "sender": "pm@xyamc.com", "domain": "xyamc.com"}],
        )
        text = self.service.vault.session_path("hermes", "evidence").read_text(encoding="utf-8")
        turn = next(turn for turn in parse_inbox_text(text, source="hermes", session_id="evidence") if turn.complete)
        candidate = {
            "candidate_id": "c3", "memory": "兴银理财流程调整", "worth": True,
            "duplicate": False, "type": "project", "scopes": ["project:兴银理财"], "scope_source": "model",
        }
        processor = Processor(self.service)
        self.assertEqual("project:鑫元基金", processor._turn_evidence_project_scope(turn, self.service.vault.config()))
        self.assertTrue(processor._scope_evidence_conflict(candidate, turn, self.service.vault.config()))


if __name__ == "__main__":
    unittest.main()
