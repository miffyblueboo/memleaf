import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memleaf import Memleaf, Memory
from memleaf.config import save_config
from memleaf.source_policy import MAX_MEMORY_SOURCES, merge_sources


class QueueBackend:
    provider = "fake"
    model = "hygiene"

    def __init__(self, responses):
        self.responses = list(responses)

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        if not self.responses:
            raise RuntimeError("queue exhausted")
        return self.responses.pop(0)


class FixedClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class LongRunHygieneTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        self.service = Memleaf(Path(self.tempdir.name) / "vault", clock=FixedClock(self.now))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_active_provenance_is_bounded(self):
        sources, metadata = merge_sources([], [{"event_key": f"e-{i}"} for i in range(100)])
        self.assertEqual(len(sources), MAX_MEMORY_SOURCES)
        self.assertEqual(metadata["source_count"], 100)
        self.assertEqual(metadata["sources_omitted"], 100 - MAX_MEMORY_SOURCES)
        self.assertEqual(sources[0]["event_key"], "e-0")
        self.assertEqual(sources[-1]["event_key"], "e-99")

        # The public raw-write API uses the same bounded representation.
        written = self.service.create_memory(
            memory_id="bounded-raw", title="Bounded raw", body="body",
            sources=[{"event_key": f"raw-{i}"} for i in range(100)],
        )
        self.assertEqual(len(written.sources), MAX_MEMORY_SOURCES)
        self.assertEqual(written.extra["source_count"], 100)
        reread = self.service.read("bounded-raw")
        self.assertEqual(len(reread.sources), MAX_MEMORY_SOURCES)
        self.assertEqual(reread.extra["sources_omitted"], 100 - MAX_MEMORY_SOURCES)

    def test_maintenance_bounds_legacy_oversized_provenance(self):
        legacy = Memory.new(
            memory_id="legacy-large-sources", title="Legacy", body="legacy",
            sources=[{"event_key": f"legacy-{i}"} for i in range(80)],
        )
        path = self.service.vault.memory_path(legacy.memory_id, "knowledge")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(legacy.to_markdown(), encoding="utf-8")
        result = self.service.process()
        self.assertEqual(result["compaction"]["retention"]["provenance_rewritten"], 1)
        current = self.service.read(legacy.memory_id)
        self.assertEqual(len(current.sources), MAX_MEMORY_SOURCES)
        self.assertEqual(current.extra["source_count"], 80)

    def test_closed_todo_retires_from_active_and_remains_queryable_as_history(self):
        old = (self.now - timedelta(days=31)).isoformat(timespec="seconds").replace("+00:00", "Z")
        todo = self.service.create_memory(
            memory_id="todo-closed",
            title="Closed task",
            body="The task is complete.",
            type="todo",
            scopes=["global"],
            status="completed",
            completed_at=old,
            created=old,
            updated=old,
        )
        result = self.service.process()
        self.assertEqual(result["compaction"]["retention"]["closed_todos_retired"], 1)
        self.assertIsNone(self.service.read(todo.memory_id))
        listed = self.service.list_todos(status="completed")
        self.assertEqual(listed["status"], "found")
        self.assertTrue(listed["results"][0]["history"])
        historical = self.service.read(listed["results"][0]["memory_id"], include_history=True)
        self.assertEqual(historical.extra["active_memory_id"], todo.memory_id)
        self.assertEqual(historical.extra["invalidated_reason"], "todo_closed")

    def test_history_policy_prunes_by_count_and_age(self):
        config = self.service.vault.config()
        config["history"]["retention_days"] = 100
        config["history"]["max_versions_per_memory"] = 3
        save_config(self.service.vault.config_path, config)
        for index in range(6):
            archived = self.now - timedelta(days=index * 10)
            self.service.create_memory(
                memory_id=f"hist-{index}",
                title=f"History {index}",
                body=f"version {index}",
                type="fact",
                scopes=["global"],
                area="history",
                active_memory_id="stable-memory",
                archived_at=archived.isoformat(timespec="seconds").replace("+00:00", "Z"),
                updated=archived.isoformat(timespec="seconds").replace("+00:00", "Z"),
            )
        result = self.service.process()
        self.assertEqual(result["compaction"]["retention"]["history_pruned"], 3)
        self.assertEqual(len(self.service._read_memories_unlocked("history")), 3)

    def test_compaction_cannot_change_canonical_memory_type(self):
        source = self.service.create_memory(
            memory_id="stable-type", title="Stable type", body="X" * 1000,
            type="fact", scopes=["global"], sources=[{"event_key": "type-1"}],
        )
        config = self.service.vault.config()
        config["process"]["memory_compact_threshold_tokens"] = 1
        config["process"]["memory_compact_candidate_ratio"] = 1.0
        save_config(self.service.vault.config_path, config)
        response = json.dumps({"memories": [{
            "title": "Retyped", "body": "short", "tags": [], "type": "project",
            "scopes": ["global"], "scope_source": "model", "aliases": [], "keywords": [],
            "source_memory_ids": [source.memory_id],
        }]})
        with self.assertRaisesRegex(Exception, "cannot change memory type"):
            self.service.compact(model=QueueBackend([response]))
        current = self.service.read(source.memory_id)
        self.assertIsNotNone(current)
        self.assertEqual(current.type, "fact")
        self.assertEqual(self.service.vault.list_markdown("history"), [])

    def test_compaction_preserves_canonical_memory_identity(self):
        first = self.service.create_memory(
            memory_id="stable-a", title="A", body="A" * 1000, type="fact", scopes=["global"],
            hit_count=10, sources=[{"event_key": "e1"}],
        )
        second = self.service.create_memory(
            memory_id="stable-b", title="B", body="B" * 1000, type="fact", scopes=["global"],
            hit_count=1, sources=[{"event_key": "e2"}],
        )
        config = self.service.vault.config()
        config["process"]["memory_compact_threshold_tokens"] = 1
        config["process"]["memory_compact_candidate_ratio"] = 1.0
        save_config(self.service.vault.config_path, config)
        response = json.dumps({"memories": [{
            "title": "Merged", "body": "compact state", "tags": ["merged"], "type": "fact",
            "scopes": ["global"], "scope_source": "model", "aliases": [], "keywords": [],
            "source_memory_ids": [first.memory_id, second.memory_id],
        }]})
        result = self.service.compact(model=QueueBackend([response]))
        self.assertEqual(result["replacements"], [first.memory_id])
        self.assertIsNotNone(self.service.read(first.memory_id))
        self.assertIsNone(self.service.read(second.memory_id))
        self.assertFalse(any(path.stem.startswith("mem-compact-") for path in self.service.vault.list_markdown("knowledge")))

    def test_core_decision_code_contains_no_source_specific_business_lexicon(self):
        root = Path(__file__).parents[1] / "src" / "memleaf"
        files = ["prompts.py", "validation.py", "memory_planner.py", "planning_context.py"]
        forbidden = (
            "邮箱", "邮件", "附件", "收件箱", "mailbox", "attachment", "daily report",
            "watchlist", "email", "meeting", "会议", "实施计划", "项目计划",
            "implementation plan", "project plan", "ppt",
        )
        text = "\n".join((root / name).read_text(encoding="utf-8").casefold() for name in files)
        for token in forbidden:
            self.assertNotIn(token.casefold(), text)


if __name__ == "__main__":
    unittest.main()
