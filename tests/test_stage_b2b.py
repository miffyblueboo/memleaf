import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.index import event_key
from memleaf.llm import ModelError
from memleaf.memory_writer import MemoryWriter
from memleaf.processing import ProcessingError
from memleaf.validation import ModelOutputError


from tests.semantic_fixtures import semantic_fixture

@semantic_fixture
class QueueBackend:
    provider = "fake"
    model = "b2b-test"

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        self.calls.append({"prompt": prompt, "purpose": purpose})
        if not self.responses:
            raise ModelError("fake queue exhausted")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeClock:
    def __init__(self):
        self.value = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, *, hours=0, minutes=0):
        self.value += timedelta(hours=hours, minutes=minutes)


class StageB2BTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "vault"
        self.clock = FakeClock()

    def tearDown(self):
        self.tempdir.cleanup()

    def make_service(self, backend=None, name="vault"):
        path = self.root if name == "vault" else Path(self.tempdir.name) / name
        return Memleaf(path, model=backend, clock=self.clock)

    @staticmethod
    def gate(candidates):
        return json.dumps({"candidates": candidates})

    @staticmethod
    def candidate(candidate_id, evidence, *, memory="confirmed fact", type="fact", update=False, target=None):
        value = {
            "candidate_id": candidate_id,
            "memory": memory,
            "evidence_event_ids": list(evidence),
            "duplicate": False,
            "worth": True,
            "type": type,
            "scopes": ["global"],
            "scope_source": "model",
        }
        if update:
            value["memory"] = f"update {memory}"
        return value

    @staticmethod
    def summary(
        event_key_value,
        *,
        title="Fact",
        body="confirmed fact",
        type="fact",
        **extra,
    ):
        value = {
            "title": title,
            "body": body,
            "tags": ["b2b"],
            "type": type,
            "scopes": ["global"],
            "scope_source": "model",
            "sources": [
                {
                    "event_key": event_key_value,
                    "session_id": "forged-session",
                    "turn_id": "forged-turn",
                    "conversation_title": "FORGED-TITLE",
                }
            ],
        }
        value.update(extra)
        return json.dumps(value)

    def capture_turn(self, service, *, turn, user_event, assistant_event, user="user", assistant="assistant"):
        service.capture("codex", "s", turn, "user", user, event_id=user_event)
        service.capture("codex", "s", turn, "assistant", assistant, event_id=assistant_event)
        return event_key(user_event), event_key(assistant_event)

    def processed(self, service):
        return json.loads(service.vault.processed_index_path.read_text(encoding="utf-8"))

    def active(self, service):
        records = service._read_memories_unlocked("knowledge")
        self.assertEqual(len(records), 1)
        return records[0].memory

    def process_create(self, service, backend, *, turn="t1", user_event="u1", assistant_event="a1", title="Original", body="old body", type="fact", extra=None):
        user_key, assistant_key = self.capture_turn(
            service,
            turn=turn,
            user_event=user_event,
            assistant_event=assistant_event,
            user=f"user {turn}",
            assistant=f"assistant {turn}",
        )
        candidate = self.candidate("candidate-" + turn, [user_key], type=type)
        summary_extra = dict(extra or {})
        backend.responses.extend(
            [
                self.gate([candidate]),
                self.summary(user_key, title=title, body=body, type=type, **summary_extra),
            ]
        )
        result = service.process()
        return service.read(result["memory_ids"][0]), user_key, assistant_key

    def update_once(self, service, backend, active_id, *, turn, user_event, assistant_event, title, body, type="fact", extra=None):
        active = service.read(active_id)
        related_context = ""
        if active is not None:
            related_context = f" {active.title} {active.body}"
        user_key, assistant_key = self.capture_turn(
            service,
            turn=turn,
            user_event=user_event,
            assistant_event=assistant_event,
            user=f"user {turn} b2b{related_context}",
            assistant=f"assistant {turn}",
        )
        candidate = self.candidate(
            "candidate-" + turn,
            [user_key],
            memory=f"{active.title} {active.body}",
            type=type,
        )
        summary_extra = dict(extra or {})
        summary_extra["update_memory_id"] = active_id
        backend.responses.extend(
            [
                self.gate([candidate]),
                self.summary(user_key, title=title, body=body, type=type, **summary_extra),
            ]
        )
        self.clock.advance(hours=1)
        service.process()
        return self.active(service), user_key, assistant_key

    def test_update_archives_old_active_and_appends_core_sources(self):
        backend = QueueBackend()
        service = self.make_service(backend)
        active, old_user_key, old_assistant_key = self.process_create(service, backend, title="Original", body="old body")
        active.hit_count = 7
        active.last_hit_at = "2026-08-23T12:00:00Z"
        service.write_memory(active)
        created = active.created
        updated_before = active.updated

        current, new_user_key, new_assistant_key = self.update_once(
            service,
            backend,
            active.memory_id,
            turn="t2",
            user_event="u2",
            assistant_event="a2",
            title="Updated",
            body="new body",
        )

        history = service._read_memories_unlocked("history")
        self.assertEqual([record.memory.memory_id for record in service._read_memories_unlocked("knowledge")], [active.memory_id])
        self.assertEqual(len(history), 1)
        archived = history[0].memory
        self.assertNotEqual(archived.memory_id, current.memory_id)
        self.assertEqual(archived.extra["active_memory_id"], current.memory_id)
        self.assertEqual(archived.extra["superseded_by"], current.memory_id)
        self.assertIn("archived_at", archived.extra)
        self.assertEqual(archived.body, "old body")
        self.assertEqual(current.created, created)
        self.assertGreater(current.updated, updated_before)
        self.assertEqual(current.hit_count, 7)
        self.assertEqual(current.last_hit_at, "2026-08-23T12:00:00Z")
        self.assertEqual(
            {item["event_key"] for item in current.sources},
            {old_user_key, old_assistant_key, new_user_key, new_assistant_key},
        )
        self.assertTrue(all(item["session_id"] == "s" for item in current.sources))
        self.assertTrue(all(item["conversation_title"] != "FORGED-TITLE" for item in current.sources))

    def test_two_updates_keep_two_searchable_history_versions(self):
        backend = QueueBackend()
        service = self.make_service(backend)
        active, _, _ = self.process_create(service, backend, title="State one", body="state one")
        self.update_once(
            service,
            backend,
            active.memory_id,
            turn="t2",
            user_event="u2",
            assistant_event="a2",
            title="State two",
            body="state two",
        )
        self.update_once(
            service,
            backend,
            active.memory_id,
            turn="t3",
            user_event="u3",
            assistant_event="a3",
            title="State three",
            body="state three",
        )

        history = [record.memory for record in service._read_memories_unlocked("history")]
        self.assertEqual(len(history), 2)
        self.assertEqual(len({memory.memory_id for memory in history}), 2)
        self.assertTrue(all(memory.extra["active_memory_id"] == active.memory_id for memory in history))
        found = service.search("state", include_history=True, todo_status="all")
        found_ids = {memory.memory_id for memory in found}
        self.assertTrue({memory.memory_id for memory in history}.issubset(found_ids))

    def test_todo_status_transitions_clear_completed_at(self):
        backend = QueueBackend()
        service = self.make_service(backend)
        active, _, _ = self.process_create(
            service,
            backend,
            title="Todo",
            body="do it",
            type="todo",
        )
        self.assertEqual(active.status, "active")
        completed_at = "2026-08-24T02:00:00Z"
        completed, _, _ = self.update_once(
            service,
            backend,
            active.memory_id,
            turn="t2",
            user_event="u2",
            assistant_event="a2",
            title="Todo done",
            body="done",
            type="todo",
            extra={"status": "completed", "completed_at": completed_at},
        )
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.completed_at, completed_at)
        cancelled, _, _ = self.update_once(
            service,
            backend,
            active.memory_id,
            turn="t3",
            user_event="u3",
            assistant_event="a3",
            title="Todo cancelled",
            body="cancelled",
            type="todo",
            extra={"status": "cancelled"},
        )
        self.assertEqual(cancelled.status, "cancelled")
        self.assertIsNone(cancelled.completed_at)
        self.assertEqual(cancelled.memory_id, active.memory_id)

    def test_forget_active_or_topic_removes_all_linked_history_without_new_history(self):
        backend = QueueBackend()
        service = self.make_service(backend)
        active, _, _ = self.process_create(service, backend, title="Forget current", body="old")
        self.update_once(
            service,
            backend,
            active.memory_id,
            turn="t2",
            user_event="u2",
            assistant_event="a2",
            title="Forget current",
            body="new",
        )
        self.assertEqual(len(service.vault.list_markdown("history")), 1)
        result = service.forget_about("Forget current")
        self.assertEqual(result.status, "deleted")
        self.assertEqual(len(service.vault.list_markdown("knowledge")), 0)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertEqual(result.deleted.count(active.memory_id), 1)

    def test_forget_history_id_only_deletes_that_history_file(self):
        backend = QueueBackend()
        service = self.make_service(backend)
        first, _, _ = self.process_create(service, backend, title="First", body="first")
        self.update_once(
            service,
            backend,
            first.memory_id,
            turn="t2",
            user_event="u2",
            assistant_event="a2",
            title="First current",
            body="first current",
        )
        second, _, _ = self.process_create(service, backend, turn="t3", user_event="u3", assistant_event="a3", title="Second", body="second")
        history_id = service._read_memories_unlocked("history")[0].memory.memory_id

        self.assertTrue(service.forget_memory(history_id))
        self.assertIsNotNone(service.read(first.memory_id))
        self.assertIsNotNone(service.read(second.memory_id))
        self.assertEqual(len(service.vault.list_markdown("history")), 0)

    def test_history_then_active_failure_retries_after_clock_change_without_collision(self):
        backend = QueueBackend()
        service = self.make_service(backend)
        active, _, _ = self.process_create(service, backend, title="Original", body="old")
        user_key, _ = self.capture_turn(
            service,
            turn="t2",
            user_event="u2",
            assistant_event="a2",
            user="user t2 old",
        )
        candidate = self.candidate(
            "candidate-t2", [user_key], memory=f"{active.title} {active.body}"
        )
        responses = [
            self.gate([candidate]),
            self.summary(user_key, title="Updated", body="new", update_memory_id=active.memory_id),
        ]
        backend.responses.extend(responses)
        import memleaf.memory_writer as writer_module

        original_write = writer_module.atomic_write_text

        def fail_active(path, text):
            if path.parent.name == "knowledge":
                raise OSError("active write failed")
            return original_write(path, text)

        with patch.object(writer_module, "atomic_write_text", side_effect=fail_active):
            with self.assertRaises(OSError):
                service.process()
        self.assertEqual(len(service.vault.list_markdown("history")), 1)
        self.assertEqual(self.active(service).body, "old")
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["watermark"], 1)

        self.clock.advance(hours=1)
        backend.responses.extend(responses)
        service.process()
        self.assertEqual(len(service.vault.list_markdown("history")), 1)
        self.assertEqual(self.active(service).body, "new")
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["watermark"], 2)

    def test_index_and_processed_write_failures_are_retryable_without_extra_history(self):
        backend = QueueBackend()
        service = self.make_service(backend)
        active, _, _ = self.process_create(service, backend, title="Original", body="b2b old")
        user_key, _ = self.capture_turn(
            service,
            turn="t2",
            user_event="u2",
            assistant_event="a2",
            user="user t2 b2b old",
        )
        candidate = self.candidate(
            "candidate-t2", [user_key], memory=f"{active.title} {active.body}"
        )
        responses = [self.gate([candidate]), self.summary(user_key, title="Updated", body="b2b new", update_memory_id=active.memory_id)]

        backend.responses.extend(responses)
        original_rebuild = service._rebuild_index_unlocked
        rebuild_calls = {"count": 0}

        def fail_rebuild():
            rebuild_calls["count"] += 1
            if rebuild_calls["count"] == 1:
                raise OSError("index failed")
            return original_rebuild()

        with patch.object(service, "_rebuild_index_unlocked", side_effect=fail_rebuild):
            with self.assertRaises(OSError):
                service.process()
        self.assertEqual(len(service.vault.list_markdown("history")), 1)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["watermark"], 1)
        backend.responses.extend(responses)
        service.process()
        self.assertEqual(len(service.vault.list_markdown("history")), 1)

        service2 = self.make_service(QueueBackend(), name="processed-failure")
        backend2 = service2.router
        active2, _, _ = self.process_create(service2, backend2, title="Original", body="b2b old")
        user_key2, _ = self.capture_turn(
            service2,
            turn="t2",
            user_event="p-u2",
            assistant_event="p-a2",
            user="user t2 b2b old",
        )
        candidate2 = self.candidate(
            "candidate-t2", [user_key2], memory=f"{active2.title} {active2.body}"
        )
        responses2 = [self.gate([candidate2]), self.summary(user_key2, title="Updated", body="b2b new", update_memory_id=active2.memory_id)]
        backend2.responses.extend(responses2)
        import memleaf.process_journal as processing_module

        original_atomic = processing_module.atomic_write_json
        calls = {"count": 0}

        def fail_processed(path, value):
            if path == service2.vault.processed_index_path:
                calls["count"] += 1
                if value.get("sessions", {}).get("codex/s", {}).get("watermark", 0) >= 2 and value["sessions"]["codex/s"].get("processing", {}).get("status") == "idle":
                    raise OSError("processed write failed")
            return original_atomic(path, value)

        with patch.object(processing_module, "atomic_write_json", side_effect=fail_processed):
            with self.assertRaises(OSError):
                service2.process()
        self.assertEqual(self.processed(service2)["sessions"]["codex/s"]["watermark"], 1)
        self.assertEqual(len(service2.vault.list_markdown("history")), 1)
        backend2.responses.extend(responses2)
        service2.process()
        self.assertEqual(len(service2.vault.list_markdown("history")), 1)

    def test_cleanup_hours_config_is_default_customized_and_strict(self):
        backend = QueueBackend()
        service = self.make_service(backend)
        config = service.vault.config()
        self.assertEqual(config["process"]["inbox_cleanup_hours"], 24)
        config["process"]["inbox_cleanup_hours"] = 2
        save_config(service.vault.config_path, config)
        self.process_create(service, backend, title="Cleanup", body="cleanup")
        entry = self.processed(service)["sessions"]["codex/s"]["processed_turns"][0]
        self.assertEqual(entry["eligible_cleanup_at"], "2026-08-24T02:00:00Z")

        config["process"]["inbox_cleanup_hours"] = True
        save_config(service.vault.config_path, config)
        with self.assertRaises(ValueError):
            service.vault.config()
        with self.assertRaises(ProcessingError):
            service.process()

    def test_cleanup_runs_without_backend_preserves_ledger_and_recapture_is_duplicate(self):
        backend = QueueBackend()
        service = self.make_service(backend)
        self.process_create(service, backend, title="Cleanup", body="cleanup")
        user_path = service.vault.inbox_path / "codex" / "s.md"
        self.assertTrue(user_path.exists())
        self.clock.advance(hours=24)
        service.router = None

        result = service.process()

        self.assertEqual(result["processed_turns"], 0)
        self.assertEqual(result["cleaned_turns"], 1)
        self.assertFalse(user_path.exists())
        processed = self.processed(service)
        entry = processed["sessions"]["codex/s"]["processed_turns"][0]
        self.assertIn("cleanup_done_at", entry)
        self.assertTrue(processed["event_keys"])
        service.rebuild_index()
        rebuilt = self.processed(service)
        self.assertTrue(set(entry["event_keys"]).issubset(set(rebuilt["event_keys"])))
        duplicate_user = service.capture("codex", "s", "t1", "user", "user t1", event_id="u1")
        duplicate_assistant = service.capture("codex", "s", "t1", "assistant", "assistant t1", event_id="a1")
        self.assertTrue(duplicate_user.duplicate)
        self.assertTrue(duplicate_assistant.duplicate)
        self.assertFalse(user_path.exists())

    def test_cleanup_preserves_unprocessed_and_unexpired_blocks(self):
        backend = QueueBackend()
        service = self.make_service(backend)
        self.process_create(service, backend, title="First", body="first")
        self.process_create(service, backend, turn="t2", user_event="u2", assistant_event="a2", title="Second", body="second")
        service.capture("codex", "s", "t3", "user", "incomplete", event_id="u3")
        value = self.processed(service)
        entries = value["sessions"]["codex/s"]["processed_turns"]
        entries[0]["eligible_cleanup_at"] = "2026-08-23T00:00:00Z"
        entries[1]["eligible_cleanup_at"] = "2026-08-25T00:00:00Z"
        service.vault.processed_index_path.write_text(json.dumps(value), encoding="utf-8")

        result = service.process()

        self.assertEqual(result["cleaned_turns"], 1)
        turns = service.vault.inbox_path / "codex" / "s.md"
        text = turns.read_text(encoding="utf-8")
        self.assertNotIn(event_key("u1"), text)
        self.assertIn(event_key("u2"), text)
        self.assertIn(event_key("u3"), text)

    def test_cleanup_failure_does_not_mark_done_and_retries(self):
        backend = QueueBackend()
        service = self.make_service(backend)
        self.process_create(service, backend, title="Cleanup", body="cleanup")
        self.clock.advance(hours=24)
        import memleaf.process_journal as processing_module

        original_atomic = processing_module.atomic_write_json

        def fail_cleanup_processed(path, value):
            if path == service.vault.processed_index_path:
                raise OSError("cleanup ledger failed")
            return original_atomic(path, value)

        with patch.object(processing_module, "atomic_write_json", side_effect=fail_cleanup_processed):
            with self.assertRaises(OSError):
                service.process()
        entry = self.processed(service)["sessions"]["codex/s"]["processed_turns"][0]
        self.assertNotIn("cleanup_done_at", entry)

        result = service.process()
        self.assertEqual(result["cleaned_turns"], 1)
        self.assertIn("cleanup_done_at", self.processed(service)["sessions"]["codex/s"]["processed_turns"][0])

        service2 = self.make_service(QueueBackend(), name="index-cleanup-failure")
        backend2 = service2.router
        self.process_create(service2, backend2, title="Cleanup", body="cleanup")
        self.clock.advance(hours=24)
        original_rebuild = service2._rebuild_index_unlocked

        with patch.object(service2, "_rebuild_index_unlocked", side_effect=OSError("cleanup index failed")):
            with self.assertRaises(OSError):
                service2.process()
        entry2 = self.processed(service2)["sessions"]["codex/s"]["processed_turns"][0]
        self.assertNotIn("cleanup_done_at", entry2)
        self.assertEqual(service2.process()["cleaned_turns"], 1)

    def test_batch_duplicate_update_target_and_deterministic_collision_are_zero_write(self):
        backend = QueueBackend()
        service = self.make_service(backend)
        active, _, _ = self.process_create(service, backend, title="Original", body="old")
        user_key, _ = self.capture_turn(
            service,
            turn="t2",
            user_event="u2",
            assistant_event="a2",
            user="user t2 b2b old",
        )
        candidates = [
            self.candidate("c1", [user_key], memory=f"{active.title} {active.body}"),
            self.candidate("c2", [user_key], memory=f"{active.title} {active.body} second"),
        ]
        backend.responses.extend(
            [
                self.gate(candidates),
                self.summary(user_key, title="One", body="one", update_memory_id=active.memory_id),
                self.summary(user_key, title="Two", body="two", update_memory_id=active.memory_id),
            ]
        )
        with self.assertRaises(ModelOutputError):
            service.process()
        self.assertEqual(self.active(service).body, "old")
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["watermark"], 1)

        service2 = self.make_service(QueueBackend(), name="collision")
        backend2 = service2.router
        self.process_create(service2, backend2, title="Original", body="old")
        user_key2, _ = self.capture_turn(service2, turn="t2", user_event="cu2", assistant_event="ca2")
        candidates2 = [self.candidate("c1", [user_key2]), self.candidate("c2", [user_key2], memory="second")]
        backend2.responses.extend(
            [
                self.gate(candidates2),
                self.summary(user_key2, title="One", body="one"),
                self.summary(user_key2, title="Two", body="two"),
            ]
        )
        with patch.object(MemoryWriter, "deterministic_memory_id", return_value="mem-collision"):
            with self.assertRaises(ModelOutputError):
                service2.process()
        self.assertEqual(len(service2.vault.list_markdown("knowledge")), 1)
        self.assertEqual(self.active(service2).body, "old")
        self.assertEqual(len(service2.vault.list_markdown("history")), 0)
        self.assertEqual(self.processed(service2)["sessions"]["codex/s"]["watermark"], 1)


if __name__ == "__main__":
    unittest.main()
