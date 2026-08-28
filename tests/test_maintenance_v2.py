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


def candidate(candidate_id, evidence, memory, *, scopes=None, scope_source="model", update_memory_id=None):
    value = {
        "candidate_id": candidate_id,
        "memory": memory,
        "evidence_event_ids": list(evidence),
        "duplicate": False,
        "worth": True,
        "type": "identity",
        "scopes": list(scopes or ["global"]),
        "scope_source": scope_source,
    }
    if update_memory_id is not None:
        value["update_memory_id"] = update_memory_id
    return value


def summary(event, body, *, title="项目负责人", scopes=None, scope_source="model", update_memory_id=None):
    value = {
        "title": title,
        "body": body,
        "tags": ["maintenance-v2"],
        "type": "identity",
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

    def test_summarizer_prompt_requires_stable_title_and_update_precedence(self):
        prompt = " ".join(SUMMARIZE_SYSTEM.split())
        self.assertIn("UPDATE or NO_CHANGE takes precedence over CREATE", prompt)
        self.assertIn("stable title made from the subject, topic, and only a necessary qualifier", prompt)
        self.assertIn("Make the body self-contained", prompt)

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
                            "某项目负责人是甲。",
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
