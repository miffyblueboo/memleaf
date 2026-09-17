from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from memleaf import Memleaf
from memleaf.inbox import parse_inbox
from memleaf.extraction_work_state import (
    complete_turn_budget,
    extraction_work_id,
    reserve_model_request,
)
from memleaf.turn_plan import turn_identity_key
from memleaf.process_journal import ProcessJournal
from memleaf.validation import parse_summarize_output


class G2SourceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.service = Memleaf.initialize(Path(self.temporary_directory.name) / "vault")

    def _capture(self, *, role: str, content: str, message_id: str, **values: object):
        return self.service.capture(
            "test-host",
            "session-1",
            "turn-1",
            role,
            content,
            event_id=f"receipt-{message_id}-{values.get('message_revision', '1')}",
            message_id=message_id,
            **values,
        )

    def test_source_time_is_distinct_from_capture_time(self) -> None:
        source_time = "2026-09-17T09:00:00+08:00"
        self._capture(
            role="user",
            content="明天交付",
            message_id="user-1",
            source_time=source_time,
            source_sequence=1,
        )
        event = parse_inbox(self.service.vault)[0].events[0]

        self.assertEqual(event.source_time, source_time)
        self.assertEqual(event.timestamp, source_time)
        self.assertIsNotNone(event.captured_at)
        self.assertNotEqual(event.captured_at, source_time)

    def test_missing_source_time_remains_unknown(self) -> None:
        self._capture(role="user", content="明天交付", message_id="user-1")
        event = parse_inbox(self.service.vault)[0].events[0]

        self.assertIsNone(event.source_time)
        self.assertIsNone(event.timestamp)
        self.assertIsNotNone(event.captured_at)

    def test_source_sequence_and_final_signal_define_complete_turn(self) -> None:
        self._capture(
            role="assistant",
            content="处理中",
            message_id="assistant-progress",
            source_sequence=3,
            final=False,
        )
        self._capture(
            role="user",
            content="第一条",
            message_id="user-1",
            source_sequence=1,
        )
        self._capture(
            role="user",
            content="改成第二条",
            message_id="user-2",
            source_sequence=2,
            previous_message_id="user-1",
        )
        self._capture(
            role="assistant",
            content="最终答复",
            message_id="assistant-final",
            source_sequence=4,
            final=True,
        )

        turn = parse_inbox(self.service.vault)[0]
        self.assertTrue(turn.complete)
        self.assertEqual([event.source_sequence for event in turn.events], [1, 2, 3, 4])
        self.assertEqual(turn.events[-1].content, "最终答复")

    def test_non_final_assistant_does_not_complete_new_contract_turn(self) -> None:
        self._capture(role="user", content="问题", message_id="user-1", source_sequence=1)
        self._capture(
            role="assistant",
            content="处理中",
            message_id="assistant-progress",
            source_sequence=2,
            final=False,
        )

        self.assertFalse(parse_inbox(self.service.vault)[0].complete)

    def test_duplicate_source_sequence_does_not_form_a_processable_turn(self) -> None:
        self._capture(role="user", content="问题", message_id="user-1", source_sequence=1)
        self._capture(
            role="assistant",
            content="答复",
            message_id="assistant-1",
            source_sequence=1,
            previous_message_id="user-1",
            final=True,
        )

        turn = parse_inbox(self.service.vault)[0]
        self.assertFalse(turn.processable)
        self.assertFalse(turn.complete)

    def test_message_revision_replaces_visible_text_and_fences_old_work(self) -> None:
        self._capture(
            role="user",
            content="旧事实",
            message_id="user-1",
            message_revision="r1",
            source_sequence=1,
        )
        self._capture(
            role="assistant",
            content="旧回复",
            message_id="assistant-1",
            message_revision="r1",
            source_sequence=2,
            previous_message_id="user-1",
            final=True,
        )
        turn = parse_inbox(self.service.vault)[0]
        state_path = self.service.vault.processed_state_path
        processed = json.loads(state_path.read_text(encoding="utf-8"))
        state = processed["sessions"]["test-host/session-1"]
        state["processed_turns"] = [
            {
                "turn_key": turn.turn_key,
                "turn_index": turn.turn_index,
                "event_keys": list(turn.event_keys),
                "memory_ids": ["mem-old"],
            }
        ]
        state["watermark"] = turn.turn_index
        state["processed_watermark"] = turn.turn_index
        state["processing"] = {
            "status": "processing",
            "token": "old-token",
            "turn_keys": [turn.turn_key],
        }
        plan_key = turn_identity_key("test-host", "session-1", turn.turn_key or "")
        processed["pending_turn_plans"] = {plan_key: {"stale": True}}
        processed["pending_operations"] = {
            "op-old": {
                "source": "test-host",
                "session_id": "session-1",
                "turn_key": turn.turn_key,
            }
        }
        state_path.write_text(json.dumps(processed), encoding="utf-8")

        self._capture(
            role="user",
            content="修订后的事实",
            message_id="user-1",
            message_revision="r2",
            previous_message_revision="r1",
            source_sequence=1,
        )

        pending_turn = parse_inbox(self.service.vault)[0]
        self.assertFalse(pending_turn.complete)
        self.assertEqual([event.content for event in pending_turn.events], ["修订后的事实"])
        self._capture(
            role="assistant",
            content="修订后的回复",
            message_id="assistant-1",
            message_revision="r2",
            previous_message_revision="r1",
            source_sequence=2,
            previous_message_id="user-1",
            final=True,
        )
        revised_turn = parse_inbox(self.service.vault)[0]
        self.assertEqual(revised_turn.events[0].content, "修订后的事实")
        self.assertEqual(revised_turn.events[1].content, "修订后的回复")
        self.assertTrue(revised_turn.complete)
        self.assertEqual(revised_turn.events[0].message_revision, "r2")
        current = json.loads(state_path.read_text(encoding="utf-8"))
        current_state = current["sessions"]["test-host/session-1"]
        self.assertEqual(current_state["processing"]["reason"], "source_revision_changed")
        self.assertEqual(current_state["revised_turns"][0]["memory_ids"], ["mem-old"])
        self.assertNotIn(plan_key, current.get("pending_turn_plans", {}))
        self.assertNotIn("op-old", current.get("pending_operations", {}))
        snapshots, _ = ProcessJournal(self.service)._snapshot(
            source="test-host",
            session_id="session-1",
            now="2026-09-17T10:00:00Z",
            cleanup_hours=24,
        )
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].turn.events[0].content, "修订后的事实")
        snapshot_state = ProcessJournal(self.service)._state_for_snapshot_unlocked(
            snapshots[0],
            json.loads(state_path.read_text(encoding="utf-8")),
        )
        self.assertEqual(snapshot_state["revised_turns"][0]["memory_ids"], ["mem-old"])

        with self.assertRaisesRegex(ValueError, "predecessor mismatch"):
            self._capture(
                role="user",
                content="迟到的旧版本",
                message_id="user-1",
                message_revision="r0",
                source_sequence=1,
            )

    def test_same_message_revision_is_idempotent(self) -> None:
        first = self._capture(
            role="user",
            content="事实",
            message_id="user-1",
            message_revision="r1",
        )
        second = self._capture(
            role="user",
            content="事实",
            message_id="user-1",
            message_revision="r1",
        )

        self.assertTrue(first.stored)
        self.assertTrue(second.duplicate)
        self.assertEqual(len(parse_inbox(self.service.vault)[0].events), 1)

        with self.assertRaisesRegex(ValueError, "different payload"):
            self._capture(
                role="user",
                content="同一 revision 的不同正文",
                message_id="user-1",
                message_revision="r1",
            )

    def test_source_time_requires_timezone(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            self._capture(
                role="user",
                content="事实",
                message_id="user-1",
                source_time="2026-09-17T09:00:00",
            )

    def test_work_identity_uses_source_revision_and_intent_not_transport(self) -> None:
        event = SimpleNamespace(
            event_key="event-key",
            message_id="message-1",
            message_revision="r1",
        )
        turn = SimpleNamespace(
            source="test-host",
            session_id="session-1",
            turn_key="turn-key",
            events=(event,),
        )

        first = extraction_work_id(turn, request_kind="automatic", intent_id="automatic")
        same = extraction_work_id(turn, request_kind="automatic", intent_id="automatic")
        explicit = extraction_work_id(
            turn,
            request_kind="explicit_remember",
            intent_id="remember-request-1",
        )
        revised_event = SimpleNamespace(
            event_key="event-key-r2",
            message_id="message-1",
            message_revision="r2",
        )
        revised_turn = SimpleNamespace(**{**vars(turn), "events": (revised_event,)})
        revised = extraction_work_id(
            revised_turn,
            request_kind="automatic",
            intent_id="automatic",
        )

        self.assertEqual(first, same)
        self.assertNotEqual(first, explicit)
        self.assertNotEqual(first, revised)

    def test_completed_budget_remains_terminal(self) -> None:
        work_id = "work-terminal"
        turn_id = "turn-terminal"
        self.assertEqual(
            reserve_model_request(self.service.vault, work_id=work_id, turn_id=turn_id),
            1,
        )
        self.assertTrue(
            complete_turn_budget(self.service.vault, work_id=work_id, turn_id=turn_id)
        )
        self.assertIsNone(
            reserve_model_request(self.service.vault, work_id=work_id, turn_id=turn_id)
        )
        state = json.loads(
            (self.service.vault.state_path / "extraction_request_budget.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(state["works"][work_id]["turns"][turn_id]["completed"])

    def test_explicit_budget_limit_survives_restart(self) -> None:
        work_id = "work-explicit"
        turn_id = "turn-explicit"
        self.assertEqual(
            reserve_model_request(
                self.service.vault,
                work_id=work_id,
                turn_id=turn_id,
                request_limit=2,
            ),
            1,
        )
        self.assertEqual(
            reserve_model_request(
                self.service.vault,
                work_id=work_id,
                turn_id=turn_id,
                request_limit=2,
            ),
            2,
        )
        self.assertIsNone(
            reserve_model_request(
                self.service.vault,
                work_id=work_id,
                turn_id=turn_id,
                request_limit=3,
            )
        )

    def test_legacy_consumption_above_current_limit_fails_closed(self) -> None:
        path = self.service.vault.state_path / "extraction_request_budget.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "works": {
                        "work-old": {
                            "turns": {
                                "turn-old": {
                                    "requests": 4,
                                    "started_at_epoch": None,
                                    "request_limit_at_creation": 5,
                                    "completed": False,
                                }
                            }
                        }
                    },
                    "order": ["work-old"],
                }
            ),
            encoding="utf-8",
        )

        self.assertIsNone(
            reserve_model_request(
                self.service.vault,
                work_id="work-old",
                turn_id="turn-old",
            )
        )

    def test_completed_status_does_not_invent_completion_time(self) -> None:
        event_key = "a" * 64
        parsed = parse_summarize_output(
            json.dumps(
                {
                    "title": "Completed task",
                    "body": "The task is complete.",
                    "tags": [],
                    "type": "todo",
                    "scopes": ["global"],
                    "sources": [event_key],
                    "status": "completed",
                }
            ),
            current_event_keys=[event_key],
        )

        self.assertEqual(parsed["status"], "completed")
        self.assertNotIn("completed_at", parsed)


if __name__ == "__main__":
    unittest.main()
