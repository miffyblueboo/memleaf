"""Focused v2 maintenance invariants: candidate lookup, batching, and deferral."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.index import event_key, turn_key
from memleaf.memory_writer import MemoryWriter
from memleaf.processing import (
    _RELATED_MAX_BODY_CHARS,
    _RELATED_MAX_CHARS,
    _RELATED_MAX_ITEMS,
    _SCOPE_DIRECTORY_MAX_CHARS,
    _SCOPE_DIRECTORY_MAX_ITEMS,
    Processor,
)
from memleaf.prompts import SUMMARIZE_SYSTEM


class QueueBackend:
    provider = "fake"
    model = "maintenance-v2-test"

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        del system, temperature
        self.calls.append({"prompt": prompt, "purpose": purpose})
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
    memory,
    *,
    scopes=None,
    scope_source="model",
    update_memory_id=None,
    type="identity",
):
    value = {
        "candidate_id": candidate_id,
        "memory": memory,
        "evidence_event_ids": list(evidence),
        "duplicate": False,
        "worth": True,
        "type": type,
        "scopes": list(scopes or ["global"]),
        "scope_source": scope_source,
    }
    if update_memory_id is not None:
        value["update_memory_id"] = update_memory_id
    return value


def summary(
    event,
    body,
    *,
    title="项目负责人",
    scopes=None,
    scope_source="model",
    update_memory_id=None,
    type="identity",
):
    value = {
        "title": title,
        "body": body,
        "tags": ["maintenance-v2"],
        "type": type,
        "scopes": list(scopes or ["global"]),
        "scope_source": scope_source,
        "sources": [{"event_key": event}],
    }
    if update_memory_id is not None:
        value["update_memory_id"] = update_memory_id
    return json.dumps(value, ensure_ascii=False)


class MaintenanceV2Tests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="memleaf-maintenance-v2-")
        self.service = Memleaf(Path(self.tempdir.name) / "vault")

    def tearDown(self):
        self.tempdir.cleanup()

    def capture(self, session, turn, user, assistant):
        user_event = f"{session}/{turn}/user"
        assistant_event = f"{session}/{turn}/assistant"
        self.service.capture("hermes", session, turn, "user", user, event_id=user_event)
        self.service.capture("hermes", session, turn, "assistant", assistant, event_id=assistant_event)
        return event_key(user_event), event_key(assistant_event)

    def processed(self):
        return json.loads(self.service.vault.processed_index_path.read_text(encoding="utf-8"))

    @staticmethod
    def related_payload(prompt):
        start_marker = "Relevant existing memleaf/native memories:\n"
        end_marker = "\nSession scope background:\n"
        start = prompt.index(start_marker) + len(start_marker)
        end = prompt.index(end_marker, start)
        return json.loads(prompt[start:end])

    def test_summarizer_prompt_requires_stable_title_and_update_precedence(self):
        prompt = " ".join(SUMMARIZE_SYSTEM.split())
        self.assertIn("UPDATE or NO_CHANGE takes precedence over CREATE", prompt)
        self.assertIn("stable title made from the subject, topic, and only a necessary qualifier", prompt)
        self.assertIn("Make the body self-contained", prompt)

    def test_scoped_elliptical_followup_reuses_existing_project_memory(self):
        first_user, first_assistant = self.capture(
            "project-lineage",
            "turn-1",
            "alpha 项目采用达梦数据库，Tomcat 改造为东方通，负责人是吴江波，38 个工作日完成。",
            "已确认 alpha 项目的技术路线、负责人和期限。",
        )
        backend = QueueBackend(
            [
                gate(
                    [
                        candidate(
                            "alpha-plan",
                            [first_user, first_assistant],
                            "alpha 项目的技术路线、负责人和期限。",
                            scopes=["project:alpha"],
                            type="project",
                        )
                    ]
                ),
                summary(
                    first_user,
                    "alpha 项目采用达梦数据库，Tomcat 改造为东方通；负责人吴江波，计划 2026-10-27 完成。",
                    title="alpha 项目技术路线与实施计划",
                    scopes=["project:alpha"],
                    type="project",
                ),
            ]
        )
        self.service.process(source="hermes", session_id="project-lineage", model=backend)
        old = self.service._read_memories_unlocked("knowledge")[0].memory
        self.assertEqual(
            self.processed()["sessions"]["hermes/project-lineage"]["scopes"],
            ["project:alpha"],
        )

        new_user, new_assistant = self.capture(
            "project-lineage",
            "turn-2",
            "这个项目的任务已同步到系统。",
            "任务同步完成，后续按项目继续跟进。",
        )
        backend.responses.extend(
            [
                gate(
                    [
                        candidate(
                            "alpha-sync",
                            [new_user, new_assistant],
                            "alpha 项目任务已同步到系统。",
                            scopes=["project:alpha"],
                            type="project",
                            update_memory_id=old.memory_id,
                        )
                    ]
                ),
                summary(
                    new_user,
                    "alpha 项目采用达梦数据库，Tomcat 改造为东方通；负责人吴江波，计划 2026-10-27 完成。任务已同步到系统。",
                    title="alpha 项目技术路线与实施计划",
                    scopes=["project:alpha"],
                    type="project",
                    update_memory_id=old.memory_id,
                ),
            ]
        )

        result = self.service.process(source="hermes", session_id="project-lineage", model=backend)

        self.assertEqual(result["memory_ids"], [old.memory_id])
        active = self.service._read_memories_unlocked("knowledge")
        self.assertEqual([record.memory.memory_id for record in active], [old.memory_id])
        self.assertIn("达梦数据库", active[0].memory.body)
        self.assertIn("东方通", active[0].memory.body)
        self.assertIn("吴江波", active[0].memory.body)
        self.assertIn("任务已同步到系统", active[0].memory.body)
        self.assertNotIn("内部", active[0].memory.body)
        history = self.service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertNotEqual(history[0].memory.memory_id, old.memory_id)
        self.assertEqual(history[0].memory.body, old.body)
        gate_prompts = [call["prompt"] for call in backend.calls if call["purpose"] == "gate"]
        self.assertEqual(len(gate_prompts), 2)
        self.assertIn(old.body, gate_prompts[1])

    def test_specific_scope_fallback_supplies_context_for_lexically_sparse_turn(self):
        first_user, first_assistant = self.capture(
            "project-lineage-sparse",
            "turn-1",
            "alpha 项目采用达梦数据库，负责人吴江波。",
            "已确认 alpha 项目的技术路线和负责人。",
        )
        backend = QueueBackend(
            [
                gate(
                    [
                        candidate(
                            "alpha-plan",
                            [first_user, first_assistant],
                            "alpha 项目的技术路线和负责人。",
                            scopes=["project:alpha"],
                            type="project",
                        )
                    ]
                ),
                summary(
                    first_user,
                    "alpha 项目采用达梦数据库；负责人吴江波。",
                    title="alpha 项目技术路线与负责人",
                    scopes=["project:alpha"],
                    type="project",
                ),
            ]
        )
        self.service.process(
            source="hermes", session_id="project-lineage-sparse", model=backend
        )
        old = self.service._read_memories_unlocked("knowledge")[0].memory
        self.capture(
            "project-lineage-sparse",
            "turn-2",
            "已同步。",
            "已完成。",
        )
        backend.responses.append(gate([]))

        result = self.service.process(
            source="hermes",
            session_id="project-lineage-sparse",
            model=backend,
        )

        self.assertEqual(result["memories_written"], 0)
        gate_prompts = [call["prompt"] for call in backend.calls if call["purpose"] == "gate"]
        self.assertEqual(len(gate_prompts), 2)
        self.assertIn(old.body, gate_prompts[1])

    def test_related_context_has_independent_budget_for_many_long_memories(self):
        tails = []
        for index in range(10):
            tail = f"UNIQUE-RELATED-TAIL-{index}"
            tails.append(tail)
            self.service.create_memory(
                memory_id=f"mem-related-{index}",
                title=f"budgeted project item {index}",
                body=f"body-{index}-" + "x" * 5000 + tail,
                type="project",
                scopes=["project:budgeted"],
            )
        self.capture(
            "related-budget",
            "turn-1",
            "budgeted 项目需要继续确认相关事项。",
            "已确认预算项目的后续安排。",
        )
        backend = QueueBackend([gate([])])

        result = self.service.process(
            source="hermes",
            session_id="related-budget",
            model=backend,
            scope=["project:budgeted"],
        )

        related = self.related_payload(backend.calls[0]["prompt"])
        encoded = json.dumps(related, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(result["memories_written"], 0)
        self.assertLessEqual(len(related), _RELATED_MAX_ITEMS)
        self.assertLessEqual(len(encoded), _RELATED_MAX_CHARS)
        self.assertTrue(all(len(item.get("body", "")) <= _RELATED_MAX_BODY_CHARS for item in related))
        self.assertTrue(all(tail not in encoded for tail in tails))

    def test_related_context_prioritizes_update_target_before_budget(self):
        target = self.service.create_memory(
            memory_id="mem-priority-target",
            title="priority project plan",
            body="TARGET-BODY " + "a" * 2200,
            type="project",
            scopes=["project:priority"],
        )
        self.service.create_memory(
            memory_id="mem-priority-decoy",
            title="priority project decoy",
            body="DECOY-BODY " + "b" * 2200,
            type="project",
            scopes=["project:priority"],
        )
        user_event, assistant_event = self.capture(
            "related-priority",
            "turn-1",
            "priority project status needs an update.",
            "Confirmed that the project entered the next stage.",
        )
        backend = QueueBackend(
            [
                gate(
                    [
                        candidate(
                            "priority-update",
                            [user_event, assistant_event],
                            "priority project 状态更新",
                            scopes=["project:priority"],
                            type="project",
                            update_memory_id=target.memory_id,
                        )
                    ]
                ),
                summary(
                    user_event,
                    "priority project 进入下一阶段。",
                    title="priority project plan",
                    scopes=["project:priority"],
                    type="project",
                    update_memory_id=target.memory_id,
                ),
            ]
        )

        result = self.service.process(
            source="hermes",
            session_id="related-priority",
            model=backend,
            scope=["project:priority"],
        )

        self.assertEqual(result["memory_ids"], [target.memory_id])
        summarize_prompt = next(
            call["prompt"] for call in backend.calls if call["purpose"] == "summarize"
        )
        related = self.related_payload(summarize_prompt)
        self.assertEqual(related[0]["memory_id"], target.memory_id)
        self.assertIn("TARGET-BODY", related[0]["body"])
        self.assertLessEqual(len(related[0]["body"]), _RELATED_MAX_BODY_CHARS)

    def test_summary_related_context_uses_same_budget_and_keeps_target(self):
        memory_ids = []
        tails = []
        for index in range(10):
            tail = f"SUMMARY-RELATED-TAIL-{index}"
            tails.append(tail)
            memory_ids.append(f"mem-summary-related-{index}")
            self.service.create_memory(
                memory_id=memory_ids[-1],
                title=f"summary budgeted project item {index}",
                body=f"summary-body-{index}-" + "y" * 5000 + tail,
                type="project",
                scopes=["project:summary-budget"],
            )
        # The target is visible in the bounded gate context, but not the first
        # result; the summarize call must promote it before applying its own
        # budget.
        target_id = memory_ids[7]
        user_event, assistant_event = self.capture(
            "summary-related-budget",
            "turn-1",
            "summary budgeted project plan needs an update.",
            "Confirmed the project entered the next stage.",
        )
        backend = QueueBackend(
            [
                gate(
                    [
                        candidate(
                            "summary-budget-update",
                            [user_event, assistant_event],
                            "summary-budget project plan update",
                            scopes=["project:summary-budget"],
                            type="project",
                            update_memory_id=target_id,
                        )
                    ]
                ),
                summary(
                    user_event,
                    "summary budgeted project entered the next stage.",
                    title="summary budgeted project item 0",
                    scopes=["project:summary-budget"],
                    type="project",
                    update_memory_id=target_id,
                ),
            ]
        )

        result = self.service.process(
            source="hermes",
            session_id="summary-related-budget",
            model=backend,
            scope=["project:summary-budget"],
        )

        self.assertEqual(result["memory_ids"], [target_id])
        summarize_prompt = next(
            call["prompt"] for call in backend.calls if call["purpose"] == "summarize"
        )
        related = self.related_payload(summarize_prompt)
        encoded = json.dumps(related, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(related), _RELATED_MAX_ITEMS)
        self.assertLessEqual(len(encoded), _RELATED_MAX_CHARS)
        self.assertEqual(related[0]["memory_id"], target_id)
        self.assertTrue(all(len(item.get("body", "")) <= _RELATED_MAX_BODY_CHARS for item in related))
        self.assertTrue(all(tail not in encoded for tail in tails))

    def test_ambiguous_sparse_scope_fallback_defers_without_full_scope_dump(self):
        bodies = []
        for index in range(2):
            body = f"AMBIGUOUS-BODY-{index} " + "z" * 2000
            bodies.append(body)
            self.service.create_memory(
                memory_id=f"mem-ambiguous-{index}",
                title=f"ambiguous project topic {index}",
                body=body,
                type="project",
                scopes=["project:ambiguous"],
            )
        user_event, assistant_event = self.capture(
            "related-ambiguous",
            "turn-1",
            "已同步。",
            "已完成。",
        )
        backend = QueueBackend(
            [
                gate(
                    [
                        candidate(
                            "ambiguous-update",
                            [user_event, assistant_event],
                            "ambiguous project task has been synced",
                            scopes=["project:ambiguous"],
                            type="project",
                        )
                    ]
                )
            ]
        )

        result = self.service.process(
            source="hermes",
            session_id="related-ambiguous",
            model=backend,
            scope=["project:ambiguous"],
        )

        related = self.related_payload(backend.calls[0]["prompt"])
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["deferred_candidates"], 1)
        self.assertEqual(related, [])
        self.assertTrue(all(body not in backend.calls[0]["prompt"] for body in bodies))
        self.assertEqual(len(self.service._read_memories_unlocked("knowledge")), 2)
        self.assertEqual(self.service._read_memories_unlocked("history"), [])

    def test_sparse_inherited_scope_uses_metadata_directory_then_selected_body(self):
        target_body = "技术路线：达梦数据库与东方通；负责人吴江波；期限为2026-10-27；约束是按既定信创方案推进。"
        contact_body = "CONTACT-ONLY-BODY"
        email_body = "EMAIL-ONLY-BODY"
        target = self.service.create_memory(
            memory_id="mem-orion-plan",
            title="信创改造实施计划",
            body=target_body,
            type="project",
            scopes=["project:orion"],
        )
        self.service.create_memory(
            memory_id="mem-orion-contact",
            title="项目联系人",
            body=contact_body,
            type="fact",
            scopes=["project:orion"],
        )
        self.service.create_memory(
            memory_id="mem-orion-email",
            title="项目邮件",
            body=email_body,
            type="fact",
            scopes=["project:orion"],
        )

        # Establish the inherited scope without adding a memory.  The next
        # sparse turn must resolve it from the directory rather than a body
        # dump or a recency guess.
        context_user, context_assistant = self.capture(
            "orion-directory",
            "turn-1",
            "准备继续处理这个项目。",
            "已确认当前工作范围。",
        )
        context_gate = gate(
            [
                {
                    "candidate_id": "scope-context",
                    "memory": "项目工作范围",
                    "evidence_event_ids": [context_user, context_assistant],
                    "duplicate": True,
                    "worth": False,
                    "type": "project",
                    "scopes": ["project:orion"],
                    "scope_source": "model",
                    "duplicate_memory_id": target.memory_id,
                }
            ]
        )
        self.service.process(
            source="hermes",
            session_id="orion-directory",
            scope="project:orion",
            model=QueueBackend([context_gate]),
        )

        user_event, assistant_event = self.capture(
            "orion-directory",
            "turn-2",
            "这个项目任务已同步到 Orion。",
            "已完成。",
        )
        backend = QueueBackend(
            [
                gate(
                    [
                        candidate(
                            "orion-sync",
                            [user_event, assistant_event],
                            "Orion 项目任务已同步。",
                            scopes=["project:orion"],
                            type="project",
                            update_memory_id=target.memory_id,
                        )
                    ]
                ),
                summary(
                    user_event,
                    target_body + " 当前进展：项目任务已同步到 Orion。",
                    title="信创改造实施计划",
                    scopes=["project:orion"],
                    type="project",
                    update_memory_id=target.memory_id,
                ),
            ]
        )

        with patch.object(
            self.service,
            "_search_unlocked",
            wraps=self.service._search_unlocked,
        ) as search, patch.object(
            Processor,
            "_scope_records_unlocked",
            autospec=True,
            side_effect=Processor._scope_records_unlocked,
        ) as scope_scan:
            result = self.service.process(
                source="hermes",
                session_id="orion-directory",
                model=backend,
            )

        self.assertEqual(result["memory_ids"], [target.memory_id])
        self.assertEqual(search.call_count, 1)
        self.assertEqual(scope_scan.call_count, 1)
        active = self.service._read_memories_unlocked("knowledge")
        self.assertEqual(
            {record.memory.memory_id for record in active},
            {target.memory_id, "mem-orion-contact", "mem-orion-email"},
        )
        updated = next(record.memory for record in active if record.memory.memory_id == target.memory_id)
        self.assertIn("达梦数据库", updated.body)
        self.assertIn("东方通", updated.body)
        self.assertIn("项目任务已同步到 Orion", updated.body)
        self.assertEqual(len(self.service._read_memories_unlocked("history")), 1)

        gate_call = next(call for call in backend.calls if call["purpose"] == "gate")
        gate_prompt_text = gate_call["prompt"]
        directory_marker = "Bounded scope candidate directory (metadata only; not evidence):\n"
        self.assertIn(directory_marker, gate_prompt_text)
        directory_start = gate_prompt_text.index(directory_marker) + len(directory_marker)
        directory_end = gate_prompt_text.index(
            "\nMinimal valid JSON example", directory_start
        )
        directory = json.loads(gate_prompt_text[directory_start:directory_end])
        self.assertLessEqual(len(directory), _SCOPE_DIRECTORY_MAX_ITEMS)
        self.assertLessEqual(
            len(json.dumps(directory, ensure_ascii=False, separators=(",", ":"))),
            _SCOPE_DIRECTORY_MAX_CHARS,
        )
        self.assertEqual(
            {entry["memory_id"] for entry in directory},
            {target.memory_id, "mem-orion-contact", "mem-orion-email"},
        )
        self.assertTrue(
            all(set(entry) == {"memory_id", "title", "type", "scopes"} for entry in directory)
        )
        self.assertNotIn(target_body, gate_prompt_text)
        self.assertNotIn(contact_body, gate_prompt_text)
        self.assertNotIn(email_body, gate_prompt_text)

        summarize_call = next(call for call in backend.calls if call["purpose"] == "summarize")
        summarize_related = self.related_payload(summarize_call["prompt"])
        self.assertEqual([item["memory_id"] for item in summarize_related], [target.memory_id])
        self.assertIn(target_body, summarize_call["prompt"])
        self.assertNotIn(contact_body, summarize_call["prompt"])
        self.assertNotIn(email_body, summarize_call["prompt"])

    def test_same_process_state_change_uses_first_turn_overlay(self):
        first_user, first_assistant = self.capture(
            "same-process",
            "turn-1",
            "项目负责人是甲。",
            "已确认负责人为甲。",
        )
        second_user, second_assistant = self.capture(
            "same-process",
            "turn-2",
            "项目负责人更新为乙。",
            "已确认今后以乙为准。",
        )
        first_id = MemoryWriter.deterministic_memory_id(
            source="hermes",
            session_id="same-process",
            turn_key=turn_key("turn-1"),
            candidate_id="owner-a",
            evidence_event_ids=[first_user, first_assistant],
        )
        backend = QueueBackend(
            [
                gate([candidate("owner-a", [first_user, first_assistant], "项目负责人是甲。")]),
                summary(first_user, "项目负责人是甲。"),
                gate(
                    [
                        candidate(
                            "owner-b",
                            [second_user, second_assistant],
                            "项目负责人更新为乙。",
                            update_memory_id=first_id,
                        )
                    ]
                ),
                summary(
                    second_user,
                    "项目负责人已更新为乙。",
                    update_memory_id=first_id,
                ),
            ]
        )

        result = self.service.process(source="hermes", session_id="same-process", model=backend)

        self.assertEqual(result["processed_turns"], 2)
        self.assertEqual(result["memories_written"], 2)
        active = self.service._read_memories_unlocked("knowledge")
        history = self.service._read_memories_unlocked("history")
        self.assertEqual([record.memory.memory_id for record in active], [first_id])
        self.assertEqual(active[0].memory.body, "项目负责人已更新为乙。")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.body, "项目负责人是甲。")
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize", "gate", "summarize"])

    def test_same_process_three_state_updates_apply_in_order(self):
        first_user, first_assistant = self.capture(
            "three-state",
            "turn-1",
            "项目负责人是甲。",
            "已确认负责人为甲。",
        )
        second_user, second_assistant = self.capture(
            "three-state",
            "turn-2",
            "项目负责人更新为乙。",
            "已确认今后以乙为准。",
        )
        third_user, third_assistant = self.capture(
            "three-state",
            "turn-3",
            "项目负责人再次更新为丙。",
            "已确认今后以丙为准。",
        )
        first_id = MemoryWriter.deterministic_memory_id(
            source="hermes",
            session_id="three-state",
            turn_key=turn_key("turn-1"),
            candidate_id="owner-a",
            evidence_event_ids=[first_user, first_assistant],
        )
        backend = QueueBackend(
            [
                gate([candidate("owner-a", [first_user, first_assistant], "项目负责人是甲。")]),
                summary(first_user, "项目负责人是甲。"),
                gate(
                    [
                        candidate(
                            "owner-b",
                            [second_user, second_assistant],
                            "项目负责人更新为乙。",
                            update_memory_id=first_id,
                        )
                    ]
                ),
                summary(
                    second_user,
                    "项目负责人已更新为乙。",
                    update_memory_id=first_id,
                ),
                gate(
                    [
                        candidate(
                            "owner-c",
                            [third_user, third_assistant],
                            "项目负责人再次更新为丙。",
                            update_memory_id=first_id,
                        )
                    ]
                ),
                summary(
                    third_user,
                    "项目负责人已更新为丙。",
                    update_memory_id=first_id,
                ),
            ]
        )

        result = self.service.process(source="hermes", session_id="three-state", model=backend)

        self.assertEqual(result["processed_turns"], 3)
        self.assertEqual(result["memories_written"], 3)
        active = self.service._read_memories_unlocked("knowledge")
        history = self.service._read_memories_unlocked("history")
        self.assertEqual([record.memory.memory_id for record in active], [first_id])
        self.assertEqual(active[0].memory.body, "项目负责人已更新为丙。")
        self.assertEqual({record.memory.body for record in history}, {"项目负责人是甲。", "项目负责人已更新为乙。"})

    def test_same_process_state_update_followed_by_query_does_not_add_snapshot(self):
        first_user, first_assistant = self.capture(
            "state-then-query",
            "turn-1",
            "项目负责人是甲。",
            "已确认负责人为甲。",
        )
        second_user, second_assistant = self.capture(
            "state-then-query",
            "turn-2",
            "项目负责人更新为乙。",
            "已确认今后以乙为准。",
        )
        query_user, query_assistant = self.capture(
            "state-then-query",
            "turn-3",
            "请问当前项目负责人是谁？",
            "当前负责人是乙。",
        )
        first_id = MemoryWriter.deterministic_memory_id(
            source="hermes",
            session_id="state-then-query",
            turn_key=turn_key("turn-1"),
            candidate_id="owner-a",
            evidence_event_ids=[first_user, first_assistant],
        )
        backend = QueueBackend(
            [
                gate([candidate("owner-a", [first_user, first_assistant], "项目负责人是甲。")]),
                summary(first_user, "项目负责人是甲。"),
                gate(
                    [
                        candidate(
                            "owner-b",
                            [second_user, second_assistant],
                            "项目负责人更新为乙。",
                            update_memory_id=first_id,
                        )
                    ]
                ),
                summary(
                    second_user,
                    "项目负责人已更新为乙。",
                    update_memory_id=first_id,
                ),
                gate([]),
            ]
        )

        result = self.service.process(source="hermes", session_id="state-then-query", model=backend)

        self.assertEqual(result["processed_turns"], 3)
        self.assertEqual(result["memories_written"], 2)
        active = self.service._read_memories_unlocked("knowledge")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].memory.body, "项目负责人已更新为乙。")
        self.assertEqual(len(self.service._read_memories_unlocked("history")), 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize", "gate", "summarize", "gate"])

    def test_batch_state_updates_recover_forward_after_processed_write_failure(self):
        first_user, first_assistant = self.capture(
            "forward-recovery",
            "turn-1",
            "项目负责人是甲。",
            "已确认负责人为甲。",
        )
        second_user, second_assistant = self.capture(
            "forward-recovery",
            "turn-2",
            "项目负责人更新为乙。",
            "已确认今后以乙为准。",
        )
        first_id = MemoryWriter.deterministic_memory_id(
            source="hermes",
            session_id="forward-recovery",
            turn_key=turn_key("turn-1"),
            candidate_id="owner-a",
            evidence_event_ids=[first_user, first_assistant],
        )
        responses = [
            gate([candidate("owner-a", [first_user, first_assistant], "项目负责人是甲。")]),
            summary(first_user, "项目负责人是甲。"),
            gate(
                [
                    candidate(
                        "owner-b",
                        [second_user, second_assistant],
                        "项目负责人更新为乙。",
                        update_memory_id=first_id,
                    )
                ]
            ),
            summary(
                second_user,
                "项目负责人已更新为乙。",
                update_memory_id=first_id,
            ),
        ]
        backend = QueueBackend(responses)
        import memleaf.processing as processing_module

        original_atomic = processing_module.atomic_write_json
        calls = {"processed": 0}

        def fail_final_processed(path, value):
            if path == self.service.vault.processed_index_path:
                calls["processed"] += 1
                if calls["processed"] == 2:
                    raise OSError("injected final processed write failure")
            return original_atomic(path, value)

        with patch.object(processing_module, "atomic_write_json", side_effect=fail_final_processed):
            with self.assertRaises(OSError):
                self.service.process(
                    source="hermes", session_id="forward-recovery", model=backend
                )

        self.assertEqual(self.service._read_memories_unlocked("knowledge")[0].memory.body, "项目负责人已更新为乙。")
        self.assertEqual(len(self.service._read_memories_unlocked("history")), 1)
        failed = self.processed()["sessions"]["hermes/forward-recovery"]
        self.assertNotIn("watermark", failed)
        self.assertEqual(failed["processing"]["status"], "failed")

        backend.responses.extend(responses)
        result = self.service.process(
            source="hermes", session_id="forward-recovery", model=backend
        )

        self.assertEqual(result["processed_turns"], 2)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(self.service._read_memories_unlocked("knowledge")[0].memory.body, "项目负责人已更新为乙。")
        self.assertEqual(len(self.service._read_memories_unlocked("history")), 1)
        recovered = self.processed()["sessions"]["hermes/forward-recovery"]
        self.assertEqual(recovered["watermark"], 2)
        self.assertEqual(recovered["processing"]["status"], "idle")

    def test_automatic_unscoped_candidate_is_deferred_and_can_retry_with_scope(self):
        user_event, assistant_event = self.capture(
            "deferred",
            "turn-1",
            "某项目负责人是甲。",
            "已确认负责人为甲。",
        )
        backend = QueueBackend(
            [
                gate(
                    [
                        candidate(
                            "owner",
                            [user_event, assistant_event],
                            "某项目负责人是甲。",
                            scopes=["unscoped"],
                            scope_source="insufficient_context",
                        )
                    ]
                )
            ]
        )

        result = self.service.process(source="hermes", session_id="deferred", model=backend)

        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["deferred_candidates"], 1)
        self.assertEqual(result["deferred_inbox_turns"], 1)
        self.assertEqual(self.service._read_memories_unlocked("knowledge"), [])
        state = self.processed()["sessions"]["hermes/deferred"]
        entry = state["processed_turns"][0]
        self.assertEqual(entry["deferred_candidates"][0]["reason"], "scope_required")
        self.assertIsNone(entry["eligible_cleanup_at"])
        self.assertTrue((self.service.vault.inbox_path / "hermes" / "deferred.md").exists())

        backend.responses.extend(
            [
                gate(
                    [
                        candidate(
                            "owner",
                            [user_event, assistant_event],
                            "alpha 项目负责人是甲。",
                            scopes=["project:alpha"],
                        )
                    ]
                ),
                summary(
                    user_event,
                    "project:alpha 项目负责人是甲。",
                    scopes=["project:alpha"],
                ),
            ]
        )
        retry = self.service.process(
            source="hermes",
            session_id="deferred",
            scope="project:alpha",
            model=backend,
        )

        self.assertEqual(retry["memories_written"], 1)
        self.assertEqual(len(self.service._read_memories_unlocked("knowledge")), 1)
        retry_entry = self.processed()["sessions"]["hermes/deferred"]["processed_turns"][0]
        self.assertNotIn("deferred_candidates", retry_entry)

    def test_deferred_counts_are_limited_to_requested_session(self):
        first_user, first_assistant = self.capture(
            "deferred-first",
            "turn-1",
            "某项目负责人是甲。",
            "已确认负责人为甲。",
        )
        second_user, second_assistant = self.capture(
            "deferred-second",
            "turn-1",
            "另一个项目负责人是乙。",
            "已确认负责人为乙。",
        )
        deferred_gate = lambda user, assistant, marker: gate(
            [
                candidate(
                    marker,
                    [user, assistant],
                    "项目负责人信息",
                    scopes=["unscoped"],
                    scope_source="insufficient_context",
                )
            ]
        )
        backend = QueueBackend(
            [
                deferred_gate(first_user, first_assistant, "first-owner"),
                deferred_gate(second_user, second_assistant, "second-owner"),
            ]
        )

        first_result = self.service.process(
            source="hermes", session_id="deferred-first", model=backend
        )
        second_result = self.service.process(
            source="hermes", session_id="deferred-second", model=backend
        )

        self.assertEqual(first_result["deferred_inbox_turns"], 1)
        self.assertEqual(second_result["deferred_inbox_turns"], 1)
        first_view = self.service.process(
            source="hermes", session_id="deferred-first", model=backend
        )
        all_view = self.service.process(source="hermes", model=backend)
        self.assertEqual(first_view["deferred_candidates"], 1)
        self.assertEqual(first_view["deferred_inbox_turns"], 1)
        self.assertEqual(all_view["deferred_candidates"], 2)
        self.assertEqual(all_view["deferred_inbox_turns"], 2)

    def test_candidate_update_target_must_match_candidate_scope_lookup(self):
        self.service.create_memory(
            memory_id="mem-alpha-owner",
            title="项目负责人",
            body="alpha 项目负责人是甲。",
            type="identity",
            scopes=["project:alpha"],
        )
        self.service.create_memory(
            memory_id="mem-beta-owner",
            title="项目负责人",
            body="beta 项目负责人是丙。",
            type="identity",
            scopes=["project:beta"],
        )
        user_event, assistant_event = self.capture(
            "isolation",
            "turn-1",
            "alpha 项目负责人更新为乙。",
            "已确认 alpha 负责人为乙；beta 项目负责人仍为丙。",
        )
        backend = QueueBackend(
            [
                gate(
                    [
                        candidate(
                            "wrong-target",
                            [user_event, assistant_event],
                            "alpha 项目负责人更新为乙。",
                            scopes=["project:alpha"],
                            update_memory_id="mem-beta-owner",
                        )
                    ]
                )
            ]
        )

        with self.assertRaises(ValueError):
            self.service.process(source="hermes", session_id="isolation", model=backend)

        self.assertEqual(self.service.read("mem-alpha-owner").body, "alpha 项目负责人是甲。")
        self.assertEqual(self.service.read("mem-beta-owner").body, "beta 项目负责人是丙。")
        self.assertEqual(self.service.vault.list_markdown("history"), [])

    def test_same_batch_projects_keep_planned_overlay_in_their_scope(self):
        alpha = self.service.create_memory(
            memory_id="mem-alpha-owner",
            title="alpha 项目负责人",
            body="alpha 项目负责人是甲。",
            type="identity",
            scopes=["project:alpha"],
        )
        beta = self.service.create_memory(
            memory_id="mem-beta-owner",
            title="beta 项目负责人",
            body="beta 项目负责人是丙。",
            type="identity",
            scopes=["project:beta"],
        )
        alpha_user, alpha_assistant = self.capture(
            "same-batch-projects",
            "turn-1",
            "alpha 项目负责人更新为乙。",
            "已确认 alpha 负责人为乙。",
        )
        beta_user, beta_assistant = self.capture(
            "same-batch-projects",
            "turn-2",
            "beta 项目负责人更新为丁。",
            "已确认 beta 负责人为丁。",
        )
        backend = QueueBackend(
            [
                gate(
                    [
                        candidate(
                            "alpha-update",
                            [alpha_user, alpha_assistant],
                            "alpha 项目负责人更新为乙。",
                            scopes=["project:alpha"],
                            update_memory_id=alpha.memory_id,
                        )
                    ]
                ),
                summary(
                    alpha_user,
                    "alpha 项目负责人已更新为乙。",
                    scopes=["project:alpha"],
                    update_memory_id=alpha.memory_id,
                ),
                gate(
                    [
                        candidate(
                            "beta-update",
                            [beta_user, beta_assistant],
                            "beta 项目负责人更新为丁。",
                            scopes=["project:beta"],
                            update_memory_id=beta.memory_id,
                        )
                    ]
                ),
                summary(
                    beta_user,
                    "beta 项目负责人已更新为丁。",
                    scopes=["project:beta"],
                    update_memory_id=beta.memory_id,
                ),
            ]
        )

        result = self.service.process(source="hermes", session_id="same-batch-projects", model=backend)

        self.assertEqual(result["memories_written"], 2)
        self.assertEqual(self.service.read(alpha.memory_id).body, "alpha 项目负责人已更新为乙。")
        self.assertEqual(self.service.read(beta.memory_id).body, "beta 项目负责人已更新为丁。")
        self.assertEqual(len(self.service._read_memories_unlocked("history")), 2)
        summary_prompts = [call["prompt"] for call in backend.calls if call["purpose"] == "summarize"]
        self.assertEqual(len(summary_prompts), 2)
        self.assertNotIn(alpha.memory_id, summary_prompts[1])


if __name__ == "__main__":
    unittest.main()
