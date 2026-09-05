import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.index import event_key
from memleaf.inbox import parse_inbox
from memleaf.llm import ModelError, ModelUnavailable
from memleaf.memory_writer import MemoryWriter
from memleaf.processing import ProcessingError, Processor
from memleaf.model_execution import ModelExecutor
from memleaf.planning_context import PlanningContext
from memleaf.prompts import RELATIVE_TIME_CORRECTION
from memleaf.validation import ModelOutputError


from tests.semantic_fixtures import semantic_fixture, deferred_target_response

@semantic_fixture
class QueueBackend:
    provider = "fake"
    model = "b2a-test"

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        self.calls.append({"prompt": prompt, "system": system, "purpose": purpose})
        if not self.responses:
            raise ModelError("fake queue exhausted")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(prompt, system=system, purpose=purpose, temperature=temperature)
        return response


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class StageB2ATest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.path = Path(self.tempdir.name) / "vault"

    def tearDown(self):
        self.tempdir.cleanup()

    def service(self, backend=None, native=None, name="vault"):
        path = self.path if name == "vault" else Path(self.tempdir.name) / name
        return Memleaf(path, model=backend, clock=self.clock, native_memory_reader=native)

    @staticmethod
    def gate(candidates):
        return json.dumps({"candidates": candidates})

    @staticmethod
    def candidate(
        candidate_id,
        evidence,
        *,
        memory="a durable fact",
        duplicate=False,
        worth=True,
        type="fact",
        update_memory_id=None,
    ):
        value = {
            "candidate_id": candidate_id,
            "memory": memory,
            "evidence_event_ids": list(evidence),
            "duplicate": duplicate,
            "worth": worth,
            "type": type,
            "scopes": ["global"],
            "scope_source": "model",
        }
        if update_memory_id is not None:
            value["update_memory_id"] = update_memory_id
        return value

    @staticmethod
    def summary(event_key_value, title="Fact", body="A durable fact", type="fact", **extra):
        value = {
            "title": title,
            "body": body,
            "tags": ["b2a"],
            "type": type,
            "scopes": ["global"],
            "scope_source": "model",
            "sources": [
                {
                    "event_key": event_key_value,
                    "session_id": "model-forged-session",
                    "turn_id": "model-forged-turn",
                    "conversation_title": "MODEL_FORGED_TITLE",
                }
            ],
        }
        value.update(extra)
        return json.dumps(value)

    def capture_turn(
        self,
        service,
        *,
        source="codex",
        session="s",
        turn="t1",
        user_event="u1",
        assistant_event="a1",
        user="user visible fact",
        assistant="assistant visible response",
    ):
        service.capture(source, session, turn, "user", user, event_id=user_event)
        if assistant_event is not None:
            service.capture(source, session, turn, "assistant", assistant, event_id=assistant_event)
        return event_key(user_event), event_key(assistant_event) if assistant_event else None

    def processed(self, service):
        return json.loads(service.vault.processed_index_path.read_text(encoding="utf-8"))

    def knowledge(self, service):
        return service._read_memories_unlocked("knowledge")

    def test_process_zero_candidates_is_success_and_marks_eligibility(self):
        backend = QueueBackend()
        service = self.service(backend)
        user_key, assistant_key = self.capture_turn(service)
        backend.responses.append(self.gate([]))

        result = service.process(source="codex", session_id="s")

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(len(self.knowledge(service)), 0)
        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["watermark"], 1)
        self.assertEqual(len(state["processed_turns"]), 1)
        entry = state["processed_turns"][0]
        self.assertEqual(set(entry["event_keys"]), {user_key, assistant_key})
        self.assertEqual(
            entry["eligible_cleanup_at"],
            "2026-08-25T00:00:00Z",
        )
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").exists())

    def test_automatic_no_change_skips_writes_and_scope_observation(self):
        backend = QueueBackend()
        service = self.service(backend, name="automatic-no-change")
        user_key, _ = self.capture_turn(
            service,
            turn="automatic-no-change",
            user_event="automatic-no-change-user",
            assistant_event="automatic-no-change-assistant",
            user="浙江东方正文为空，尚未闭环，待问结论。",
            assistant="先暂存为待跟进事项。",
        )
        no_change = self.candidate(
            "zhejiang-pending",
            [user_key],
            memory="浙江东方正文为空，尚未闭环，待问结论。",
            type="event",
        )
        no_change["scopes"] = ["project:浙江东方"]
        temporary = self.candidate(
            "temporary-feedback",
            [user_key],
            memory="金元顺安等待反馈。",
            worth=False,
            type=None,
        )
        temporary["scopes"] = ["project:金元顺安"]
        backend.responses.extend(
            [
                self.gate([no_change, temporary]),
                json.dumps({"decision": "NO_CHANGE"}),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["memory_ids"], [])
        self.assertEqual(self.knowledge(service), [])
        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["watermark"], 1)
        self.assertEqual(state["processed_turns"][0]["memory_ids"], [])
        self.assertNotIn("project:浙江东方", state.get("scopes", []))
        self.assertNotIn("project:金元顺安", state.get("scopes", []))
        self.assertNotIn("project:浙江东方", service.vault.config()["scopes"])
        self.assertNotIn("project:金元顺安", service.vault.config()["scopes"])
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").is_file())

    def test_explicit_no_change_is_rejected_and_retried(self):
        backend = QueueBackend([json.dumps({"decision": "NO_CHANGE"})] * 3)
        service = self.service(backend, name="explicit-no-change")

        with self.assertRaises(ModelOutputError) as raised:
            service.remember(
                "A concrete explicitly requested memory.",
                source="codex",
                session_id="explicit-no-change",
                turn_id="remember-turn",
                event_id="explicit-no-change-event",
                model=backend,
            )

        self.assertEqual(raised.exception.validation_detail, "unknown_fields")
        self.assertEqual(raised.exception.stage, "summarize")
        self.assertEqual(raised.exception.attempt_count, 3)
        self.assertEqual([call["purpose"] for call in backend.calls], ["summarize"] * 3)
        self.assertEqual(self.knowledge(service), [])
        marker = self.processed(service)["sessions"]["codex/explicit-no-change"]["processing"]
        self.assertEqual(marker["failure_stage"], "summarize")
        self.assertEqual(marker["validation_detail"], "unknown_fields")
        self.assertEqual(marker["attempt_count"], 3)

    def test_gate_scope_misroute_retries_then_automatic_no_change_is_safe(self):
        backend = QueueBackend()
        service = self.service(backend, name="scope-misroute-no-change")
        config = service.vault.config()
        config["scopes"] = {"project:zhongyin": {"aliases": ["中银国际"]}}
        save_config(service.vault.config_path, config)
        user_key, _ = self.capture_turn(
            service,
            turn="scope-misroute",
            user_event="scope-misroute-user",
            assistant_event="scope-misroute-assistant",
            user="浙江东方正文为空，尚未闭环，待问结论。",
            assistant="先暂存为待跟进事项。",
        )
        wrong = self.candidate(
            "wrong-zhejiang-scope",
            [user_key],
            memory="浙江东方正文为空，尚未闭环，待问结论。",
            type="event",
        )
        wrong["scopes"] = ["project:zhongyin"]
        corrected = dict(wrong, candidate_id="correct-zhejiang-scope", scopes=["project:浙江东方"])
        backend.responses.extend(
            [
                self.gate([wrong]),
                self.gate([corrected]),
                json.dumps({"decision": "NO_CHANGE"}),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memories_written"], 0)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "summarize"])
        self.assertIn("Previous output violated: scope_not_grounded.", backend.calls[1]["prompt"])
        self.assertEqual(self.knowledge(service), [])
        state = self.processed(service)["sessions"]["codex/s"]
        self.assertNotIn("project:zhongyin", state.get("scopes", []))
        self.assertNotIn("project:浙江东方", state.get("scopes", []))
        self.assertNotIn("project:浙江东方", service.vault.config()["scopes"])

    def test_gate_scope_not_grounded_model_corrects_on_final_retry(self):
        backend = QueueBackend()
        service = self.service(backend, name="scope-final-correct")
        config = service.vault.config()
        config["scopes"] = {
            "project:zhongyin": {"aliases": ["中银国际"]},
            "project:金元顺安": {"aliases": ["金元顺安"]},
        }
        save_config(service.vault.config_path, config)
        user_key, _ = self.capture_turn(
            service,
            turn="scope-final-correct",
            user_event="scope-final-correct-user",
            assistant_event="scope-final-correct-assistant",
            user="中银国际实施计划需要更新。",
            assistant="确认中银国际计划更新。",
        )
        wrong = self.candidate(
            "wrong-zg-scope",
            [user_key],
            memory="中银国际实施计划需要更新。",
            type="project",
        )
        wrong["scopes"] = ["project:金元顺安"]
        backend.responses.extend(
            [
                self.gate([wrong]),
                self.gate([wrong]),
                self.gate([{**wrong, "scopes": ["project:zhongyin"]}]),
                self.summary(
                    user_key,
                    title="中银国际实施计划",
                    body="中银国际实施计划需要更新。",
                    type="project",
                    scopes=["project:zhongyin"],
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "gate", "summarize"])
        self.assertIn("Previous output violated: scope_not_grounded.", backend.calls[1]["prompt"])
        self.assertEqual(self.knowledge(service)[0].memory.scopes, ["project:zhongyin"])
        self.assertIn("project:zhongyin", service.vault.config()["scopes"])

    def test_scope_not_grounded_final_retry_keeps_other_valid_candidates(self):
        backend = QueueBackend()
        service = self.service(backend, name="scope-final-keep-valid")
        config = service.vault.config()
        config["scopes"] = {
            "project:zhongyin": {"aliases": ["中银国际"]},
            "project:xinyuan": {"aliases": ["鑫元基金"]},
        }
        save_config(service.vault.config_path, config)
        user_key, assistant_key = self.capture_turn(
            service,
            turn="scope-final-keep-valid",
            user_event="scope-final-keep-valid-user",
            assistant_event="scope-final-keep-valid-assistant",
            user="中银国际计划已更新；鑫元待反馈。",
            assistant="待确认中银国际与鑫元相关事项。",
        )

        valid = self.candidate(
            "zhongyin-plan",
            [user_key],
            memory="中银国际计划已更新。",
            type="project",
        )
        valid["scopes"] = ["project:zhongyin"]
        bad = self.candidate(
            "xinyuan-ambiguous",
            [assistant_key],
            memory="鑫元 待反馈。",
            type="event",
        )
        bad["scopes"] = ["project:xinyuan"]
        backend.responses.extend(
            [
                self.gate([bad, valid]),
                self.gate([bad, valid]),
                self.gate([bad, valid]),
                self.summary(
                    user_key,
                    title="中银国际计划",
                    body="中银国际计划已更新。",
                    type="project",
                    scopes=["project:zhongyin"],
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "gate", "summarize"])
        self.assertIn("Previous output violated: scope_not_grounded.", backend.calls[1]["prompt"])
        self.assertEqual(len(self.knowledge(service)), 1)
        self.assertEqual(self.knowledge(service)[0].memory.scopes, ["project:zhongyin"])
        turn_entry = self.processed(service)["sessions"]["codex/s"]["processed_turns"][0]
        deferred = turn_entry["deferred_candidates"]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["candidate_id"], "xinyuan-ambiguous")
        self.assertNotIn("鑫元", "\n".join(record.memory.body for record in self.knowledge(service)))

    def test_scope_not_grounded_final_retry_ambiguous_mention_no_write(self):
        backend = QueueBackend()
        service = self.service(backend, name="scope-final-ambiguous")
        config = service.vault.config()
        config["scopes"] = {
            "project:zhongyin": {"aliases": ["中银国际"]},
            "project:jinyuan": {"aliases": ["金元顺安"]},
        }
        save_config(service.vault.config_path, config)
        user_key, _ = self.capture_turn(
            service,
            turn="scope-final-ambiguous",
            user_event="scope-final-ambiguous-user",
            assistant_event="scope-final-ambiguous-assistant",
            user="中银国际和金元顺安均有更新。",
            assistant="已确认两个项目的进展。",
        )
        wrong = self.candidate(
            "both-mentioned",
            [user_key],
            memory="中银国际和金元顺安均有更新。",
            type="fact",
        )
        wrong["scopes"] = ["project:zhongyin"]
        backend.responses.extend([self.gate([wrong]), self.gate([wrong]), self.gate([wrong])])

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memories_written"], 0)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "gate"])
        self.assertEqual(self.knowledge(service), [])
        turn_entry = self.processed(service)["sessions"]["codex/s"]["processed_turns"][0]
        deferred = turn_entry["deferred_candidates"]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["candidate_id"], "both-mentioned")
        self.assertEqual(deferred[0]["reason"], "scope_conflict")
        self.assertEqual(deferred[0]["scopes"], ["project:zhongyin"])
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["processing"]["status"], "idle")

    def test_scope_not_grounded_final_retry_wrong_scope_and_wrong_update_target_is_safe(self):
        backend = QueueBackend()
        service = self.service(backend, name="scope-final-wrong-scope-target")
        config = service.vault.config()
        config["scopes"] = {
            "project:alpha": {"aliases": ["阿尔法"]},
            "project:beta": {"aliases": ["贝塔"]},
        }
        save_config(service.vault.config_path, config)
        old = service.create_memory(
            memory_id="alpha-existing",
            title="阿尔法 项目负责人",
            body="阿尔法 项目负责人当前为甲。",
            type="identity",
            scopes=["project:alpha"],
        )
        user_key, _ = self.capture_turn(
            service,
            turn="scope-final-wrong-scope-target",
            user_event="scope-final-wrong-scope-target-user",
            assistant_event="scope-final-wrong-scope-target-assistant",
            user="阿尔法 项目负责人当前为甲；贝塔 项目负责人更新为乙。",
            assistant="已确认两个项目的负责人信息。",
        )
        wrong = self.candidate(
            "wrong-scope-target",
            [user_key],
            memory="贝塔 项目负责人更新为乙。",
            type="identity",
            update_memory_id=old.memory_id,
        )
        wrong["scopes"] = ["project:alpha"]
        backend.responses.extend(
            [
                self.gate([wrong]),
                self.gate([wrong]),
                self.gate([{**{k: v for k, v in wrong.items() if k != "update_memory_id"},
                            "scopes": ["project:beta"]}]),
                self.summary(
                    user_key,
                    title="贝塔 项目负责人",
                    body="贝塔 项目负责人更新为乙。",
                    type="identity",
                    scopes=["project:beta"],
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "gate", "summarize"])
        self.assertIn("Previous output violated: scope_not_grounded.", backend.calls[1]["prompt"])
        self.assertEqual(service.read(old.memory_id).body, old.body)
        self.assertEqual(len(self.knowledge(service)), 2)
        self.assertIn(
            "贝塔 项目负责人更新为乙。",
            "\n".join(record.memory.body for record in self.knowledge(service)),
        )
        self.assertEqual(
            {
                tuple(record.memory.scopes): record.memory.body
                for record in self.knowledge(service)
            }[("project:beta",)],
            "贝塔 项目负责人更新为乙。",
        )

    def test_scope_not_grounded_final_retry_preserves_unregistered_project_scope(self):
        backend = QueueBackend()
        service = self.service(backend, name="scope-final-unregistered-legal")
        config = service.vault.config()
        config["scopes"] = {
            "project:alpha": {"aliases": ["阿尔法"]},
        }
        save_config(service.vault.config_path, config)
        old = service.create_memory(
            memory_id="alpha-existing",
            title="阿尔法 项目状态",
            body="阿尔法 项目状态保持不变。",
            type="project",
            scopes=["project:alpha"],
        )
        user_key, _ = self.capture_turn(
            service,
            turn="scope-final-unregistered-legal",
            user_event="scope-final-unregistered-legal-user",
            assistant_event="scope-final-unregistered-legal-assistant",
            user="阿尔法项目状态不变；浙江东方实施计划待确认。",
            assistant="已确认两个项目的状态。",
        )
        wrong = self.candidate(
            "new-unregistered-final-retry",
            [user_key],
            memory="浙江东方实施计划待确认。",
            type="project",
            update_memory_id=old.memory_id,
        )
        wrong["scopes"] = ["project:浙江东方"]
        backend.responses.extend(
            [
                self.gate([wrong]),
                self.gate([wrong]),
                self.gate([{k: v for k, v in wrong.items() if k != "update_memory_id"}]),
                self.summary(
                    user_key,
                    title="浙江东方实施计划",
                    body="浙江东方实施计划待确认。",
                    type="project",
                    scopes=["project:浙江东方"],
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "gate", "summarize"])
        self.assertIn("Previous output violated: target_not_relevant.", backend.calls[1]["prompt"])
        self.assertEqual(service.read(old.memory_id).body, old.body)
        self.assertEqual(len(self.knowledge(service)), 2)
        unregistered = [
            record
            for record in self.knowledge(service)
            if record.memory.scopes == ["project:浙江东方"]
        ]
        self.assertEqual(len(unregistered), 1)
        self.assertEqual(unregistered[0].memory.body, "浙江东方实施计划待确认。")

    def test_summary_scope_drift_retries_before_writing(self):
        backend = QueueBackend()
        service = self.service(backend, name="summary-scope-drift")
        user_key, _ = self.capture_turn(
            service,
            turn="summary-scope-drift",
            user_event="summary-scope-drift-user",
            assistant_event="summary-scope-drift-assistant",
            user="中银国际实施计划需要更新。",
            assistant="已确认中银国际计划调整。",
        )
        candidate = self.candidate(
            "zhongyin-plan",
            [user_key],
            memory="中银国际实施计划需要更新。",
            type="project",
        )
        candidate["scopes"] = ["project:中银国际"]
        backend.responses.extend(
            [
                self.gate([candidate]),
                self.summary(
                    user_key,
                    title="中银国际实施计划",
                    body="中银国际实施计划需要更新。",
                    type="project",
                    scopes=["project:摩根基金"],
                ),
                self.summary(
                    user_key,
                    title="中银国际实施计划",
                    body="中银国际实施计划需要更新。",
                    type="project",
                    scopes=["project:中银国际"],
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize", "summarize"])
        self.assertIn("Previous output violated: scope_drift.", backend.calls[2]["prompt"])
        self.assertEqual(self.knowledge(service)[0].memory.scopes, ["project:中银国际"])
        self.assertNotIn("project:摩根基金", service.vault.config()["scopes"])

    def test_three_summary_scope_drifts_keep_queue_and_write_nothing(self):
        backend = QueueBackend()
        service = self.service(backend, name="summary-scope-drift-failure")
        user_key, _ = self.capture_turn(
            service,
            turn="summary-scope-drift-failure",
            user_event="summary-scope-drift-failure-user",
            assistant_event="summary-scope-drift-failure-assistant",
            user="中银国际实施计划需要更新。",
            assistant="已确认中银国际计划调整。",
        )
        candidate = self.candidate(
            "zhongyin-plan-failure",
            [user_key],
            memory="中银国际实施计划需要更新。",
            type="project",
        )
        candidate["scopes"] = ["project:中银国际"]
        bad_summary = self.summary(
            user_key,
            title="中银国际实施计划",
            body="中银国际实施计划需要更新。",
            type="project",
            scopes=["project:摩根基金"],
        )
        backend.responses.extend([self.gate([candidate]), bad_summary, bad_summary, bad_summary])

        with self.assertRaises(ModelOutputError) as raised:
            service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(raised.exception.validation_detail, "scope_drift")
        self.assertEqual(raised.exception.stage, "summarize")
        self.assertEqual(raised.exception.attempt_count, 3)
        marker = self.processed(service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["failure_stage"], "summarize")
        self.assertEqual(marker["validation_detail"], "scope_drift")
        self.assertEqual(marker["attempt_count"], 3)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"].get("watermark", 0), 0)
        self.assertEqual(self.knowledge(service), [])
        self.assertNotIn("project:摩根基金", service.vault.config()["scopes"])
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").is_file())

    def test_two_candidates_are_separate_deterministic_memories_and_repeat_is_noop(self):
        backend = QueueBackend()
        service = self.service(backend)
        user_key, assistant_key = self.capture_turn(service, user="A durable fact. A second durable fact.")
        backend.responses.extend(
            [
                self.gate(
                    [
                        self.candidate("c1", [user_key]),
                        self.candidate("c2", [user_key], memory="another durable fact"),
                    ]
                ),
                self.summary(user_key, title="First"),
                self.summary(user_key, title="Second"),
            ]
        )

        first = service.process()
        calls_after_first = len(backend.calls)
        second = service.process()

        self.assertEqual(first["memories_written"], 2)
        self.assertEqual(len(set(first["memory_ids"])), 2)
        self.assertEqual(len(self.knowledge(service)), 2)
        self.assertEqual(second["processed_turns"], 0)
        self.assertEqual(second["memories_written"], 0)
        self.assertEqual(len(backend.calls), calls_after_first)
        self.assertEqual(len(self.knowledge(service)), 2)

    def test_mixed_project_digest_retries_to_atomic_project_outputs(self):
        service = self.service(name="mixed-project-digest")
        history_memory = service.create_memory(
            memory_id="mem-zhongyin-history",
            title="中银国际历史需求",
            body="中银国际历史需求已完整覆盖：需求清单逐项确认、信创测试环境部署、历史数据库和附件全量迁移。",
            type="project",
            scopes=["project:zhongyin"],
        )
        existing = service.create_memory(
            memory_id="mem-zhongyin-plan",
            title="中银国际实施计划",
            body="中银国际实施计划当前按原始安排执行。",
            type="project",
            scopes=["project:zhongyin"],
        )
        config = service.vault.config()
        config["llm"]["diagnostic_logging"] = True
        config["scopes"] = {
            "project:zhongyin": {"aliases": ["中银国际"]},
            "project:morgan": {"aliases": ["摩根基金"]},
        }
        save_config(service.vault.config_path, config)
        user_key, assistant_key = self.capture_turn(
            service,
            session="mixed-project-digest",
            user_event="mixed-digest-user",
            assistant_event="mixed-digest-assistant",
            user=(
                "邮箱巡检：中银国际历史需求已覆盖需求清单逐项确认、信创测试环境部署、"
                "历史数据库和附件全量迁移；客户提出单点登录提前并行、数据文件规范提前确认、"
                "重新压实实施计划；摩根基金本周三排查证券申报类型；兴银转给测试同事；"
                "金元顺安待反馈；嘉实一次性启动会安排；浙江东方正文为空、尚未闭环、待问结论。"
            ),
            assistant="会议和待办已整理，后续按项目分别跟进。",
        )

        def scoped(candidate_id, *, memory, type, scopes, worth=True, update_memory_id=None):
            value = self.candidate(
                candidate_id,
                [user_key, assistant_key],
                memory=memory,
                type=type,
                worth=worth,
                update_memory_id=update_memory_id,
            )
            value["scopes"] = list(scopes)
            return value

        mixed = scoped(
            "digest",
            memory="2026-09-01邮箱巡检需关注事项",
            type="fact",
            scopes=[
                "project:zhongyin",
                "project:morgan",
                "project:兴银",
                "project:金元顺安",
                "project:嘉实",
                "project:浙江东方",
            ],
        )
        corrected = [
            scoped(
                "zhongyin-plan",
                memory="中银国际实施计划新增客户建议：单点登录提前并行、数据文件规范提前确认、重新压实实施计划",
                type="project",
                scopes=["project:zhongyin"],
                update_memory_id=existing.memory_id,
            ),
            scoped(
                "morgan-todo",
                memory="摩根基金排查证券申报类型",
                type="todo",
                scopes=["project:morgan"],
            ),
            scoped(
                "xingyin-transfer",
                memory="兴银事项已转给测试同事，等待内部处理",
                type="event",
                scopes=["project:兴银"],
            ),
            scoped(
                "jinyuan-pending",
                memory="金元顺安待反馈",
                type="event",
                scopes=["project:金元顺安"],
            ),
            scoped(
                "jiashi-kickoff",
                memory="嘉实一次性启动会安排，暂无持久决策或项目约束",
                type="event",
                scopes=["project:嘉实"],
            ),
            scoped(
                "zhejiang-pending",
                memory="浙江东方正文为空，尚未闭环，待问结论",
                type="event",
                scopes=["project:浙江东方"],
            ),
        ]
        backend = QueueBackend(
            [
                self.gate([mixed]),
                self.gate(corrected),
                self.summary(
                    user_key,
                    title="中银国际实施计划",
                    body=(
                        "客户提出单点登录提前并行、数据文件规范提前确认、重新压实实施计划；"
                        "当前待纳入，尚未表示已落地。"
                    ),
                    type="project",
                    scopes=["project:zhongyin"],
                    update_memory_id=existing.memory_id,
                ),
                self.summary(
                    user_key,
                    title="摩根基金证券申报类型排查",
                    body="待排查摩根基金证券申报类型，截止日期为2026-09-02。",
                    type="todo",
                    scopes=["project:morgan"],
                    status="active",
                ),
                json.dumps({"decision": "NO_CHANGE"}),
                json.dumps({"decision": "NO_CHANGE"}),
                json.dumps({"decision": "NO_CHANGE"}),
                json.dumps({"decision": "NO_CHANGE"}),
            ]
        )

        result = service.process(
            source="codex",
            session_id="mixed-project-digest",
            model=backend,
        )

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 2)
        self.assertEqual(
            [call["purpose"] for call in backend.calls],
            ["gate", "gate"] + ["summarize"] * 6,
        )
        self.assertIn("Previous output violated: mixed_project_scopes.", backend.calls[1]["prompt"])
        active = self.knowledge(service)
        self.assertEqual(len(active), 3)
        active_by_id = {record.memory.memory_id: record.memory for record in active}
        self.assertIn(history_memory.memory_id, active_by_id)
        self.assertIn(existing.memory_id, active_by_id)
        self.assertTrue(all(sum(scope.startswith("project:") for scope in record.memory.scopes) <= 1 for record in active))
        self.assertEqual(active_by_id[existing.memory_id].type, "project")
        self.assertIn("客户提出", active_by_id[existing.memory_id].body)
        self.assertIn("待纳入", active_by_id[existing.memory_id].body)
        todo = next(
            memory
            for memory_id, memory in active_by_id.items()
            if memory_id not in {history_memory.memory_id, existing.memory_id}
        )
        self.assertEqual(
            set(active_by_id),
            {history_memory.memory_id, existing.memory_id, todo.memory_id},
        )
        self.assertEqual(todo.type, "todo")
        self.assertEqual(todo.scopes, ["project:morgan"])
        self.assertIn("2026-09-02", todo.body)
        self.assertNotIn("本周三", todo.body)
        self.assertEqual(active_by_id[history_memory.memory_id].body, history_memory.body)
        self.assertIn("单点登录提前并行", active_by_id[existing.memory_id].body)
        self.assertNotIn("需求清单逐项确认", active_by_id[existing.memory_id].body)
        self.assertNotIn("历史数据库和附件全量迁移", active_by_id[existing.memory_id].body)
        history = service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.extra["active_memory_id"], existing.memory_id)
        self.assertEqual(history[0].memory.body, existing.body)
        bodies = "\n".join(record.memory.body for record in active)
        self.assertNotIn("邮箱巡检", bodies)
        self.assertNotIn("需关注事项", bodies)
        self.assertNotIn("转给测试同事", bodies)
        self.assertNotIn("待反馈", bodies)
        self.assertNotIn("启动会", bodies)
        self.assertNotIn("正文为空", bodies)
        self.assertEqual(
            len(service.vault.list_markdown("history")),
            1,
        )
        self.assertEqual(
            service.vault.config()["scopes"].keys(),
            {"project:zhongyin", "project:morgan"},
        )
        state = self.processed(service)["sessions"]["codex/mixed-project-digest"]
        self.assertEqual(state["scopes"], ["project:zhongyin", "project:morgan"])
        self.assertNotIn("project:兴银", state["scopes"])
        self.assertNotIn("project:金元顺安", state["scopes"])
        self.assertNotIn("project:嘉实", state["scopes"])
        self.assertNotIn("project:浙江东方", state["scopes"])
        diagnostics = [
            json.loads(line)
            for line in (service.vault.logs_path / "model-diagnostics.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(diagnostics[0]["validation_detail"], "mixed_project_scopes")

    def test_three_mixed_project_gate_failures_retain_deferred_evidence(self):
        backend = QueueBackend()
        service = self.service(backend, name="mixed-gate-failure")
        user_key, assistant_key = self.capture_turn(
            service,
            session="mixed-gate-failure",
            user_event="mixed-gate-failure-user",
            assistant_event="mixed-gate-failure-assistant",
        )
        mixed = self.candidate(
            "mixed-gate-candidate",
            [user_key, assistant_key],
            memory="cross-project digest",
            type="fact",
        )
        mixed["scopes"] = ["project:zhongyin", "project:morgan"]
        backend.responses.extend([self.gate([mixed])] * 3)

        result = service.process(source="codex", session_id="mixed-gate-failure", model=backend)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["deferred_candidates"], 1)
        self.assertEqual([c["purpose"] for c in backend.calls], ["gate"] * 3)
        state = self.processed(service)["sessions"]["codex/mixed-gate-failure"]
        row = state["processed_turns"][0]["deferred_candidates"][0]
        self.assertEqual(row["reason"], "scope_conflict")
        self.assertEqual(row["scopes"], mixed["scopes"])
        # Scan progress can advance; unresolved original evidence remains.
        self.assertEqual(state["watermark"], 1)
        self.assertTrue((service.vault.inbox_path / "codex" / "mixed-gate-failure.md").is_file())
        self.assertEqual(self.knowledge(service), [])

    def test_draft_turn_is_discarded_and_final_email_state_is_the_only_memory(self):
        backend = QueueBackend()
        service = self.service(backend, name="email-final-only")

        draft_user, draft_assistant = self.capture_turn(
            service,
            turn="draft",
            user_event="draft-user",
            assistant_event="draft-assistant",
            user="Prepare an email to Chen Zhongkai for my confirmation.",
            assistant="The email draft is ready for confirmation.",
        )
        backend.responses.append(self.gate([]))
        draft_result = service.process()
        self.assertEqual(draft_result["memories_written"], 0)
        self.assertEqual(len(self.knowledge(service)), 0)

        final_user, final_assistant = self.capture_turn(
            service,
            turn="sent",
            user_event="sent-user",
            assistant_event="sent-assistant",
            user="Confirm sending the Chen Zhongkai email now.",
            assistant="The email was sent to Chen Zhongkai.",
        )
        final_candidate = self.candidate(
            "email-sent",
            [final_user, final_assistant],
            memory="The email to Chen Zhongkai was sent.",
            type="event",
        )
        backend.responses.extend(
            [
                self.gate([final_candidate]),
                self.summary(
                    final_user,
                    title="Chen Zhongkai email sent",
                    body="The email to Chen Zhongkai was sent.",
                    type="event",
                ),
            ]
        )
        result = service.process()

        self.assertEqual(result["memories_written"], 1)
        active = self.knowledge(service)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].memory.body, "The email to Chen Zhongkai was sent.")
        self.assertNotIn("draft", active[0].memory.body.lower())
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertEqual(
            [memory.memory_id for memory in service.search("draft", include_history=False, todo_status="all")],
            [],
        )
        found = service.search("Chen Zhongkai email", include_history=False, todo_status="all")
        self.assertEqual([memory.memory_id for memory in found], [active[0].memory.memory_id])

    def test_gate_update_target_is_forwarded_when_summary_omits_it_and_archives_old_state(self):
        backend = QueueBackend()
        service = self.service(backend, name="email-update-target")
        old = service.create_memory(
            memory_id="mem-draft",
            title="Chen Zhongkai email draft",
            body="A draft email to Chen Zhongkai is awaiting confirmation.",
            tags=["email"],
            type="event",
        )
        user_key, assistant_key = self.capture_turn(
            service,
            turn="sent",
            user_event="sent-user",
            assistant_event="sent-assistant",
            user="Confirm sending the Chen Zhongkai email.",
            assistant="The Chen Zhongkai email has been sent.",
        )
        candidate = self.candidate(
            "email-sent",
            [user_key, assistant_key],
            memory="The Chen Zhongkai email was sent.",
            type="event",
            update_memory_id=old.memory_id,
        )
        backend.responses.extend(
            [
                self.gate([candidate]),
                self.summary(
                    user_key,
                    title="Chen Zhongkai email sent",
                    body="The Chen Zhongkai email was sent.",
                    type="event",
                ),
            ]
        )

        result = service.process()

        self.assertEqual(result["memories_written"], 1)
        active = self.knowledge(service)
        self.assertEqual([record.memory.memory_id for record in active], [old.memory_id])
        self.assertEqual(active[0].memory.body, "The Chen Zhongkai email was sent.")
        history = service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.body, old.body)
        self.assertEqual(history[0].memory.extra["active_memory_id"], old.memory_id)
        self.assertEqual(
            [memory.memory_id for memory in service.search("draft", include_history=False, todo_status="all")],
            [],
        )
        self.assertEqual(
            [memory.memory_id for memory in service.search("Chen Zhongkai email", include_history=False, todo_status="all")],
            [old.memory_id],
        )

    def test_natural_project_owner_update_reuses_id_and_archives_old_state(self):
        backend = QueueBackend()
        service = self.service(backend, name="natural-owner-update")

        first_user, first_assistant = self.capture_turn(
            service,
            source="hermes",
            session="s",
            turn="owner-a",
            user_event="owner-a-user",
            assistant_event="owner-a-assistant",
            user="ML-STATE-20260827 项目负责人是甲。",
            assistant="已确认 ML-STATE-20260827 当前负责人为甲。",
        )
        backend.responses.extend(
            [
                self.gate(
                    [
                        self.candidate(
                            "ml-state-owner",
                            [first_user, first_assistant],
                            memory="ML-STATE-20260827 项目负责人是甲。",
                            type="identity",
                        )
                    ]
                ),
                self.summary(
                    first_user,
                    title="ML-STATE-20260827 项目负责人",
                    body="ML-STATE-20260827 项目负责人是甲。",
                    type="identity",
                ),
            ]
        )
        first_result = service.process(source="hermes", session_id="s")
        self.assertEqual(first_result["processed_turns"], 1)
        first_memory = self.knowledge(service)[0].memory

        second_user, second_assistant = self.capture_turn(
            service,
            source="hermes",
            session="s",
            turn="owner-b",
            user_event="owner-b-user",
            assistant_event="owner-b-assistant",
            user="ML-STATE-20260827 同一项目负责人更新为乙。",
            assistant="已确认今后以乙为准。",
        )
        update_candidate = self.candidate(
            "ml-state-owner-update",
            [second_user, second_assistant],
            memory="ML-STATE-20260827 项目负责人更新为乙。",
            type="identity",
            update_memory_id=first_memory.memory_id,
        )
        backend.responses.extend(
            [
                self.gate([update_candidate]),
                self.summary(
                    second_user,
                    title="ML-STATE-20260827 项目负责人",
                    body="ML-STATE-20260827 项目负责人已更新为乙。",
                    type="identity",
                    update_memory_id=first_memory.memory_id,
                ),
            ]
        )
        second_result = service.process(source="hermes", session_id="s")

        self.assertEqual(first_result["memory_ids"], [first_memory.memory_id])
        self.assertEqual(second_result["processed_turns"], 1)
        active = self.knowledge(service)
        self.assertEqual([record.memory.memory_id for record in active], [first_memory.memory_id])
        self.assertEqual(active[0].memory.body, "ML-STATE-20260827 项目负责人已更新为乙。")
        history = service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.body, "ML-STATE-20260827 项目负责人是甲。")
        self.assertEqual(history[0].memory.extra["active_memory_id"], first_memory.memory_id)
        self.assertEqual(
            [memory.memory_id for memory in service.search("ML-STATE-20260827 项目负责人", include_history=False, todo_status="all")],
            [first_memory.memory_id],
        )
        self.assertEqual(
            [memory.memory_id for memory in service.search("负责人是甲", include_history=False, todo_status="all")],
            [],
        )
        processed = self.processed(service)["sessions"]["hermes/s"]
        self.assertEqual(processed["watermark"], 2)
        self.assertEqual(processed["processing"]["status"], "idle")

    def test_additive_project_plan_update_retains_existing_plan_facts(self):
        backend = QueueBackend()
        service = self.service(backend, name="additive-project-plan")
        old_body = (
            "金元顺安信创实施计划采用达梦和东方通，要求38个工作日完成，"
            "上线日期为2026-10-27，负责人为吴江波。"
        )
        existing = service.create_memory(
            memory_id="mem-jinyuan-plan",
            title="金元顺安员工投资行为申报系统信创改造实施计划",
            body=old_body,
            tags=["金元顺安", "达梦", "东方通", "负责人"],
            type="project",
            scopes=["project:金元顺安"],
        )
        user_key, assistant_key = self.capture_turn(
            service,
            source="hermes",
            session="jinyuan-plan",
            turn="feedback",
            user_event="jinyuan-plan-user",
            assistant_event="jinyuan-plan-assistant",
            user="金元顺安客户要求实施计划补充数据迁移、安全基线、漏洞扫描和回滚演练。",
            assistant="需增加数据迁移、安全基线、漏洞扫描和回滚演练。",
        )
        item = self.candidate(
            "jinyuan-plan-feedback",
            [user_key, assistant_key],
            memory="金元顺安实施计划需补充数据迁移、安全基线、漏洞扫描和回滚演练。",
            type="project",
            update_memory_id=existing.memory_id,
        )
        item["scopes"] = ["project:金元顺安"]
        backend.responses.extend(
            [
                self.gate([item]),
                self.summary(
                    user_key,
                    title=existing.title,
                    body=old_body + "\n\n客户要求补充数据迁移、安全基线、漏洞扫描和回滚演练。",
                    tags=["金元顺安", "达梦", "东方通", "负责人", "调整建议"],
                    type="project",
                    scopes=["project:金元顺安"],
                    update_memory_id=existing.memory_id,
                ),
            ]
        )

        result = service.process(source="hermes", session_id="jinyuan-plan", model=backend)

        self.assertEqual(result["memory_ids"], [existing.memory_id])
        current = service.read(existing.memory_id)
        self.assertIn(old_body, current.body)
        self.assertIn("回滚演练", current.body)
        self.assertEqual(current.title, existing.title)
        self.assertEqual(current.tags, ["金元顺安", "达梦", "东方通", "负责人", "调整建议"])
        history = service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.body, old_body)

    def test_explicit_project_plan_replacement_does_not_merge_conflicting_body(self):
        service = self.service()
        target = service.create_memory(memory_id="mem-plan-replacement", title="北辰项目实施计划",
            body="北辰项目数据库采用达梦。", tags=["达梦"], type="project", scopes=["project:北辰"])
        key, _ = self.capture_turn(service, user="北辰项目实施计划的数据库改为PostgreSQL。")
        proposal = self.candidate("replacement", [key], memory="北辰项目实施计划的数据库改为PostgreSQL。",
            type="project", update_memory_id=target.memory_id)
        proposal["scopes"] = ["project:北辰"]
        body = "北辰项目数据库改为PostgreSQL。"
        backend = QueueBackend([self.gate([proposal]), self.summary(key, title=target.title,
            body=body, type="project", scopes=["project:北辰"], update_memory_id=target.memory_id)])
        result = service.process(model=backend, scope="project:北辰")
        self.assertEqual(result["memory_ids"], [target.memory_id])
        self.assertEqual(service.read(target.memory_id).body, body)
        history = service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.body, target.body)

    def test_summary_update_target_mismatch_is_retried_before_writing(self):
        backend = QueueBackend()
        service = self.service(backend, name="email-update-mismatch")
        old = service.create_memory(
            memory_id="mem-draft",
            title="Email draft",
            body="A draft email is awaiting confirmation.",
            type="event",
        )
        other = service.create_memory(
            memory_id="mem-other",
            title="Other email",
            body="An unrelated email fact.",
            type="event",
        )
        user_key, _ = self.capture_turn(
            service,
            turn="sent",
            user_event="sent-user",
            assistant_event="sent-assistant",
            user="Confirm the email is sent.",
        )
        candidate = self.candidate(
            "email-sent",
            [user_key],
            memory="The email was sent.",
            type="event",
            update_memory_id=old.memory_id,
        )
        backend.responses.extend(
            [
                self.gate([candidate]),
                self.summary(user_key, title="Sent", body="The email was sent.", type="event", update_memory_id=other.memory_id),
                self.summary(user_key, title="Sent", body="The email was sent.", type="event", update_memory_id=old.memory_id),
            ]
        )

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize", "summarize"])
        self.assertIn("gate selected the update target", backend.calls[2]["prompt"])
        self.assertEqual(service.read(old.memory_id).body, "The email was sent.")
        self.assertEqual(service.read(other.memory_id).body, other.body)
        self.assertEqual(len(service.vault.list_markdown("history")), 1)

    def test_same_project_different_future_use_target_retries_then_creates(self):
        backend = QueueBackend()
        service = self.service(backend, name="same-project-wrong-target")
        old = service.create_memory(
            memory_id="mem-orion-sync",
            title="金元顺安项目任务同步到 Orion 系统",
            body=(
                "金元顺安项目任务已同步到 Orion；里程碑包含信创环境搭建、"
                "功能验证测试和上线安排。"
            ),
            type="todo",
            scopes=["project:金元顺安"],
        )
        user_key, assistant_key = self.capture_turn(
            service,
            turn="same-project-wrong-target",
            user_event="same-project-wrong-target-user",
            assistant_event="same-project-wrong-target-assistant",
            user="金元顺安实施计划六项建议；同时需要查看 Orion 任务同步。",
            assistant="已整理金元顺安实施计划与 Orion 任务同步。",
        )
        wrong = self.candidate(
            "wrong-jinyuan-target",
            [user_key, assistant_key],
            memory="金元顺安实施计划六项建议含里程碑、信创环境、测试和上线安排。",
            type="todo",
            update_memory_id=old.memory_id,
        )
        wrong["scopes"] = ["project:金元顺安"]
        corrected = dict(wrong, candidate_id="jinyuan-independent")
        corrected.pop("update_memory_id")
        backend.responses.extend(
            [
                self.gate([wrong]),
                self.gate([corrected]),
                self.summary(
                    user_key,
                    title="金元顺安实施计划六项建议",
                    body="金元顺安实施计划六项建议含里程碑、信创环境、测试和上线安排。",
                    type="todo",
                    scopes=["project:金元顺安"],
                    status="active",
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "summarize"])
        self.assertIn("Previous output violated: target_not_relevant.", backend.calls[1]["prompt"])
        self.assertEqual(service.read(old.memory_id).title, old.title)
        self.assertEqual(service.read(old.memory_id).body, old.body)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        active = service._read_memories_unlocked("knowledge")
        self.assertEqual(len(active), 2)
        self.assertIn(
            "金元顺安实施计划六项建议含里程碑、信创环境、测试和上线安排。",
            "\n".join(item.memory.body for item in active),
        )

    def test_same_project_same_future_use_target_remains_valid(self):
        backend = QueueBackend()
        service = self.service(backend, name="same-project-valid-target")
        old = service.create_memory(
            memory_id="mem-jinyuan-plan",
            title="金元顺安实施计划",
            body="金元顺安实施计划按原方案执行。",
            type="project",
            scopes=["project:金元顺安"],
        )
        user_key, assistant_key = self.capture_turn(
            service,
            turn="same-project-valid-target",
            user_event="same-project-valid-target-user",
            assistant_event="same-project-valid-target-assistant",
            user="金元顺安实施计划新增部署要求。",
            assistant="已确认金元顺安实施计划需要纳入部署要求。",
        )
        candidate = self.candidate(
            "valid-jinyuan-target",
            [user_key, assistant_key],
            memory="金元顺安实施计划新增部署要求。",
            type="project",
            update_memory_id=old.memory_id,
        )
        candidate["scopes"] = ["project:金元顺安"]
        backend.responses.extend(
            [
                self.gate([candidate]),
                self.summary(
                    user_key,
                    title="金元顺安实施计划",
                    body="金元顺安实施计划按原方案执行。\n\n金元顺安实施计划新增部署要求。",
                    type="project",
                    scopes=["project:金元顺安"],
                    update_memory_id=old.memory_id,
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize"])
        self.assertEqual(
            service.read(old.memory_id).body,
            "金元顺安实施计划按原方案执行。\n\n金元顺安实施计划新增部署要求。",
        )
        self.assertEqual(len(service.vault.list_markdown("history")), 1)

    def test_model_selects_immutable_project_type_and_target(self):
        backend = QueueBackend()
        service = self.service(backend, name="inferred-plan-update")
        old = service.create_memory(
            memory_id="mem-zhongyin-plan",
            title="中银国际员工投资行为申报系统信创改造实施计划",
            body="中银国际实施计划当前按原始安排执行。",
            type="project",
            scopes=["project:中银国际"],
        )
        user_key, assistant_key = self.capture_turn(
            service,
            turn="inferred-plan-update",
            user_event="inferred-plan-update-user",
            assistant_event="inferred-plan-update-assistant",
            user="中银国际客户要求实施计划提前部署测试环境并重新压实计划。",
            assistant="已记录中银国际实施计划调整建议，待更新计划。",
        )
        candidate = self.candidate(
            "zhongyin-plan-feedback",
            [user_key, assistant_key],
            memory="中银国际客户提出实施计划调整建议：提前部署测试环境并重新压实计划。",
            type="project",
            update_memory_id=old.memory_id,
        )
        candidate["scopes"] = ["project:中银国际"]
        backend.responses.extend(
            [
                self.gate([candidate]),
                self.summary(
                    user_key,
                    title=old.title,
                    body="中银国际实施计划当前按原始安排执行；客户提出提前部署测试环境并重新压实计划。",
                    type="project",
                    scopes=["project:中银国际"],
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memory_ids"], [old.memory_id])
        self.assertEqual(len(self.knowledge(service)), 1)
        self.assertEqual(service.read(old.memory_id).type, "project")
        self.assertIn("原始安排", service.read(old.memory_id).body)
        self.assertIn("提前部署测试环境", service.read(old.memory_id).body)
        self.assertEqual(len(service.vault.list_markdown("history")), 1)
        self.assertNotIn("update_target_type_mismatch", " ".join(call["prompt"] for call in backend.calls))

    def test_project_plan_target_wins_over_sent_mail_for_zhongyin_and_jinyuan(self):
        cases = (
            (
                "zhongyin",
                "中银国际",
                "中银国际员工申报系统信创改造实施计划",
                "陈国金",
            ),
            (
                "jinyuan",
                "金元顺安",
                "金元顺安员工投资行为申报系统信创改造实施计划",
                "方明",
            ),
        )
        for slug, project, plan_title, recipient in cases:
            with self.subTest(project=project):
                backend = QueueBackend()
                service = self.service(backend, name=f"multi-target-{slug}")
                plan = service.create_memory(
                    memory_id=f"mem-{slug}-plan",
                    title=plan_title,
                    body=f"{project}实施计划当前按原始安排执行。",
                    type="project",
                    scopes=[f"project:{project}"],
                )
                adjacent = []
                for suffix, title, body in (
                    (
                        "sent-mail",
                        f"已发送{plan_title}邮件给{recipient}",
                        f"已向{recipient}发送{project}实施计划邮件。",
                    ),
                    (
                        "attachment",
                        f"{project}实施计划附件清单",
                        f"{project}实施计划附件已归档。",
                    ),
                    (
                        "meeting",
                        f"{project}实施计划会议纪要",
                        f"{project}实施计划会议已完成。",
                    ),
                ):
                    adjacent.append(
                        service.create_memory(
                            memory_id=f"mem-{slug}-{suffix}",
                            title=title,
                            body=body,
                            type="fact",
                            scopes=[f"project:{project}"],
                        )
                    )
                user_key, assistant_key = self.capture_turn(
                    service,
                    turn=f"multi-target-{slug}",
                    user_event=f"multi-target-{slug}-user",
                    assistant_event=f"multi-target-{slug}-assistant",
                    user=f"{project}客户要求实施计划提前部署测试环境并重新压实计划。",
                    assistant=f"已记录{project}实施计划的新约束，待更新计划。",
                )
                candidate = self.candidate(
                    f"{slug}-plan-feedback",
                    [user_key, assistant_key],
                    memory=f"{project}客户提出实施计划调整建议：提前部署测试环境并重新压实计划。",
                    type="project",
                    update_memory_id=plan.memory_id,
                )
                candidate["scopes"] = [f"project:{project}"]
                backend.responses.extend(
                    [
                        self.gate([candidate]),
                        self.summary(
                            user_key,
                            title=plan_title,
                            body=(
                                f"{project}实施计划当前按原始安排执行；"
                                "客户提出提前部署测试环境并重新压实计划。"
                            ),
                            type="project",
                            scopes=[f"project:{project}"],
                        ),
                    ]
                )

                result = service.process(source="codex", session_id="s", model=backend)

                self.assertEqual(result["memory_ids"], [plan.memory_id])
                self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize"])
                self.assertEqual(len(self.knowledge(service)), 4)
                self.assertIn("提前部署测试环境", service.read(plan.memory_id).body)
                for record in adjacent:
                    self.assertEqual(service.read(record.memory_id).body, record.body)
                self.assertEqual(len(service.vault.list_markdown("history")), 1)

    def test_multiple_same_project_plan_targets_are_deferred_without_sibling(self):
        backend = QueueBackend()
        service = self.service(backend, name="ambiguous-project-plan-target")
        for memory_id, title in (
            ("mem-jinyuan-plan-test", "金元顺安实施计划：测试环境"),
            ("mem-jinyuan-plan-release", "金元顺安实施计划：上线安排"),
        ):
            service.create_memory(
                memory_id=memory_id,
                title=title,
                body=f"{title}按原始安排执行。",
                type="project",
                scopes=["project:金元顺安"],
            )
        user_key, assistant_key = self.capture_turn(
            service,
            turn="ambiguous-project-plan-target",
            user_event="ambiguous-project-plan-target-user",
            assistant_event="ambiguous-project-plan-target-assistant",
            user="金元顺安客户提出实施计划调整建议。",
            assistant="已记录金元顺安实施计划的新约束，待确认对应计划。",
        )
        candidate = self.candidate(
            "ambiguous-jinyuan-plan-feedback",
            [user_key, assistant_key],
            memory="金元顺安实施计划",
            type="fact",
        )
        candidate["scopes"] = ["project:金元顺安"]
        backend.responses.append(deferred_target_response)

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memory_ids"], [])
        self.assertEqual(result["memories_written"], 0)
        self.assertGreater(result["unresolved_evidence_count"], 0)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate"])
        self.assertEqual(
            self.processed(service)["sessions"]["codex/s"]["processed_turns"][0]["deferred_evidence"][0]["reason"],
            "target_ambiguous",
        )
        self.assertEqual(len(self.knowledge(service)), 2)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").is_file())

    def test_model_corrects_wrong_update_to_create_on_final_retry(self):
        backend = QueueBackend()
        service = self.service(backend, name="same-project-wrong-target-final-retry")
        old = service.create_memory(
            memory_id="mem-beta-owner-final-retry",
            title="beta 项目负责人",
            body="beta 项目负责人是丙。",
            type="identity",
            scopes=["project:beta"],
        )
        user_key, assistant_key = self.capture_turn(
            service,
            turn="same-project-wrong-target-final-retry",
            user_event="same-project-wrong-target-final-retry-user",
            assistant_event="same-project-wrong-target-final-retry-assistant",
            user="alpha 项目负责人更新为乙；beta 项目负责人仍为丙。",
            assistant="已确认 alpha 负责人为乙，beta 负责人仍为丙。",
        )
        wrong = self.candidate(
            "wrong-target-final-retry",
            [user_key, assistant_key],
            memory="alpha 项目负责人更新为乙。",
            type="identity",
            update_memory_id=old.memory_id,
        )
        wrong["scopes"] = ["project:alpha"]
        invalid_gate = self.gate([wrong])
        backend.responses.extend(
            [
                invalid_gate,
                invalid_gate,
                self.gate([{k: v for k, v in wrong.items() if k != "update_memory_id"}]),
                self.summary(
                    user_key,
                    title="alpha 项目负责人",
                    body="alpha 项目负责人已更新为乙。",
                    type="identity",
                    scopes=["project:alpha"],
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(
            [call["purpose"] for call in backend.calls],
            ["gate", "gate", "gate", "summarize"],
        )
        self.assertIn("Previous output violated: target_not_relevant.", backend.calls[1]["prompt"])
        self.assertEqual(service.read(old.memory_id).body, old.body)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["processing"]["status"], "idle")
        self.assertIn(
            "alpha 项目负责人已更新为乙。",
            "\n".join(record.memory.body for record in service._read_memories_unlocked("knowledge")),
        )

    def test_wrong_duplicate_is_retained_as_deferred_on_final_retry(self):
        backend = QueueBackend()
        service = self.service(backend, name="same-project-wrong-duplicate-final-retry")
        old = service.create_memory(
            memory_id="mem-beta-duplicate-final-retry",
            title="beta 项目状态",
            body="beta 项目状态保持不变。",
            type="fact",
            scopes=["project:beta"],
        )
        user_key, assistant_key = self.capture_turn(
            service,
            turn="same-project-wrong-duplicate-final-retry",
            user_event="same-project-wrong-duplicate-final-retry-user",
            assistant_event="same-project-wrong-duplicate-final-retry-assistant",
            user="alpha 项目状态保持不变；beta 项目状态也保持不变。",
            assistant="已确认两个项目状态均未变化。",
        )
        wrong = self.candidate(
            "wrong-duplicate-final-retry",
            [user_key, assistant_key],
            memory="alpha 项目状态保持不变。",
            duplicate=True,
            worth=False,
            type="fact",
        )
        wrong["duplicate_memory_id"] = old.memory_id
        wrong["scopes"] = ["project:alpha"]
        invalid_gate = self.gate([wrong])
        backend.responses.extend([invalid_gate, invalid_gate, invalid_gate])

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "gate"])
        self.assertIn("Previous output violated: target_not_relevant.", backend.calls[1]["prompt"])
        self.assertEqual(service.read(old.memory_id).body, old.body)
        self.assertEqual(len(service._read_memories_unlocked("knowledge")), 1)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["processing"]["status"], "idle")

    def test_gate_and_summary_update_target_mismatch_fails_after_bounded_retries(self):
        backend = QueueBackend()
        service = self.service(backend, name="email-update-mismatch-failure")
        old = service.create_memory(
            memory_id="mem-draft",
            title="Email draft",
            body="A draft email is awaiting confirmation.",
            type="event",
        )
        other = service.create_memory(
            memory_id="mem-other",
            title="Other email",
            body="An unrelated email fact.",
            type="event",
        )
        user_key, _ = self.capture_turn(
            service,
            turn="sent",
            user_event="sent-user",
            assistant_event="sent-assistant",
            user="Confirm the email is sent.",
        )
        candidate = self.candidate(
            "email-sent",
            [user_key],
            memory="The email was sent.",
            type="event",
            update_memory_id=old.memory_id,
        )
        bad_summary = self.summary(
            user_key,
            title="Sent",
            body="The email was sent.",
            type="event",
            update_memory_id=other.memory_id,
        )
        backend.responses.extend([self.gate([candidate]), bad_summary, bad_summary, bad_summary])

        with self.assertRaises(ModelOutputError) as raised:
            service.process()

        self.assertEqual(raised.exception.validation_detail, "invalid_update_target")
        self.assertEqual(raised.exception.stage, "summarize")
        self.assertEqual(raised.exception.attempt_count, 3)
        marker = self.processed(service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["failure_stage"], "summarize")
        self.assertEqual(marker["attempt_count"], 3)
        self.assertEqual(service.read(old.memory_id).body, old.body)
        self.assertEqual(service.read(other.memory_id).body, other.body)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"].get("watermark", 0), 0)

    def test_gate_and_summary_update_type_mismatches_are_retried(self):
        backend = QueueBackend()
        service = self.service(backend, name="update-type-mismatch-retry")
        existing = service.create_memory(
            memory_id="mem-existing",
            title="Existing fact",
            body="The existing fact.",
            type="fact",
            scopes=["global"],
        )
        user_key, _ = self.capture_turn(
            service,
            turn="update-type-mismatch",
            user_event="update-type-user",
            assistant_event="update-type-assistant",
        )
        wrong_gate = self.candidate(
            "wrong-gate-type",
            [user_key],
            type="project",
            update_memory_id=existing.memory_id,
        )
        corrected_gate = self.candidate(
            "correct-gate-type",
            [user_key],
            type="fact",
            update_memory_id=existing.memory_id,
        )
        backend.responses.extend(
            [
                self.gate([wrong_gate]),
                self.gate([corrected_gate]),
                self.summary(user_key, title="Updated", body="Wrong summary type.", type="project", update_memory_id=existing.memory_id),
                self.summary(user_key, title="Updated", body="The fact is updated.", type="fact", update_memory_id=existing.memory_id),
            ]
        )

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "summarize", "summarize"])
        self.assertIn("Previous output violated: update_target_type_mismatch.", backend.calls[1]["prompt"])
        self.assertIn("existing active update target's type is immutable", backend.calls[1]["prompt"])
        self.assertIn("Previous output violated: invalid_type.", backend.calls[3]["prompt"])
        self.assertEqual(service.read(existing.memory_id).body, "The fact is updated.")
        self.assertEqual(len(service.vault.list_markdown("history")), 1)

    def test_different_future_use_omits_update_target_and_creates_independent_memory(self):
        backend = QueueBackend()
        service = self.service(backend, name="independent-future-use")
        existing = service.create_memory(
            memory_id="mem-existing-fact",
            title="Existing fact",
            body="The existing fact remains unchanged.",
            type="fact",
            scopes=["global"],
        )
        user_key, _ = self.capture_turn(
            service,
            turn="independent-future-use",
            user_event="independent-future-use-user",
            assistant_event="independent-future-use-assistant",
            user="A separate project needs a deployment checklist.",
            assistant="The checklist is a new future action.",
        )
        candidate = self.candidate(
            "independent-project-todo",
            [user_key],
            memory="Prepare a deployment checklist for a separate project.",
            type="todo",
        )
        candidate["scopes"] = ["project:separate"]
        backend.responses.extend(
            [
                self.gate([candidate]),
                self.summary(
                    user_key,
                    title="Deployment checklist",
                    body="Prepare a deployment checklist for a separate project.",
                    type="todo",
                    scopes=["project:separate"],
                    status="active",
                ),
            ]
        )

        result = service.process()

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(service.read(existing.memory_id).body, existing.body)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        active = self.knowledge(service)
        self.assertEqual(len(active), 2)
        independent = next(record.memory for record in active if record.memory.memory_id != existing.memory_id)
        self.assertEqual(independent.type, "todo")
        self.assertEqual(independent.scopes, ["project:separate"])

    def test_three_gate_update_target_type_mismatches_keep_queue_and_write_nothing(self):
        backend = QueueBackend()
        service = self.service(backend, name="update-target-type-failure")
        existing = service.create_memory(
            memory_id="mem-fact-target",
            title="Existing fact",
            body="The existing fact.",
            type="fact",
            scopes=["global"],
        )
        user_key, _ = self.capture_turn(
            service,
            turn="update-target-type-failure",
            user_event="update-target-type-failure-user",
            assistant_event="update-target-type-failure-assistant",
        )
        wrong_gate = self.candidate(
            "wrong-target-type",
            [user_key],
            memory="The existing fact is updated to a new value.",
            type="project",
            update_memory_id=existing.memory_id,
        )
        backend.responses.extend([self.gate([wrong_gate])] * 3)

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["memory_ids"], [])
        self.assertEqual(result["deferred_candidates"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate"] * 3)
        entry = self.processed(service)["sessions"]["codex/s"]["processed_turns"][0]
        self.assertEqual(len(entry["deferred_candidates"]), 1)
        self.assertEqual(entry["deferred_candidates"][0]["candidate_id"], "wrong-target-type")
        self.assertEqual(entry["deferred_candidates"][0]["reason"], "update_target_type_mismatch")
        self.assertEqual(self.processed(service)["sessions"]["codex/s"].get("watermark"), 1)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["processing"]["status"], "idle")
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").is_file())
        self.assertEqual(service.read(existing.memory_id).body, existing.body)
        self.assertEqual(service._read_memories_unlocked("history"), [])

    def test_mixed_project_summary_retries_before_writing(self):
        backend = QueueBackend()
        service = self.service(backend, name="mixed-summary-retry")
        user_key, _ = self.capture_turn(
            service,
            turn="mixed-summary",
            user_event="mixed-summary-user",
            assistant_event="mixed-summary-assistant",
        )
        candidate = self.candidate(
            "mixed-summary-candidate",
            [user_key],
            memory="zhongyin project topic",
            type="project",
        )
        candidate["scopes"] = ["project:zhongyin"]
        mixed_summary = self.summary(
            user_key,
            title="One topic",
            body="One project topic.",
            type="project",
            scopes=["project:zhongyin", "project:morgan"],
        )
        corrected_summary = self.summary(
            user_key,
            title="One topic",
            body="One project topic.",
            type="project",
            scopes=["project:zhongyin"],
        )
        backend.responses.extend([self.gate([candidate]), mixed_summary, corrected_summary])

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize", "summarize"])
        self.assertIn("Previous output violated: mixed_project_scopes.", backend.calls[2]["prompt"])
        self.assertEqual(service._read_memories_unlocked("knowledge")[0].memory.scopes, ["project:zhongyin"])

    def test_three_mixed_project_summary_failures_keep_inbox_and_watermark(self):
        backend = QueueBackend()
        service = self.service(backend, name="mixed-summary-failure")
        user_key, _ = self.capture_turn(
            service,
            turn="mixed-summary-failure",
            user_event="mixed-summary-failure-user",
            assistant_event="mixed-summary-failure-assistant",
        )
        candidate = self.candidate(
            "mixed-summary-failure-candidate",
            [user_key],
            memory="zhongyin project topic",
            type="project",
        )
        candidate["scopes"] = ["project:zhongyin"]
        mixed_summary = self.summary(
            user_key,
            title="One topic",
            body="One project topic.",
            type="project",
            scopes=["project:zhongyin", "project:morgan"],
        )
        backend.responses.extend([self.gate([candidate]), mixed_summary, mixed_summary, mixed_summary])

        with self.assertRaises(ModelOutputError) as raised:
            service.process()

        self.assertEqual(raised.exception.validation_detail, "mixed_project_scopes")
        self.assertEqual(raised.exception.stage, "summarize")
        self.assertEqual(raised.exception.attempt_count, 3)
        marker = self.processed(service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["failure_stage"], "summarize")
        self.assertEqual(marker["validation_detail"], "mixed_project_scopes")
        self.assertEqual(marker["attempt_count"], 3)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"].get("watermark", 0), 0)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").is_file())
        self.assertEqual(service._read_memories_unlocked("knowledge"), [])

    def test_chinese_relative_date_alias_retries_to_absolute_date(self):
        anchor = "2026-09-01T02:01:41Z"
        backend = QueueBackend()
        with patch("memleaf.capture._timestamp", return_value=anchor):
            service = self.service(backend, name="chinese-relative-retry-anchored")
            user_key, _ = self.capture_turn(
                service,
                turn="chinese-relative",
                user_event="chinese-relative-user",
                assistant_event="chinese-relative-assistant",
            )
            candidate = self.candidate(
                "chinese-relative-candidate",
                [user_key],
                memory="完成截止日期",
                type="todo",
            )
            backend.responses.extend(
                [
                    self.gate([candidate]),
                    self.summary(user_key, title="截止昨日", body="昨日完成。", type="todo", status="active"),
                    self.summary(user_key, title="截止日期", body="截止日期为2026-08-31。", type="todo", status="active"),
                ]
            )
            result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize"])
        self.assertEqual(service._read_memories_unlocked("knowledge")[0].memory.body, "2026-08-31完成。")

    def test_ambiguous_relative_date_is_deferred_without_guessing(self):
        anchor = "2026-09-01T02:01:41Z"
        backend = QueueBackend()
        service = self.service(backend, name="chinese-relative-failure")
        with patch("memleaf.capture._timestamp", return_value=anchor):
            user_key, _ = self.capture_turn(
                service,
                turn="chinese-relative-failure",
                user_event="chinese-relative-failure-user",
                assistant_event="chinese-relative-failure-assistant",
            )
        candidate = self.candidate(
            "chinese-relative-failure-candidate",
            [user_key],
            memory="完成截止日期",
            type="todo",
        )
        bad_summary = self.summary(
            user_key,
            title="截止本周末",
            body="本周末完成。",
            type="todo",
            status="active",
        )
        backend.responses.extend([self.gate([candidate]), bad_summary, bad_summary, bad_summary])

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(
            [call["purpose"] for call in backend.calls],
            ["gate", "summarize", "summarize", "summarize"],
        )
        self.assertIn(RELATIVE_TIME_CORRECTION, backend.calls[2]["prompt"])
        self.assertIn("Previous output violated: relative_time.", backend.calls[2]["prompt"])
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["deferred_candidates"], 1)
        marker = self.processed(service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["status"], "idle")
        entry = self.processed(service)["sessions"]["codex/s"]["processed_turns"][0]
        self.assertEqual(entry["deferred_candidates"][0]["reason"], "relative_time")
        self.assertEqual(self.processed(service)["sessions"]["codex/s"].get("watermark", 0), 1)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").is_file())

    def test_relative_time_candidate_is_deferred_without_blocking_other_candidates(self):
        anchor = "2026-09-02T02:01:41Z"
        backend = QueueBackend()
        service = self.service(backend, name="relative-candidate-isolation")
        with patch("memleaf.capture._timestamp", return_value=anchor):
            user_key, assistant_key = self.capture_turn(
                service,
                turn="relative-candidate-isolation",
                user_event="relative-candidate-isolation-user",
                assistant_event="relative-candidate-isolation-assistant",
            )
        ambiguous = self.candidate(
            "ambiguous-date",
            [user_key],
            memory="项目截止日期需要确认。",
            type="todo",
        )
        valid = self.candidate(
            "valid-fact",
            [user_key],
            memory="项目负责人已确认。",
            type="fact",
        )
        bad_summary = self.summary(
            user_key,
            title="截止本周末",
            body="本周末完成。",
            type="todo",
        )
        good_summary = self.summary(
            user_key,
            title="项目负责人",
            body="项目负责人已确认。",
            type="fact",
        )
        backend.responses.extend(
            [self.gate([ambiguous, valid]), bad_summary, bad_summary, bad_summary, good_summary]
        )

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(result["deferred_candidates"], 1)
        self.assertEqual(len(self.knowledge(service)), 1)
        self.assertEqual(self.knowledge(service)[0].memory.body, "项目负责人已确认。")
        entry = self.processed(service)["sessions"]["codex/s"]["processed_turns"][0]
        self.assertEqual(
            [item["candidate_id"] for item in entry["deferred_candidates"]],
            ["ambiguous-date"],
        )
        self.assertEqual(entry["deferred_candidates"][0]["reason"], "relative_time")
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["processing"]["status"], "idle")
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["watermark"], 1)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").is_file())

    def test_shared_update_target_is_reconciled_before_writer(self):
        """Multiple admitted updates need one complete model decision before commit."""
        backend = QueueBackend()
        service = self.service(backend, name="shared-update-target")
        existing = service.create_memory(memory_id="mem-existing", title="Existing fact",
            body="The existing fact.", type="fact", scopes=["global"])
        user_key, _ = self.capture_turn(service, turn="shared-update",
            user_event="shared-user", assistant_event="shared-assistant",
            user="The first update is true. The second update is also true.", assistant="Noted.")
        candidates = [
            self.candidate("update-one", [user_key], memory="The first update is true.",
                update_memory_id=existing.memory_id),
            self.candidate("update-two", [user_key], memory="The second update is also true.",
                update_memory_id=existing.memory_id),
        ]
        merged = json.loads(self.summary(user_key, title="Existing fact",
            body="The existing fact now includes both updates.", update_memory_id=existing.memory_id))
        backend.responses.extend([
            self.gate(candidates),
            self.summary(user_key, title="Existing fact", body="The first update is true.",
                update_memory_id=existing.memory_id),
            self.summary(user_key, title="Existing fact", body="The second update is also true.",
                update_memory_id=existing.memory_id),
            json.dumps({"decision": "UPDATE", "candidate_ids": ["update-one", "update-two"],
                "summary": merged}),
        ])
        result = service.process()
        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls],
            ["gate", "summarize", "summarize", "summarize"])
        self.assertIn("SAME_TARGET_RECONCILIATION", backend.calls[-1]["prompt"])
        self.assertEqual(service.read(existing.memory_id).body,
            "The existing fact now includes both updates.")
        self.assertEqual(len(service.vault.list_markdown("history")), 1)
        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["processing"]["status"], "idle")
        self.assertEqual(state["watermark"], 1)
        rows = state["processed_turns"][0]["candidate_dispositions"]
        self.assertEqual({r["candidate_id"] for r in rows}, {"update-one", "update-two"})
        self.assertTrue(all(r["disposition"] == "UPDATE" for r in rows))
        self.assertEqual(len({r["operation_id"] for r in rows}), 1)

    def test_three_invalid_group_responses_defer_updates_and_keep_inbox(self):
        """An invalid reconciliation never writes a partial update or drops evidence."""
        backend = QueueBackend()
        service = self.service(backend, name="shared-update-failure")
        existing = service.create_memory(memory_id="mem-existing", title="Existing fact",
            body="The existing fact.", type="fact", scopes=["global"])
        user_key, _ = self.capture_turn(service, turn="shared-update-failure",
            user_event="failure-user", assistant_event="failure-assistant",
            user="The first update is true. The second update is also true.", assistant="Noted.")
        candidates = [
            self.candidate("update-one", [user_key], memory="The first update is true.",
                update_memory_id=existing.memory_id),
            self.candidate("update-two", [user_key], memory="The second update is also true.",
                update_memory_id=existing.memory_id),
        ]
        backend.responses.extend([
            self.gate(candidates),
            self.summary(user_key, title="Existing fact", body="The first update is true.",
                update_memory_id=existing.memory_id),
            self.summary(user_key, title="Existing fact", body="The second update is also true.",
                update_memory_id=existing.memory_id),
            *[json.dumps({"decision": "NO_CHANGE", "candidate_ids": ["update-one"]})] * 3,
        ])
        result = service.process()
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["deferred_candidates"], 2)
        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["processing"]["status"], "idle")
        self.assertEqual(state["watermark"], 1)
        rows = state["processed_turns"][0]["candidate_dispositions"]
        self.assertTrue(all(r["disposition"] == "DEFERRED" for r in rows))
        self.assertEqual({r["reason"] for r in rows}, {"same_turn_reconciliation_failed"})
        self.assertEqual(len(self.knowledge(service)), 1)
        self.assertEqual(service.read(existing.memory_id).body, existing.body)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").exists())
        self.assertEqual(len(backend.calls), 6)
        self.assertTrue(all("SAME_TARGET_RECONCILIATION" in c["prompt"] for c in backend.calls[-3:]))

    def test_writer_batch_defense_labels_duplicate_update_conflict(self):
        service = self.service(QueueBackend(), name="writer-duplicate-update-defense")
        existing = service.create_memory(
            memory_id="mem-existing",
            title="Existing fact",
            body="The existing fact.",
            type="fact",
            scopes=["global"],
        )
        self.capture_turn(
            service,
            turn="writer-defense",
            user_event="writer-defense-user",
            assistant_event="writer-defense-assistant",
        )
        turn = parse_inbox(service.vault)[0]
        request = {
            "summary": {"type": "fact", "update_memory_id": existing.memory_id},
            "memory_id": "mem-one",
            "turn": turn,
        }
        with self.assertRaises(ModelOutputError) as raised:
            MemoryWriter(service)._preflight([request, dict(request, memory_id="mem-two")])
        self.assertEqual(raised.exception.validation_detail, "duplicate_update_target")
        self.assertEqual(raised.exception.validation_reason, "schema_violation")
        self.assertEqual(raised.exception.stage, "summarize")

    def test_writer_batch_defense_labels_deterministic_id_conflict(self):
        service = self.service(QueueBackend(), name="writer-deterministic-id-defense")
        self.capture_turn(
            service,
            turn="writer-deterministic",
            user_event="writer-deterministic-user",
            assistant_event="writer-deterministic-assistant",
        )
        turn = parse_inbox(service.vault)[0]
        request = {
            "summary": {"type": "fact"},
            "memory_id": "mem-same",
            "turn": turn,
        }
        with self.assertRaises(ModelOutputError) as raised:
            MemoryWriter(service)._preflight([request, dict(request)])
        self.assertEqual(raised.exception.validation_reason, "schema_violation")
        self.assertEqual(raised.exception.validation_detail, "other_schema_violation")
        self.assertEqual(raised.exception.stage, "summarize")

    def test_remember_skips_gate_native_duplicate_still_writes_and_retries_idempotently(self):
        backend = QueueBackend()

        def native_reader(query):
            return [{"title": "native", "body": "remember this", "hidden": "DO_NOT_PROMPT"}]

        service = self.service(backend, native=native_reader)
        remember_key = event_key("remember-event")
        backend.responses.append(self.summary(remember_key, title="Remembered", body="remember this"))

        first = service.remember(
            "remember this",
            source="codex",
            session_id="remember-session",
            turn_id="remember-turn",
            event_id="remember-event",
        )
        calls_after_first = len(backend.calls)
        second = service.remember(
            "remember this",
            source="codex",
            session_id="remember-session",
            turn_id="remember-turn",
            event_id="remember-event",
        )

        self.assertEqual([call["purpose"] for call in backend.calls], ["summarize"])
        self.assertEqual(first["memories_written"], 1)
        self.assertEqual(second["memory_ids"], first["memory_ids"])
        self.assertEqual(len(backend.calls), calls_after_first)
        self.assertEqual(len(self.knowledge(service)), 1)
        self.assertNotIn("DO_NOT_PROMPT", backend.calls[0]["prompt"])

    def test_prompt_contains_only_visible_turn_and_related_memories(self):
        prompts = []

        def inspect_prompt(prompt, **kwargs):
            prompts.append(prompt)
            return self.gate([])

        backend = QueueBackend([inspect_prompt])

        def native_reader(query):
            return [{"title": "native title", "body": "native related fact", "system_prompt": "PRIVATE"}]

        service = self.service(backend, native=native_reader)
        service.create_memory(title="local title", body="visible user statement local related fact", tags=["local"])
        user_key, _ = self.capture_turn(
            service,
            user="visible user statement",
            assistant="visible assistant answer",
        )
        processed_path = service.vault.processed_index_path
        value = self.processed(service)
        value["sessions"]["codex/s"]["scope"] = "project:visible"
        value["sessions"]["codex/s"]["system_prompt"] = "PRIVATE_SESSION_STATE"
        processed_path.write_text(json.dumps(value), encoding="utf-8")

        service.process()

        self.assertEqual(len(prompts), 1)
        prompt = prompts[0]
        for visible in ("visible user statement", "visible assistant answer", "local related fact", "native related fact", "project:visible"):
            self.assertIn(visible, prompt)
        for hidden in ("PRIVATE", "PRIVATE_SESSION_STATE", "system_prompt"):
            self.assertNotIn(hidden, prompt)
        self.assertIn(user_key, prompt)

    def test_invalid_gate_leaves_retryable_state_and_no_model_is_needed_when_idle(self):
        invalid_gate = json.dumps({"candidates": "invalid"})
        backend = QueueBackend([invalid_gate, invalid_gate, invalid_gate])
        service = self.service(backend)
        self.capture_turn(service)

        with self.assertRaises(ModelOutputError):
            service.process()

        failed = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(failed["processing"]["status"], "failed")
        self.assertNotIn("processed_turns", failed)
        self.assertEqual(len(self.knowledge(service)), 0)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").exists())

        backend.responses.append(self.gate([]))
        self.assertEqual(service.process()["processed_turns"], 1)
        service.router = None
        self.assertEqual(service.process()["processed_turns"], 0)

    def test_invalid_gate_retries_once_with_correction_and_commits_once(self):
        backend = QueueBackend(["```json\n{\"candidates\":[]}\n```", self.gate([])])
        service = self.service(backend)
        self.capture_turn(service)

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(len(self.knowledge(service)), 0)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate"])
        self.assertIn("Correction:", backend.calls[1]["prompt"])
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["processing"]["status"], "idle")
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["watermark"], 1)

    def test_schema_violation_gets_third_correction_attempt_and_commits(self):
        invalid = json.dumps({"candidates": "invalid"})
        backend = QueueBackend([invalid, invalid, self.gate([])])
        service = self.service(backend, name="schema-retry-three")
        self.capture_turn(service)

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(len(backend.calls), 3)
        self.assertTrue(all("Correction:" in call["prompt"] for call in backend.calls[1:]))
        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["processing"]["status"], "idle")
        self.assertEqual(state["watermark"], 1)

    def test_three_schema_violations_fail_with_final_stage_diagnostics(self):
        invalid = json.dumps({"candidates": "invalid"})
        backend = QueueBackend([invalid, invalid, invalid])
        service = self.service(backend, name="schema-retry-failure")
        self.capture_turn(service)

        with self.assertRaises(ModelOutputError):
            service.process()

        marker = self.processed(service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["status"], "failed")
        self.assertEqual(marker["failure_code"], "model_invalid_response")
        self.assertEqual(marker["failure_stage"], "gate")
        self.assertEqual(marker["validation_reason"], "schema_violation")
        self.assertEqual(marker["validation_detail"], "root_shape")
        self.assertEqual(marker["attempt_count"], 3)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"].get("watermark", 0), 0)
        self.assertEqual(len(backend.calls), 3)

    def test_invalid_type_correction_hint_is_safe_and_does_not_echo_first_output(self):
        secret = "FIRST_GATE_OUTPUT_SECRET"
        backend = QueueBackend()
        service = self.service(backend, name="correction-detail")
        user_key, _ = self.capture_turn(service, user="visible user fact")
        invalid = self.gate(
            [self.candidate("bad", [user_key], memory=secret, type="requirement")]
        )
        backend.responses.extend([invalid, self.gate([])])

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertIn("Previous output violated: invalid_type.", backend.calls[1]["prompt"])
        self.assertIn("whenever type is non-null", backend.calls[1]["prompt"])
        self.assertNotIn(secret, backend.calls[1]["prompt"])

    def test_empty_fenced_and_missing_gate_shapes_retry_once(self):
        invalid_outputs = ("", "```json\n{\"candidates\":[]}\n```", "{}")
        for index, invalid_output in enumerate(invalid_outputs):
            with self.subTest(index=index):
                backend = QueueBackend([invalid_output, self.gate([])])
                service = self.service(backend, name=f"gate-retry-{index}")
                self.capture_turn(service)

                result = service.process()

                self.assertEqual(result["processed_turns"], 1)
                self.assertEqual(len(backend.calls), 2)
                self.assertEqual(
                    self.processed(service)["sessions"]["codex/s"]["processing"]["status"],
                    "idle",
                )

    def test_two_invalid_gate_attempts_save_safe_diagnostics_and_keep_inbox(self):
        invalid = "```json\nGATE_OUTPUT_SECRET\n```"
        backend = QueueBackend([invalid, invalid, invalid])
        service = self.service(backend)
        self.capture_turn(service)

        with self.assertRaises(ModelOutputError):
            service.process()

        marker = self.processed(service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["failure_code"], "model_invalid_response")
        self.assertEqual(marker["failure_stage"], "gate")
        self.assertEqual(marker["validation_reason"], "invalid_json")
        self.assertEqual(marker["attempt_count"], 3)
        self.assertNotIn("GATE_OUTPUT_SECRET", json.dumps(marker))
        self.assertNotIn("GATE_OUTPUT_SECRET", backend.calls[1]["prompt"])
        self.assertEqual(self.processed(service)["sessions"]["codex/s"].get("watermark", 0), 0)
        self.assertEqual(len(self.knowledge(service)), 0)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").exists())

    def test_diagnostics_are_opt_in_bounded_and_structural_only(self):
        service = self.service(QueueBackend())
        self.assertFalse(service.vault.logs_path.exists())
        config = service.vault.config()
        config["llm"]["diagnostic_logging"] = True
        save_config(service.vault.config_path, config)
        secret = "MODEL_BODY_SECRET sk-test-123 https://secret.invalid/v1"
        invalid = self.gate(
            [
                dict(
                    self.candidate("c1", ["wrong-event"], memory=secret),
                    unknown_field=secret,
                )
            ]
        )
        backend = QueueBackend([invalid, invalid, invalid])
        service.router = backend
        self.capture_turn(service)

        with self.assertRaises(ModelOutputError):
            service.process()

        path = service.vault.logs_path / "model-diagnostics.jsonl"
        self.assertTrue(path.is_file())
        if os.name == "posix":  # Windows security is governed by inherited ACLs, not mode bits.
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertLessEqual(path.stat().st_size, 256 * 1024)
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 3)
        for index, entry in enumerate(lines):
            self.assertEqual(entry["stage"], "gate")
            self.assertEqual(entry["source"], "codex")
            self.assertEqual(entry["session_id"], "s")
            self.assertEqual(entry["turn_index"], 1)
            self.assertEqual(entry["attempt_count"], index + 1)
            self.assertEqual(entry["top_level_type"], "object")
            self.assertEqual(entry["candidate_count"], 1)
            self.assertEqual(entry["unknown_fields_count"], 1)
            self.assertEqual(len(entry["output_sha256"]), 64)
            self.assertNotIn(secret, json.dumps(entry))
            self.assertNotIn("sk-test-123", json.dumps(entry))
            self.assertNotIn("secret.invalid", json.dumps(entry))
        self.assertEqual(
            self.processed(service)["sessions"]["codex/s"]["processing"]["validation_detail"],
            "unknown_fields",
        )

    def test_diagnostic_write_failure_preserves_model_output_error(self):
        config_service = self.service(QueueBackend())
        config = config_service.vault.config()
        config["llm"]["diagnostic_logging"] = True
        save_config(config_service.vault.config_path, config)
        invalid = json.dumps({"candidates": "invalid"})
        backend = QueueBackend([invalid, invalid, invalid])
        config_service.router = backend
        self.capture_turn(config_service)
        with patch.object(
            ModelExecutor,
            "_write_model_diagnostic",
            side_effect=OSError("diagnostic write secret"),
        ):
            with self.assertRaises(ModelOutputError):
                config_service.process()
        marker = self.processed(config_service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["failure_code"], "model_invalid_response")
        self.assertEqual(marker["validation_detail"], "root_shape")

    def test_dangling_diagnostic_symlink_cannot_receive_logs_or_mask_model_error(self):
        service = self.service(QueueBackend(), name="dangling-diagnostic")
        config = service.vault.config()
        config["llm"]["diagnostic_logging"] = True
        save_config(service.vault.config_path, config)
        service.vault.logs_path.mkdir(mode=0o700)
        target = Path(self.tempdir.name) / "diagnostic-target"
        diagnostic_path = service.vault.logs_path / "model-diagnostics.jsonl"
        diagnostic_path.symlink_to(target)
        invalid = json.dumps({"candidates": "invalid"})
        backend = QueueBackend([invalid, invalid, invalid])
        service.router = backend
        self.capture_turn(service)

        with self.assertRaises(ModelOutputError):
            service.process()

        self.assertTrue(diagnostic_path.is_symlink())
        self.assertFalse(target.exists())
        marker = self.processed(service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["failure_code"], "model_invalid_response")

    def test_backend_empty_content_model_error_retries_once_and_commits(self):
        backend = QueueBackend(
            [
                ModelError(
                    "BACKEND_EMPTY_SECRET",
                    code="model_invalid_response",
                    validation_reason="empty_content",
                ),
                self.gate([]),
            ]
        )
        service = self.service(backend)
        self.capture_turn(service)

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate"])
        self.assertIn("Correction:", backend.calls[1]["prompt"])
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["watermark"], 1)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["processing"]["status"], "idle")

    def test_empty_content_gets_one_additional_attempt_and_third_valid_gate_commits(self):
        empty = lambda: ModelError(
            "EMPTY_RESPONSE_SECRET",
            code="model_invalid_response",
            validation_reason="empty_content",
        )
        backend = QueueBackend([empty(), empty(), self.gate([])])
        service = self.service(backend, name="three-attempt-success")
        self.capture_turn(service)

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(len(backend.calls), 3)
        self.assertIn("Previous output violated: empty_content.", backend.calls[1]["prompt"])
        self.assertIn("Previous output violated: empty_content.", backend.calls[2]["prompt"])
        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["processing"]["status"], "idle")
        self.assertEqual(state["watermark"], 1)

    def test_three_empty_contents_fail_at_attempt_three_without_watermark_or_inbox_loss(self):
        backend = QueueBackend(
            [
                ModelError("EMPTY_ONE_SECRET", code="model_invalid_response", validation_reason="empty_content"),
                ModelError("EMPTY_TWO_SECRET", code="model_invalid_response", validation_reason="empty_content"),
                ModelError("EMPTY_THREE_SECRET", code="model_invalid_response", validation_reason="empty_content"),
            ]
        )
        service = self.service(backend, name="three-attempt-failure")
        self.capture_turn(service)

        with self.assertRaises(ModelError):
            service.process()

        state = self.processed(service)["sessions"]["codex/s"]
        marker = state["processing"]
        self.assertEqual(marker["status"], "failed")
        self.assertEqual(marker["failure_code"], "model_invalid_response")
        self.assertEqual(marker["validation_reason"], "empty_content")
        self.assertEqual(marker["attempt_count"], 3)
        self.assertEqual(state.get("watermark", 0), 0)
        self.assertEqual(len(self.knowledge(service)), 0)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").exists())
        self.assertEqual(len(backend.calls), 3)

    def test_model_response_diagnostics_are_opt_in_and_structural_only(self):
        config_service = self.service(QueueBackend(), name="response-diagnostic")
        config = config_service.vault.config()
        config["llm"]["diagnostic_logging"] = True
        save_config(config_service.vault.config_path, config)
        errors = []
        for index in range(3):
            error = ModelError(
                f"MODEL_BODY_SECRET_{index}",
                code="model_invalid_response",
                validation_reason="empty_content",
            )
            error.with_response_diagnostics(
                {
                    "finish_reason": "length",
                    "completion_tokens": 4096,
                    "content_present": False,
                    "content_chars": 0,
                    "reasoning_present": True,
                    "reasoning_chars": 128,
                    "url": "https://secret.invalid",
                    "api_key": "API_KEY_SECRET",
                }
            )
            errors.append(error)
        backend = QueueBackend(errors)
        config_service.router = backend
        self.capture_turn(config_service)

        with self.assertRaises(ModelError):
            config_service.process()

        path = config_service.vault.logs_path / "model-diagnostics.jsonl"
        self.assertTrue(path.is_file())
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(entries), 3)
        for entry in entries:
            self.assertEqual(entry["finish_reason"], "length")
            self.assertEqual(entry["completion_tokens"], 4096)
            self.assertFalse(entry["content_present"])
            self.assertEqual(entry["content_chars"], 0)
            self.assertTrue(entry["reasoning_present"])
            self.assertEqual(entry["reasoning_chars"], 128)
            self.assertNotIn("SECRET", json.dumps(entry))
            self.assertNotIn("secret.invalid", json.dumps(entry))

    def test_committed_first_turn_survives_second_turn_empty_content_failure(self):
        backend = QueueBackend()
        service = self.service(backend, name="committed-before-failure")
        first_user_key, _ = self.capture_turn(
            service,
            turn="t1",
            user_event="first-u",
            assistant_event="first-a",
            user="first turn visible",
        )
        backend.responses.extend(
            [
                self.gate([self.candidate("first-memory", [first_user_key])]),
                self.summary(first_user_key, title="First committed memory", body="First durable memory"),
            ]
        )
        self.assertEqual(service.process()["processed_turns"], 1)
        first_memory = self.knowledge(service)[0].memory
        first_memory_id = first_memory.memory_id
        first_memory_path = service.vault.memory_path(first_memory_id, "knowledge")
        first_memory_text = first_memory_path.read_text(encoding="utf-8")

        backend.responses.extend(
            [
                ModelError("EMPTY_ONE", code="model_invalid_response", validation_reason="empty_content"),
                ModelError("EMPTY_TWO", code="model_invalid_response", validation_reason="empty_content"),
                ModelError("EMPTY_THREE", code="model_invalid_response", validation_reason="empty_content"),
            ]
        )
        self.capture_turn(
            service,
            turn="t2",
            user_event="second-u",
            assistant_event="second-a",
            user="second turn visible",
        )
        with self.assertRaises(ModelError):
            service.process()

        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["watermark"], 1)
        self.assertEqual([item.memory.memory_id for item in self.knowledge(service)], [first_memory_id])
        self.assertTrue(first_memory_path.is_file())
        self.assertEqual(first_memory_path.read_text(encoding="utf-8"), first_memory_text)
        inbox_path = service.vault.inbox_path / "codex" / "s.md"
        self.assertTrue(inbox_path.exists())
        self.assertIn("second turn visible", inbox_path.read_text(encoding="utf-8"))

    def test_non_invalid_gate_error_is_not_retried(self):
        backend = QueueBackend([ModelError("MODEL_TIMEOUT_SECRET", code="model_timeout")])
        service = self.service(backend)
        self.capture_turn(service)

        with self.assertRaises(ModelError):
            service.process()

        self.assertEqual(len(backend.calls), 1)
        marker = self.processed(service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["failure_code"], "model_timeout")
        self.assertNotIn("MODEL_TIMEOUT_SECRET", json.dumps(marker))

    def test_explicit_remember_retries_invalid_summary_once(self):
        remember_key = event_key("remember-retry")
        backend = QueueBackend(
            [
                "```json\n{\"bad\":true}\n```",
                self.summary(remember_key, title="Retried"),
            ]
        )
        service = self.service(backend)

        result = service.remember(
            "remember this retryable fact",
            source="codex",
            session_id="remember-retry",
            turn_id="remember-retry-turn",
            event_id="remember-retry",
        )

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["summarize", "summarize"])
        self.assertIn("Correction:", backend.calls[1]["prompt"])
        self.assertEqual(self.processed(service)["sessions"]["codex/remember-retry"]["processing"]["status"], "idle")

    def test_invalid_second_summary_does_not_write_first_summary(self):
        backend = QueueBackend()
        service = self.service(backend)
        user_key, assistant_key = self.capture_turn(service, user="A durable fact. A second durable fact.")
        candidates = [self.candidate("c1", [user_key]), self.candidate("c2", [user_key], memory="A second durable fact")]
        backend.responses.extend(
            [
                self.gate(candidates),
                self.summary(user_key, title="first"),
                json.dumps({"bad": True}),
                json.dumps({"bad": True}),
                json.dumps({"bad": True}),
            ]
        )

        with self.assertRaises(ModelOutputError):
            service.process()

        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["processing"]["status"], "failed")
        self.assertEqual(state.get("watermark", 0), 0)
        self.assertEqual(state.get("processed_turns", []), [])
        self.assertEqual(len(self.knowledge(service)), 0)

        backend.responses.extend(
            [self.gate(candidates), self.summary(user_key, title="first"), self.summary(user_key, title="second")]
        )
        self.assertEqual(service.process()["memories_written"], 2)

    def test_second_model_failure_does_not_advance_watermark(self):
        backend = QueueBackend()
        service = self.service(backend)
        user_key, assistant_key = self.capture_turn(service, user="A durable fact. A second durable fact.")
        candidates = [self.candidate("c1", [user_key]), self.candidate("c2", [user_key], memory="A second durable fact")]
        backend.responses.extend(
            [self.gate(candidates), self.summary(user_key, title="first"), ModelError("second failed")]
        )

        with self.assertRaises(ModelError):
            service.process()

        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state.get("watermark", 0), 0)
        self.assertEqual(len(self.knowledge(service)), 0)

    def test_failed_marker_records_safe_model_code_and_retry_replaces_it(self):
        backend = QueueBackend()
        service = self.service(backend)
        self.capture_turn(service, user="visible user", assistant="visible assistant")
        backend.responses.append(ModelError("MODEL_RESPONSE_SECRET", code="model_timeout", stage="gate"))

        with self.assertRaises(ModelError):
            service.process()

        failed = self.processed(service)["sessions"]["codex/s"]
        marker = failed["processing"]
        self.assertEqual(marker["status"], "failed")
        self.assertEqual(marker["failure_code"], "model_timeout")
        self.assertEqual(marker["failure_stage"], "gate")
        self.assertNotIn("MODEL_RESPONSE_SECRET", json.dumps(marker))
        self.assertEqual(failed.get("watermark", 0), 0)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").exists())

        backend.responses.append(self.gate([]))
        self.assertEqual(service.process()["processed_turns"], 1)
        retried = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(retried["processing"]["status"], "idle")
        self.assertEqual(retried["watermark"], 1)

    def test_partial_knowledge_write_is_retryable_without_duplicate_files(self):
        backend = QueueBackend()
        service = self.service(backend)
        user_key, assistant_key = self.capture_turn(service, user="A durable fact. A second durable fact.")
        candidates = [self.candidate("c1", [user_key]), self.candidate("c2", [user_key], memory="A second durable fact")]
        responses = [self.gate(candidates), self.summary(user_key, title="first"), self.summary(user_key, title="second")]
        backend.responses.extend(responses)
        import memleaf.memory_writer as memory_writer_module

        original_write = memory_writer_module.atomic_write_text
        knowledge_writes = {"count": 0}

        def flaky_write(path, text):
            if path.parent.name == "knowledge":
                knowledge_writes["count"] += 1
                if knowledge_writes["count"] == 2:
                    raise OSError("controlled write failure")
            return original_write(path, text)

        with patch.object(memory_writer_module, "atomic_write_text", side_effect=flaky_write):
            with self.assertRaises(OSError):
                service.process()

        self.assertEqual(len(self.knowledge(service)), 1)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"].get("watermark", 0), 0)
        backend.responses.extend(responses)
        result = service.process()

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(len(self.knowledge(service)), 2)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["watermark"], 1)

    def test_sources_are_core_generated_and_todo_defaults_active(self):
        backend = QueueBackend()
        service = self.service(backend)
        user_key, assistant_key = self.capture_turn(service, user="I need to submit the report.")
        backend.responses.extend(
            [
                self.gate([self.candidate("todo", [user_key], type="todo")]),
                self.summary(
                    user_key,
                    title="Todo",
                    body="Do the thing",
                    type="todo",
                ),
            ]
        )

        service.process()
        memory = self.knowledge(service)[0].memory

        self.assertEqual(memory.status, "active")
        self.assertEqual({item["event_key"] for item in memory.sources}, {user_key, assistant_key})
        self.assertTrue(all(item["session_id"] == "s" for item in memory.sources))
        self.assertTrue(all(item["conversation_title"] != "MODEL_FORGED_TITLE" for item in memory.sources))
        self.assertTrue(all(item["turn_id"] == "t1" for item in memory.sources))

    def test_source_session_filters_incomplete_turns_and_missing_middle(self):
        backend = QueueBackend()
        service = self.service(backend)
        self.capture_turn(service, source="a", session="s1", turn="t1", user_event="a1u", assistant_event="a1a")
        self.capture_turn(service, source="a", session="s2", turn="t1", user_event="a2u", assistant_event="a2a")
        self.capture_turn(service, source="b", session="s1", turn="t1", user_event="b1u", assistant_event="b1a")
        backend.responses.extend([self.gate([])])

        result = service.process(source="a", session_id="s2")
        self.assertEqual(result["processed_turns"], 1)
        states = self.processed(service)["sessions"]
        self.assertNotIn("watermark", states["a/s1"])
        self.assertNotIn("watermark", states["b/s1"])

        gap = self.service(QueueBackend([self.gate([])]), name="gap")
        self.capture_turn(gap, turn="t1", user_event="g1u", assistant_event="g1a")
        self.capture_turn(gap, turn="t2", user_event="g2u", assistant_event=None)
        self.capture_turn(gap, turn="t3", user_event="g3u", assistant_event="g3a")

        self.assertEqual(gap.process()["processed_turns"], 1)
        self.assertEqual(gap.process()["processed_turns"], 0)
        gap_state = self.processed(gap)["sessions"]["codex/s"]
        self.assertEqual(gap_state["watermark"], 1)
        self.assertEqual(len(gap_state["processed_turns"]), 1)

    def test_orphaned_processing_marker_is_recoverable_and_remember_does_not_overwrite_live_owner(self):
        backend = QueueBackend([self.gate([])])
        service = self.service(backend)
        self.capture_turn(service)
        value = self.processed(service)
        value["sessions"]["codex/s"]["processing"] = {
            "status": "processing",
            "token": "orphan",
            "started_at": "2000-01-01T00:00:00Z",
        }
        service.vault.processed_index_path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(service.process()["processed_turns"], 1)

        live = self.processed(service)
        live["sessions"]["codex/live"] = {
            "processing": {
                "status": "processing",
                "token": "owned",
                "owner_pid": os.getpid(),
                "started_at": "2026-08-23T23:40:00Z",
            }
        }
        service.vault.processed_index_path.write_text(json.dumps(live), encoding="utf-8")
        with self.assertRaises(ProcessingError):
            service.remember("conflicting remember", source="codex", session_id="live", event_id="r")
        self.assertEqual(self.processed(service)["sessions"]["codex/live"]["processing"]["token"], "owned")

    def test_dead_processing_owner_is_recovered_immediately(self):
        backend = QueueBackend([self.gate([])])
        service = self.service(backend)
        self.capture_turn(service)
        value = self.processed(service)
        value["sessions"]["codex/s"]["processing"] = {
            "status": "processing",
            "token": "dead-owner",
            "owner_pid": 12345,
            "started_at": "2026-08-24T00:00:00Z",
        }
        service.vault.processed_index_path.write_text(json.dumps(value), encoding="utf-8")
        with patch("memleaf.process_journal.ProcessJournal._owner_pid_status", return_value=False):
            self.assertEqual(service.process()["processed_turns"], 1)

    def test_legacy_processing_marker_uses_short_grace_period(self):
        backend = QueueBackend([self.gate([])])
        service = self.service(backend)
        self.capture_turn(service)
        value = self.processed(service)
        value["sessions"]["codex/s"]["processing"] = {
            "status": "processing",
            "token": "legacy-live",
            "started_at": "2026-08-23T23:55:00Z",
        }
        service.vault.processed_index_path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(service.process()["processed_turns"], 0)
        self.clock.value += timedelta(minutes=6)
        self.assertEqual(service.process()["processed_turns"], 1)

    def test_process_and_remember_markers_record_owner_pid(self):
        service = self.service(QueueBackend([self.gate([])]))
        self.capture_turn(service)
        observed = {}

        def inspect_process(prompt, **kwargs):
            del prompt, kwargs
            processed = self.processed(service)
            observed["process"] = processed["sessions"]["codex/s"]["processing"]["owner_pid"]
            return self.gate([])

        service.router = QueueBackend([inspect_process])
        service.process()
        self.assertEqual(observed["process"], os.getpid())

        remember_service = self.service(QueueBackend([self.summary(event_key("remember-owner"))]), name="remember-owner")
        observed_remember = {}

        def inspect_remember(prompt, **kwargs):
            del prompt, kwargs
            processed = self.processed(remember_service)
            observed_remember["remember"] = processed["sessions"]["codex/remember"]["processing"]["owner_pid"]
            return self.summary(event_key("remember-owner"))

        remember_service.router = QueueBackend([inspect_remember])
        remember_service.remember("owner marker", source="codex", session_id="remember", event_id="remember-owner")
        self.assertEqual(observed_remember["remember"], os.getpid())

    def test_model_callback_can_acquire_vault_lock_because_calls_are_outside_lock(self):
        active = {"value": False}
        queue = [self.gate([])]
        service = self.service()
        original_lock = service.vault.lock

        @contextmanager
        def probe_lock():
            with original_lock():
                active["value"] = True
                try:
                    yield
                finally:
                    active["value"] = False

        class LockCheckingBackend:
            provider = "fake"
            model = "lock-check"

            def complete(self, prompt, **kwargs):
                if active["value"]:
                    raise AssertionError("model called while vault lock is held")
                with service.vault.lock():
                    pass
                return queue.pop(0)

        service.vault.lock = probe_lock
        service.router = LockCheckingBackend()
        self.capture_turn(service)

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)

    def test_model_unavailable_is_explicit_when_work_exists(self):
        service = self.service()
        self.capture_turn(service)
        with self.assertRaises(ModelUnavailable):
            service.process()
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["processing"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
