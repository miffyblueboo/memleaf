import copy
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.compaction import CompactionError
from memleaf.config import save_config
from memleaf.index import event_key
from memleaf.llm import ModelError
from memleaf.locking import atomic_write_text


class FakeClock:
    def __init__(self):
        self.value = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, *, hours=0):
        self.value += timedelta(hours=hours)


class QueueBackend:
    provider = "fake"
    model = "b3a-commit"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        self.calls.append({"prompt": prompt, "purpose": purpose})
        if not self.responses:
            raise ModelError("queue exhausted")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class StageB3ACommitTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.service = Memleaf(Path(self.tempdir.name) / "vault", clock=self.clock)

    def tearDown(self):
        self.tempdir.cleanup()

    def set_process(self, *, threshold=1, ratio=1.0):
        config = self.service.vault.config()
        config["process"]["memory_compact_threshold_tokens"] = threshold
        config["process"]["memory_compact_candidate_ratio"] = ratio
        save_config(self.service.vault.config_path, config)

    def add_memory(self, memory_id, body, *, tags=None, sources=None, extra=None):
        return self.service.create_memory(
            memory_id=memory_id,
            title=memory_id,
            body=body,
            tags=tags or [memory_id],
            type="fact",
            scopes=["global"],
            scope_source="model",
            sources=sources or [],
            extra=extra or {},
        )

    @staticmethod
    def item(source_ids, *, title="Merged", body="merged fact", tags=None, type="fact", **extra):
        value = {
            "title": title,
            "body": body,
            "tags": tags or ["merged"],
            "type": type,
            "scopes": ["global"],
            "scope_source": "model",
            "aliases": [],
            "keywords": [],
            "source_memory_ids": list(source_ids),
        }
        value.update(extra)
        return value

    def compact_response(self, source_ids, **kwargs):
        return json.dumps({"memories": [self.item(source_ids, **kwargs)]})

    def make_pair(self):
        first = self.add_memory("m1", "A" * 1000, tags=["one"], sources=[{"event_key": "e1"}])
        second = self.add_memory("m2", "B" * 1000, tags=["two"], sources=[{"event_key": "e2"}])
        self.set_process()
        return first, second

    def assert_old_state(self, first, second):
        self.assertEqual(
            {record.memory.memory_id for record in self.service._read_memories_unlocked("knowledge")},
            {first.memory_id, second.memory_id},
        )
        self.assertEqual(self.service._read_memories_unlocked("history"), [])
        self.assertFalse(self.service.vault.compaction_journal_path.exists())

    def test_two_to_one_commit_keeps_unconsumed_and_indexes(self):
        first, second = self.make_pair()
        third = self.add_memory("m3", "C" * 1000, tags=["keep"])
        backend = QueueBackend([self.compact_response([first.memory_id, second.memory_id])])

        result = self.service.compact(model=backend)

        self.assertEqual(result["status"], "compacted")
        self.assertEqual(result["compacted"], 2)
        self.assertEqual(len(result["replacements"]), 1)
        replacement_id = result["replacements"][0]
        active_ids = {record.memory.memory_id for record in self.service._read_memories_unlocked("knowledge")}
        self.assertEqual(active_ids, {replacement_id, third.memory_id})
        history = self.service._read_memories_unlocked("history")
        self.assertEqual(len(history), 2)
        self.assertEqual({record.memory.extra["reason"] for record in history}, {"compaction"})
        self.assertEqual({record.memory.extra["active_memory_id"] for record in history}, {replacement_id})
        self.assertEqual({record.memory.extra["compacted_into"] for record in history}, {replacement_id})
        self.assertTrue(self.service.search("merged"))
        self.assertTrue(self.service.search("one", include_history=True, todo_status="all"))
        self.assertFalse(self.service.vault.compaction_journal_path.exists())

    def test_multiple_replacements_and_partial_consumption(self):
        memories = [self.add_memory(f"m{i}", chr(65 + i) * 900) for i in range(4)]
        self.set_process(ratio=1.0)
        response = json.dumps(
            {
                "memories": [
                    self.item([memories[0].memory_id, memories[1].memory_id], title="first merge"),
                    self.item([memories[2].memory_id, memories[3].memory_id], title="second merge"),
                ]
            }
        )

        result = self.service.compact(model=QueueBackend([response]))

        self.assertEqual(result["status"], "compacted")
        self.assertEqual(result["compacted"], 4)
        self.assertEqual(len(result["replacements"]), 2)
        self.assertEqual(len(self.service._read_memories_unlocked("knowledge")), 2)
        self.assertEqual(len(self.service._read_memories_unlocked("history")), 4)

    def test_forget_replacement_removes_linked_history_but_history_id_is_precise(self):
        first, second = self.make_pair()
        result = self.service.compact(
            model=QueueBackend([self.compact_response([first.memory_id, second.memory_id])])
        )
        replacement_id = result["replacements"][0]
        history_ids = [record.memory.memory_id for record in self.service._read_memories_unlocked("history")]

        self.assertTrue(self.service.forget_memory(history_ids[0]))
        self.assertIsNotNone(self.service.read(replacement_id))
        self.assertEqual(len(self.service._read_memories_unlocked("history")), 1)
        self.assertTrue(self.service.forget_memory(replacement_id))
        self.assertIsNone(self.service.read(replacement_id))
        self.assertEqual(self.service._read_memories_unlocked("history"), [])

    def test_snapshot_edit_rejects_without_writing_or_deleting(self):
        first, second = self.make_pair()
        original_path = self.service.vault.memory_path(first.memory_id, "knowledge")
        response = self.compact_response([first.memory_id, second.memory_id])

        def edit_then_return(prompt, **kwargs):
            text = original_path.read_text(encoding="utf-8").replace("A" * 1000, "USER EDIT")
            atomic_write_text(original_path, text)
            return response

        with self.assertRaises(CompactionError):
            self.service.compact(model=edit_then_return)

        self.assertIn("USER EDIT", original_path.read_text(encoding="utf-8"))
        self.assertEqual(self.service._read_memories_unlocked("history"), [])
        self.assertEqual(
            {record.memory.memory_id for record in self.service._read_memories_unlocked("knowledge")},
            {first.memory_id, second.memory_id},
        )

    def _assert_injected_failure_rolls_back(self, fail):
        first, second = self.make_pair()
        response = self.compact_response([first.memory_id, second.memory_id])
        with fail:
            with self.assertRaises(CompactionError):
                self.service.compact(model=QueueBackend([response]))
        # Every public read entry point must recover/settle the pending state
        # under the vault lock before exposing Markdown-derived data.
        self.service.stats()
        self.assert_old_state(first, second)
        self.assertEqual(self.service.search("m1"), [first])

    def test_journal_failure_rolls_back_and_cleans_staging(self):
        def fail(*args, **kwargs):
            raise OSError("journal injection")

        self._assert_injected_failure_rolls_back(patch("memleaf.compaction.atomic_write_json", side_effect=fail))

    def test_staging_failure_rolls_back_without_journal(self):
        original = __import__("memleaf.compaction", fromlist=["atomic_write_text"]).atomic_write_text

        def fail(path, text, *args, **kwargs):
            if ".compaction-staging" in str(path):
                raise OSError("staging injection")
            return original(path, text, *args, **kwargs)

        self._assert_injected_failure_rolls_back(patch("memleaf.compaction.atomic_write_text", side_effect=fail))

    def test_history_failure_rolls_back_partial_history(self):
        original = __import__("memleaf.compaction", fromlist=["atomic_write_text"]).atomic_write_text

        def fail(path, text, *args, **kwargs):
            if Path(path).parent == self.service.vault.history_path:
                raise OSError("history injection")
            return original(path, text, *args, **kwargs)

        self._assert_injected_failure_rolls_back(patch("memleaf.compaction.atomic_write_text", side_effect=fail))

    def test_replacement_failure_rolls_back_history(self):
        original = __import__("memleaf.compaction", fromlist=["atomic_write_text"]).atomic_write_text

        def fail(path, text, *args, **kwargs):
            path = Path(path)
            if path.parent == self.service.vault.knowledge_path and "merged fact" in text:
                raise OSError("replacement injection")
            return original(path, text, *args, **kwargs)

        self._assert_injected_failure_rolls_back(patch("memleaf.compaction.atomic_write_text", side_effect=fail))

    def test_source_unlink_failure_rolls_back_partial_delete(self):
        original = __import__("memleaf.compaction", fromlist=["atomic_unlink"]).atomic_unlink

        def fail(path):
            if Path(path).name == "m2.md":
                raise OSError("unlink injection")
            return original(path)

        self._assert_injected_failure_rolls_back(patch("memleaf.compaction.atomic_unlink", side_effect=fail))

    def test_index_failure_rolls_back_and_rebuilds_on_recovery(self):
        first, second = self.make_pair()
        response = self.compact_response([first.memory_id, second.memory_id])
        original = self.service._rebuild_index_unlocked
        calls = {"count": 0}

        def fail_once():
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError("index injection")
            return original()

        with patch.object(self.service, "_rebuild_index_unlocked", side_effect=fail_once):
            with self.assertRaises(CompactionError):
                self.service.compact(model=QueueBackend([response]))
        self.service.rebuild_index()
        self.assert_old_state(first, second)
        self.assertGreaterEqual(calls["count"], 2)

    def test_clear_failure_leaves_complete_new_state_and_next_read_clears_journal(self):
        first, second = self.make_pair()
        response = self.compact_response([first.memory_id, second.memory_id])
        original = __import__("memleaf.compaction", fromlist=["atomic_unlink"]).atomic_unlink
        journal = self.service.vault.compaction_journal_path
        calls = {"count": 0}

        def fail_once(path):
            if Path(path) == journal and calls["count"] == 0:
                calls["count"] += 1
                raise OSError("clear injection")
            return original(path)

        with patch("memleaf.compaction.atomic_unlink", side_effect=fail_once):
            with self.assertRaises(CompactionError):
                self.service.compact(model=QueueBackend([response]))
        self.assertTrue(self.service.search("merged"))
        self.assertFalse(journal.exists())
        self.assertEqual(len(self.service._read_memories_unlocked("history")), 2)

    def test_successful_history_does_not_delete_manually_restored_source(self):
        first, second = self.make_pair()
        original = first.to_markdown()
        result = self.service.compact(
            model=QueueBackend([self.compact_response([first.memory_id, second.memory_id])])
        )
        self.assertEqual(result["status"], "compacted")
        restored_path = self.service.vault.memory_path(first.memory_id, "knowledge")
        atomic_write_text(restored_path, original)
        self.service.rebuild_index()

        no_op = self.service.compact(model=QueueBackend(['{"memories": []}']))

        self.assertEqual(no_op["status"], "noop")
        self.assertTrue(restored_path.exists())
        self.assertIsNotNone(self.service.read(first.memory_id))

    def test_process_no_pending_turn_can_auto_compact_when_due(self):
        self.add_memory("active", "A" * 1000)
        self.set_process(threshold=1, ratio=1.0)
        backend = QueueBackend(['{"memories": []}'])

        result = self.service.process(model=backend)

        self.assertEqual(result["processed_turns"], 0)
        self.assertEqual(result["compaction"]["status"], "noop")
        self.assertEqual([call["purpose"] for call in backend.calls], ["compact"])

    def test_process_success_survives_compaction_model_failure(self):
        user_key = event_key("u1")
        self.service.capture("codex", "s", "t1", "user", "remember this", event_id="u1")
        self.service.capture("codex", "s", "t1", "assistant", "ack", event_id="a1")
        gate = {
            "candidates": [
                {
                    "candidate_id": "c1",
                    "memory": "remember this",
                    "evidence_event_ids": [user_key],
                    "duplicate": False,
                    "worth": True,
                    "type": "fact",
                    "scopes": ["global"],
                    "scope_source": "model",
                }
            ]
        }
        summary = {
            "title": "Processed",
            "body": "P" * 1000,
            "tags": ["processed"],
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
            "sources": [{"event_key": user_key}],
        }
        self.set_process(threshold=1, ratio=1.0)
        backend = QueueBackend([json.dumps(gate), json.dumps(summary), "not json"])

        result = self.service.process(model=backend)

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["compaction"]["status"], "invalid_output")
        processed = json.loads(self.service.vault.processed_index_path.read_text(encoding="utf-8"))
        self.assertEqual(processed["sessions"]["codex/s"]["watermark"], 1)
        self.assertEqual(len(self.service._read_memories_unlocked("knowledge")), 1)

    def test_remember_explicit_metadata_and_compaction_error_are_safe(self):
        remembered_key = event_key("remember-event")
        summary = {
            "title": "Explicit",
            "body": "E" * 700,
            "tags": ["explicit"],
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "user",
            "sources": [{"event_key": remembered_key}],
        }
        self.set_process(threshold=100000)
        remember_backend = QueueBackend([json.dumps(summary)])
        remembered = self.service.remember(
            "explicit content",
            source="codex",
            session_id="remember",
            turn_id="turn",
            event_id="remember-event",
            model=remember_backend,
        )
        remembered_memory = self.service.read(remembered["memory_ids"][0])
        self.assertTrue(remembered_memory.extra["explicit_remember"])

        regular = self.add_memory("regular", "R" * 700)
        self.set_process(threshold=1, ratio=0.5)
        no_op = self.service.compact(model=QueueBackend(['{"memories": []}']))
        self.assertEqual(no_op["candidates"], [regular.memory_id])

        self.set_process(threshold=1, ratio=1.0)
        response = self.compact_response([remembered_memory.memory_id, regular.memory_id], body="merged")
        compacted = self.service.compact(model=QueueBackend([response]))

        self.assertEqual(compacted["status"], "compacted")
        replacement = self.service.read(compacted["replacements"][0])
        self.assertTrue(replacement.extra["explicit_remember"])

    def test_process_and_remember_do_not_call_compact_below_threshold(self):
        self.add_memory("small", "small")
        self.set_process(threshold=100000)
        process_backend = QueueBackend([])
        process_result = self.service.process(model=process_backend)
        self.assertEqual(process_result["compaction"]["status"], "not_due")
        self.assertEqual(process_backend.calls, [])

        remembered_key = event_key("remember-small")
        summary = {
            "title": "small remember",
            "body": "small",
            "tags": [],
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "user",
            "sources": [{"event_key": remembered_key}],
        }
        remember_backend = QueueBackend([json.dumps(summary)])
        remember_result = self.service.remember(
            "small remember",
            source="codex",
            session_id="small",
            turn_id="turn",
            event_id="remember-small",
            model=remember_backend,
        )
        self.assertEqual(remember_result["compaction"]["status"], "not_due")
        self.assertEqual([call["purpose"] for call in remember_backend.calls], ["summarize"])


if __name__ == "__main__":
    unittest.main()
