"""Regression coverage for automatic admission of operational noise."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf
from memleaf.index import event_key
from memleaf.memory_writer import MemoryWriter
from memleaf.prompts import GATE_SYSTEM, SUMMARIZE_SYSTEM, gate_prompt, summarize_prompt
from memleaf.validation import ModelOutputError


class QueueBackend:
    provider = "fake"
    model = "admission-noise-test"

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[dict[str, str]] = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        del temperature
        self.calls.append({"prompt": prompt, "system": system, "purpose": purpose})
        if not self.responses:
            raise AssertionError("test model response queue exhausted")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def gate(candidates):
    return json.dumps({"candidates": candidates}, ensure_ascii=False)


def candidate(
    candidate_id,
    evidence,
    *,
    memory,
    type="fact",
    duplicate=False,
    worth=True,
    duplicate_memory_id=None,
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
    if duplicate_memory_id is not None:
        value["duplicate_memory_id"] = duplicate_memory_id
    if update_memory_id is not None:
        value["update_memory_id"] = update_memory_id
    return value


def summary(event, *, title, body, type="fact", update_memory_id=None):
    value = {
        "title": title,
        "body": body,
        "tags": ["admission-test"],
        "type": type,
        "scopes": ["global"],
        "scope_source": "model",
        "sources": [{"event_key": event}],
    }
    if update_memory_id is not None:
        value["update_memory_id"] = update_memory_id
    return json.dumps(value, ensure_ascii=False)


class AdmissionPromptTests(unittest.TestCase):
    def test_automatic_gate_has_explicit_operational_noise_boundary(self):
        text = " ".join(GATE_SYSTEM.casefold().split())
        for phrase in (
            "in the automatic capture/process path",
            "return candidates=[]",
            "mcp/tool connection",
            "failure diagnosis",
            "do not keep it merely to avoid a future investigation",
            "same operational incident spans stats/search/remember",
            "separate, user-confirmed future-use fact",
            "preference, identity, constraint",
            "project risk",
            "durable lesson",
            "pure read-only query",
            "never set duplicate_memory_id",
            "append its source",
            "request to invoke or test the remember tool",
            "do not store the failure report",
            "explicit remember mode",
        ):
            self.assertIn(phrase, text)

        prompt = gate_prompt(
            [{"event_key": "event-1", "role": "assistant", "content": "MCP test failed"}],
        )
        self.assertIn("Mode: automatic capture/process", prompt)
        self.assertIn("does not prove it succeeded", prompt)
        self.assertIn("A pure query answered by restating a related active memory is read-only", prompt)
        self.assertIn("do not set duplicate_memory_id or update_memory_id", prompt)
        self.assertIn("no admissible future-use information", gate_prompt([]))

    def test_summary_preserves_business_exception_without_importing_diagnostics(self):
        text = " ".join(SUMMARIZE_SYSTEM.casefold().split())
        for phrase in (
            "never summarize a pure mcp/tool connectivity test",
            "failed outcome or surrounding diagnosis",
            "user-confirmed future-use preference, identity, constraint",
            "summarize only that future-use topic",
            "summarize only the requested object",
            "do not append tool/test diagnostics",
        ):
            self.assertIn(phrase, text)

        event = {"event_key": "event-1", "role": "user", "content": "a project risk"}
        prompt = summarize_prompt(
            candidate("risk", ["event-1"], memory="a project risk"),
            [event],
            explicit=False,
        )
        self.assertIn("Mode: candidate passed the gate", prompt)


class AdmissionFlowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="memleaf-admission-")
        self.service = None

    def tearDown(self):
        self.tempdir.cleanup()

    def make_service(self, name):
        self.service = Memleaf(Path(self.tempdir.name) / name)
        return self.service

    @staticmethod
    def capture_turn(service, *, source="hermes", session, turn, user, assistant):
        user_key = event_key(f"{session}/{turn}/user")
        assistant_key = event_key(f"{session}/{turn}/assistant")
        service.capture(source, session, turn, "user", user, event_id=f"{session}/{turn}/user")
        service.capture(
            source,
            session,
            turn,
            "assistant",
            assistant,
            event_id=f"{session}/{turn}/assistant",
        )
        return user_key, assistant_key

    def seed_owner_state(self, service, backend, *, marker="ML-QUERY-OWNER-01", session="owner-state"):
        user_key, assistant_key = self.capture_turn(
            service,
            session=session,
            turn="turn-a",
            user=f"{marker} 项目负责人是甲。",
            assistant=f"已确认 {marker} 当前负责人为甲。",
        )
        backend.responses.extend(
            [
                gate(
                    [
                        candidate(
                            "owner-a",
                            [user_key, assistant_key],
                            memory=f"{marker} 项目负责人是甲。",
                            type="identity",
                        )
                    ]
                ),
                summary(
                    user_key,
                    title=f"{marker} 项目负责人",
                    body=f"{marker} 项目负责人是甲。",
                    type="identity",
                ),
            ]
        )
        result = service.process(source="hermes", session_id=session, model=backend)
        self.assertEqual(len(result["memory_ids"]), 1)
        return service._read_memories_unlocked("knowledge")[0].memory

    def update_owner_state(self, service, backend, *, old, owner, marker="ML-QUERY-OWNER-01", session="owner-state", turn="turn-b"):
        user_key, assistant_key = self.capture_turn(
            service,
            session=session,
            turn=turn,
            user=f"{marker} 同一项目负责人更新为{owner}。",
            assistant=f"已确认今后以{owner}为准。",
        )
        backend.responses.extend(
            [
                gate(
                    [
                        candidate(
                            f"owner-{owner}",
                            [user_key, assistant_key],
                            memory=f"{marker} 项目负责人更新为{owner}。",
                            type="identity",
                            update_memory_id=old.memory_id,
                        )
                    ]
                ),
                summary(
                    user_key,
                    title=f"{marker} 项目负责人",
                    body=f"{marker} 项目负责人已更新为{owner}。",
                    type="identity",
                    update_memory_id=old.memory_id,
                ),
            ]
        )
        result = service.process(source="hermes", session_id=session, model=backend)
        self.assertEqual(result["memory_ids"], [old.memory_id])
        return service.read(old.memory_id)

    def test_four_turn_mcp_failure_and_failed_remember_stay_out_even_with_related_fault(self):
        service = self.make_service("mcp-failure")
        service.create_memory(
            memory_id="mem-existing-mcp-fault",
            title="Existing operational note",
            body="MCP stdio subprocess exited during an earlier controlled check.",
            tags=["operational"],
            type="fact",
        )
        backend = QueueBackend()
        for index in range(4):
            if index == 2:
                user = "请只通过 memleaf MCP remember 保存 TEST-R3-FAILED-REMEMBER。"
                assistant = "remember 调用失败：MCP stdio subprocess for memleaf has exited。"
            else:
                user = f"只通过 memleaf MCP 执行第 {index} 次 stats/search 连通性测试。"
                assistant = "stats/search 均失败：MCP stdio subprocess for memleaf has exited。"
            user_key, assistant_key = self.capture_turn(
                service,
                session="mcp-failure",
                turn=f"turn-{index + 1}",
                user=user,
                assistant=assistant,
            )
            del user_key, assistant_key
            backend.responses.append(gate([]))

        result = service.process(source="hermes", session_id="mcp-failure", model=backend)

        self.assertEqual(result["processed_turns"], 4)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(
            [record.memory.memory_id for record in service._read_memories_unlocked("knowledge")],
            ["mem-existing-mcp-fault"],
        )
        processed = json.loads(service.vault.processed_index_path.read_text(encoding="utf-8"))
        self.assertEqual(processed["sessions"]["hermes/mcp-failure"]["watermark"], 4)
        # The related fault is visible for duplicate/state comparison, but it
        # cannot turn a pure operational test into an automatic candidate.
        self.assertTrue(any("Existing operational note" in call["prompt"] for call in backend.calls))

    def test_confirmed_project_risk_remains_admissible(self):
        service = self.make_service("project-risk")
        backend = QueueBackend()
        user_key, assistant_key = self.capture_turn(
            service,
            session="project-risk",
            turn="turn-1",
            user="北辰项目存在供应商交付延期风险，请后续跟进。",
            assistant="已确认该项目风险，后续评审需要继续跟进供应商交付。",
        )
        backend.responses.extend(
            [
                gate(
                    [
                        candidate(
                            "project-risk",
                            [user_key, assistant_key],
                            memory="北辰项目存在供应商交付延期风险，需要后续评审跟进。",
                            type="project",
                        )
                    ]
                ),
                summary(
                    user_key,
                    title="北辰项目交付延期风险",
                    body="北辰项目存在供应商交付延期风险，需要后续评审跟进。",
                    type="project",
                ),
            ]
        )

        result = service.process(source="hermes", session_id="project-risk", model=backend)

        self.assertEqual(result["memories_written"], 1)
        memories = service._read_memories_unlocked("knowledge")
        self.assertEqual(len(memories), 1)
        self.assertIn("交付延期风险", memories[0].memory.body)

    def test_combined_orion_digest_is_dropped_but_atomic_action_is_kept(self):
        service = self.make_service("orion-digest")
        backend = QueueBackend()
        user_key, _ = self.capture_turn(
            service,
            session="orion-digest",
            turn="turn-1",
            user="查看 Orion 邮件汇总并告诉我需要关注的事项。",
            assistant="Orion汇总：子任务完成4条；现场增补待受理2条；另派发提供旧版本生产取数脚本。",
        )
        aggregate = candidate(
            "orion-aggregate",
            [user_key],
            memory="Orion汇总（2026-09-02）：子任务完成4条；现场增补待受理2条——另派发子任务提供旧版本生产取数脚本。",
            type="fact",
        )
        aggregate["scopes"] = ["project:orion"]
        atomic = candidate(
            "orion-script-task",
            [user_key],
            memory="Orion需要提供旧版本生产取数脚本。",
            type="todo",
        )
        atomic["scopes"] = ["project:orion"]
        atomic_summary = json.loads(
            summary(
                user_key,
                title="Orion提供旧版本生产取数脚本",
                body="Orion需要提供旧版本生产取数脚本。",
                type="todo",
            )
        )
        atomic_summary.update(
            {
                "scopes": ["project:orion"],
                "status": "active",
            }
        )
        backend.responses.extend(
            [gate([aggregate, atomic]), json.dumps(atomic_summary, ensure_ascii=False)]
        )

        result = service.process(source="hermes", session_id="orion-digest", model=backend)

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(result["deferred_candidates"], 0)
        memories = service._read_memories_unlocked("knowledge")
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].memory.title, "Orion提供旧版本生产取数脚本")
        self.assertNotIn("汇总", memories[0].memory.title)
        self.assertNotIn("完成4条", memories[0].memory.body)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize"])

    def test_mixed_preference_is_kept_while_mcp_failure_is_not(self):
        service = self.make_service("mixed-preference")
        backend = QueueBackend()
        user_key, assistant_key = self.capture_turn(
            service,
            session="mixed-preference",
            turn="turn-1",
            user="以后处理我的项目邮件时请保持简洁；顺便查询 MCP stats。",
            assistant="好的，我会保持邮件简洁；本次 stats 查询失败，但这不改变该偏好。",
        )
        backend.responses.extend(
            [
                gate(
                    [
                        candidate(
                            "concise-project-mail",
                            [user_key, assistant_key],
                            memory="用户偏好项目邮件保持简洁。",
                            type="preference",
                        )
                    ]
                ),
                summary(
                    user_key,
                    title="项目邮件保持简洁",
                    body="用户偏好项目邮件保持简洁。",
                    type="preference",
                ),
            ]
        )

        result = service.process(source="hermes", session_id="mixed-preference", model=backend)

        self.assertEqual(result["memories_written"], 1)
        memories = service._read_memories_unlocked("knowledge")
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].memory.type, "preference")
        self.assertIn("保持简洁", memories[0].memory.body)
        self.assertNotIn("MCP", memories[0].memory.body)

    def test_read_only_query_duplicate_does_not_append_source_or_history(self):
        service = self.make_service("read-only-duplicate")
        backend = QueueBackend()
        old = self.seed_owner_state(service, backend)
        self.update_owner_state(service, backend, old=old, owner="乙")

        active_path = service.vault.memory_path(old.memory_id, "knowledge")
        active_before = active_path.read_text(encoding="utf-8")
        active = service.read(old.memory_id)
        sources_before = list(active.sources)
        updated_before = active.updated
        history_before = [path.name for path in service.vault.list_markdown("history")]

        user_key, assistant_key = self.capture_turn(
            service,
            session="owner-query-duplicate",
            turn="query",
            user="ML-QUERY-OWNER-01 项目负责人是谁？",
            assistant="ML-QUERY-OWNER-01 项目负责人是乙。",
        )
        backend.responses.append(
            gate(
                [
                    candidate(
                        "owner-query-duplicate",
                        [user_key, assistant_key],
                        memory=active.body,
                        type="identity",
                        duplicate=True,
                        worth=False,
                        duplicate_memory_id=old.memory_id,
                    )
                ]
            )
        )

        result = service.process(
            source="hermes",
            session_id="owner-query-duplicate",
            model=backend,
        )

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["memory_ids"], [])
        self.assertEqual(result["metadata_merged"], 0)
        self.assertEqual(active_path.read_text(encoding="utf-8"), active_before)
        current = service.read(old.memory_id)
        self.assertEqual(current.sources, sources_before)
        self.assertEqual(current.updated, updated_before)
        self.assertEqual(
            [path.name for path in service.vault.list_markdown("history")],
            history_before,
        )
        processed = json.loads(service.vault.processed_index_path.read_text(encoding="utf-8"))
        entry = processed["sessions"]["hermes/owner-query-duplicate"]["processed_turns"][0]
        self.assertEqual(entry["memory_ids"], [])

    def test_read_only_query_update_with_identical_summary_is_noop_across_hosts(self):
        service = self.make_service("read-only-update")
        backend = QueueBackend()
        old = self.seed_owner_state(service, backend)
        active_before = service.read(old.memory_id)
        active_path = service.vault.memory_path(old.memory_id, "knowledge")
        active_text_before = active_path.read_text(encoding="utf-8")
        history_before = [path.name for path in service.vault.list_markdown("history")]

        user_key, assistant_key = self.capture_turn(
            service,
            source="codex",
            session="owner-query-update",
            turn="query",
            user="ML-QUERY-OWNER-01 项目负责人是谁？",
            assistant="ML-QUERY-OWNER-01 项目负责人是甲。",
        )
        query_summary = json.loads(
            summary(
                user_key,
                title=active_before.title,
                body=active_before.body,
                type=active_before.type,
                update_memory_id=old.memory_id,
            )
        )
        query_summary["scope_source"] = "session_context"
        query_candidate = candidate(
            "owner-query-update",
            [user_key, assistant_key],
            memory=active_before.body,
            type="identity",
            update_memory_id=old.memory_id,
        )
        query_candidate["scope_source"] = "session_context"
        backend.responses.extend(
            [
                gate([query_candidate]),
                json.dumps(query_summary, ensure_ascii=False),
            ]
        )

        result = service.process(
            source="codex",
            session_id="owner-query-update",
            model=backend,
        )

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["memory_ids"], [])
        self.assertEqual(result["metadata_merged"], 0)
        self.assertEqual(active_path.read_text(encoding="utf-8"), active_text_before)
        self.assertEqual(
            [path.name for path in service.vault.list_markdown("history")],
            history_before,
        )
        current = service.read(old.memory_id)
        self.assertEqual(current.extra, active_before.extra)
        processed = json.loads(service.vault.processed_index_path.read_text(encoding="utf-8"))
        entry = processed["sessions"]["codex/owner-query-update"]["processed_turns"][0]
        self.assertEqual(entry["memory_ids"], [])

    def test_meaningful_owner_update_after_read_only_query_still_archives_previous_state(self):
        service = self.make_service("read-only-then-update")
        backend = QueueBackend()
        old = self.seed_owner_state(service, backend)
        self.update_owner_state(service, backend, old=old, owner="乙")

        query_user, query_assistant = self.capture_turn(
            service,
            session="owner-query",
            turn="query",
            user="ML-QUERY-OWNER-01 项目负责人是谁？",
            assistant="ML-QUERY-OWNER-01 项目负责人是乙。",
        )
        active = service.read(old.memory_id)
        backend.responses.append(
            gate(
                [
                    candidate(
                        "owner-query",
                        [query_user, query_assistant],
                        memory=active.body,
                        type="identity",
                        duplicate=True,
                        worth=False,
                        duplicate_memory_id=old.memory_id,
                    )
                ]
            )
        )
        query_result = service.process(source="hermes", session_id="owner-query", model=backend)
        self.assertEqual(query_result["memory_ids"], [])

        update_user, update_assistant = self.capture_turn(
            service,
            session="owner-update-after-query",
            turn="update",
            user="ML-QUERY-OWNER-01 同一项目负责人更新为丙。",
            assistant="已确认今后以丙为准。",
        )
        backend.responses.extend(
            [
                gate(
                    [
                        candidate(
                            "owner-c",
                            [update_user, update_assistant],
                            memory="ML-QUERY-OWNER-01 项目负责人更新为丙。",
                            type="identity",
                            update_memory_id=old.memory_id,
                        )
                    ]
                ),
                summary(
                    update_user,
                    title="ML-QUERY-OWNER-01 项目负责人",
                    body="ML-QUERY-OWNER-01 项目负责人已更新为丙。",
                    type="identity",
                    update_memory_id=old.memory_id,
                ),
            ]
        )
        result = service.process(
            source="hermes",
            session_id="owner-update-after-query",
            model=backend,
        )

        self.assertEqual(result["memory_ids"], [old.memory_id])
        self.assertEqual(service.read(old.memory_id).body, "ML-QUERY-OWNER-01 项目负责人已更新为丙。")
        history = service._read_memories_unlocked("history")
        self.assertEqual(len(history), 2)
        self.assertEqual(
            {memory.memory.body for memory in history},
            {
                "ML-QUERY-OWNER-01 项目负责人是甲。",
                "ML-QUERY-OWNER-01 项目负责人已更新为乙。",
            },
        )

    def test_successful_explicit_remember_keeps_only_requested_object(self):
        service = self.make_service("explicit-remember")
        raw_event_id = "explicit-remember-event"
        remember_key = event_key(raw_event_id)
        backend = QueueBackend(
            [
                summary(
                    remember_key,
                    title="User requested memory marker",
                    body="TEST-R3-EXPLICIT: retain this requested marker.",
                )
            ]
        )

        result = service.remember(
            "TEST-R3-EXPLICIT: retain this requested marker.",
            source="hermes",
            session_id="explicit-remember",
            turn_id="remember-turn",
            event_id=raw_event_id,
            model=backend,
        )

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(len(service._read_memories_unlocked("knowledge")), 1)
        self.assertEqual(
            service._read_memories_unlocked("knowledge")[0].memory.body,
            "TEST-R3-EXPLICIT: retain this requested marker.",
        )

        existing = service._read_memories_unlocked("knowledge")[0].memory
        second_event_id = "explicit-remember-second-event"
        second_key = event_key(second_event_id)
        backend.responses.append(
            summary(
                second_key,
                title=existing.title,
                body=existing.body,
                type=existing.type,
                update_memory_id=existing.memory_id,
            )
        )
        second = service.remember(
            existing.body,
            source="hermes",
            session_id="explicit-remember-second",
            turn_id="remember-second-turn",
            event_id=second_event_id,
            model=backend,
        )

        self.assertEqual(second["memories_written"], 1)
        self.assertEqual(second["memory_ids"], [existing.memory_id])
        self.assertEqual(len(service._read_memories_unlocked("history")), 1)

    def test_empty_writer_batch_clears_previous_result_metadata(self):
        service = self.make_service("empty-writer-batch")
        writer = MemoryWriter(service)
        writer.last_metadata_merged = 4
        writer.last_noop_memory_ids = {"stale"}

        self.assertEqual(writer.write_many_unlocked([], now="2026-08-27T00:00:00Z"), [])
        self.assertEqual(writer.last_metadata_merged, 0)
        self.assertEqual(writer.last_noop_memory_ids, set())

    def test_natural_project_owner_update_reuses_id_and_archives_old_state(self):
        service = self.make_service("natural-owner-update")
        backend = QueueBackend()

        first_user, first_assistant = self.capture_turn(
            service,
            session="natural-owner-update",
            turn="turn-1",
            user="ML-STATE-20260827 项目负责人是甲，后续按项目查询负责人。",
            assistant="已确认 ML-STATE-20260827 当前负责人为甲。",
        )
        backend.responses.extend(
            [
                gate(
                    [
                        candidate(
                            "owner",
                            [first_user, first_assistant],
                            memory="ML-STATE-20260827 项目负责人是甲。",
                            type="identity",
                        )
                    ]
                ),
                summary(
                    first_user,
                    title="ML-STATE-20260827 项目负责人",
                    body="ML-STATE-20260827 项目负责人是甲。",
                    type="identity",
                ),
            ]
        )
        first_result = service.process(
            source="hermes",
            session_id="natural-owner-update",
            model=backend,
        )
        old = service._read_memories_unlocked("knowledge")[0].memory

        second_user, second_assistant = self.capture_turn(
            service,
            session="natural-owner-update",
            turn="turn-2",
            user="ML-STATE-20260827 同一项目负责人更新为乙。",
            assistant="已确认今后以乙为准。",
        )
        updated = candidate(
            "owner-update",
            [second_user, second_assistant],
            memory="ML-STATE-20260827 项目负责人已更新为乙。",
            type="identity",
            update_memory_id=old.memory_id,
        )
        backend.responses.extend(
            [
                gate([updated]),
                summary(
                    second_user,
                    title="ML-STATE-20260827 项目负责人",
                    body="ML-STATE-20260827 项目负责人已更新为乙。",
                    type="identity",
                    update_memory_id=old.memory_id,
                ),
            ]
        )
        second_result = service.process(
            source="hermes",
            session_id="natural-owner-update",
            model=backend,
        )

        self.assertEqual(first_result["processed_turns"], 1)
        self.assertEqual(first_result["memories_written"], 1)
        self.assertEqual(second_result["processed_turns"], 1)
        self.assertEqual(second_result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize", "gate", "summarize"])
        active = service._read_memories_unlocked("knowledge")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].memory.memory_id, old.memory_id)
        self.assertEqual(active[0].memory.body, "ML-STATE-20260827 项目负责人已更新为乙。")
        self.assertNotIn("当前负责人是甲", active[0].memory.body)
        history = service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.body, old.body)
        self.assertEqual(history[0].memory.body, "ML-STATE-20260827 项目负责人是甲。")
        self.assertEqual(history[0].memory.extra["active_memory_id"], old.memory_id)
        self.assertEqual(
            [memory.memory_id for memory in service.search(
                "ML-STATE-20260827 项目负责人",
                include_history=False,
                todo_status="all",
            )],
            [old.memory_id],
        )
        self.assertEqual(
            [memory.memory_id for memory in service.search(
                "负责人是甲",
                include_history=False,
                todo_status="all",
            )],
            [],
        )
        processed = json.loads(service.vault.processed_index_path.read_text(encoding="utf-8"))
        marker = processed["sessions"]["hermes/natural-owner-update"]
        self.assertEqual(marker["watermark"], 2)
        self.assertEqual(marker["processing"]["status"], "idle")

    def test_multiple_candidates_cannot_update_one_active_memory_in_one_batch(self):
        service = self.make_service("duplicate-update")
        old = service.create_memory(
            memory_id="mem-state",
            title="Project state",
            body="The project is in state A.",
            type="project",
        )
        backend = QueueBackend()
        first_user, _ = self.capture_turn(
            service,
            session="duplicate-update",
            turn="turn-1",
            user="项目状态从 A 进入 B，同时同步最新风险。",
            assistant="已确认状态更新为 B。",
        )
        second_user = event_key("duplicate-update/turn-1/assistant")
        backend.responses.extend(
            [
                gate(
                    [
                        candidate(
                            "state",
                            [first_user],
                            memory="项目状态为 B。",
                            type="project",
                            update_memory_id=old.memory_id,
                        ),
                        candidate(
                            "risk",
                            [second_user],
                            memory="项目存在最新风险。",
                            type="project",
                            update_memory_id=old.memory_id,
                        ),
                    ]
                ),
                summary(
                    first_user,
                    title="Project state B",
                    body="项目状态为 B。",
                    type="project",
                    update_memory_id=old.memory_id,
                ),
                summary(
                    second_user,
                    title="Project risk",
                    body="项目存在最新风险。",
                    type="project",
                    update_memory_id=old.memory_id,
                ),
            ]
        )

        with self.assertRaises(ModelOutputError):
            service.process(source="hermes", session_id="duplicate-update", model=backend)

        self.assertEqual(service.read(old.memory_id).body, old.body)
        self.assertEqual(service.vault.list_markdown("history"), [])
        processed = json.loads(service.vault.processed_index_path.read_text(encoding="utf-8"))
        self.assertEqual(processed["sessions"]["hermes/duplicate-update"].get("watermark", 0), 0)


if __name__ == "__main__":
    unittest.main()
