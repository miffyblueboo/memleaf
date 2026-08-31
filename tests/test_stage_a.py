import json
import hashlib
import tempfile
import threading
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from memleaf import (
    FrontmatterError,
    Memleaf,
    Memory,
    Vault,
    dump_frontmatter,
    dump_yaml,
    load_config,
    load_yaml,
    parse_frontmatter,
    save_config,
)


class StageATest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.vault_path = Path(self.tempdir.name) / "vault"
        self.service = Memleaf(self.vault_path)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_vault_initialization_and_restricted_yaml_frontmatter(self):
        for name in ("inbox", "knowledge", "history", "_index"):
            self.assertTrue((self.vault_path / name).is_dir())
        self.assertFalse((self.vault_path / "logs").exists())
        self.assertTrue((self.vault_path / "config.yaml").is_file())
        config = load_config(self.vault_path / "config.yaml")
        self.assertEqual(config["capture"]["redact_secrets"], True)
        self.assertEqual(config["process"]["memory_compact_threshold_tokens"], 100000)
        self.assertEqual(config["process"]["memory_compact_candidate_ratio"], 0.30)
        self.assertTrue((self.vault_path / "_index" / "tags.json").is_file())
        self.assertTrue((self.vault_path / "_index" / "processed.json").is_file())
        self.assertEqual(json.loads((self.vault_path / "_index" / "tags.json").read_text())["tags"], {})
        self.assertEqual(json.loads((self.vault_path / "_index" / "processed.json").read_text())["event_keys"], [])

        metadata = {
            "title": "数据库决定",
            "memory_id": "m-yaml",
            "tags": ["database"],
            "sources": [{"session_id": "s1", "turn_id": "t1"}],
        }
        rendered = dump_frontmatter(metadata, "使用 MySQL。")
        parsed_metadata, body = parse_frontmatter(rendered)
        self.assertEqual(parsed_metadata, metadata)
        self.assertEqual(body, "使用 MySQL。")
        self.assertEqual(load_yaml(dump_yaml(config)), config)
        with self.assertRaises(FrontmatterError):
            load_yaml("a: &anchor\n")

    def test_capture_is_idempotent_incremental_redacted_and_path_safe(self):
        secret = "sk-proj-12345678901234567890"
        first = self.service.capture(
            "codex", "session-1", "turn-1", "user",
            f"Use api_key={secret} and Cookie: sid=private-cookie",
            event_id="event-1",
        )
        duplicate = self.service.capture(
            "codex", "session-1", "turn-1", "user", "different text", event_id="event-1"
        )
        second = self.service.capture(
            "codex", "session-1", "turn-2", "assistant", "Confirmed.", event_id="event-2"
        )
        self.assertTrue(first.stored)
        self.assertTrue(second.stored)
        self.assertTrue(duplicate.duplicate)
        session_file = self.vault_path / "inbox" / "codex" / "session-1.md"
        text = session_file.read_text(encoding="utf-8")
        self.assertEqual(text.count("<!-- memleaf:event-key:v1:"), 2)
        self.assertNotIn(secret, text)
        self.assertNotIn("private-cookie", text)
        self.assertIn("[REDACTED_SECRET]", text)

        skipped = self.service.capture(
            "codex", "session-2", "turn-1", "user", "do not persist", record=False
        )
        hidden = self.service.capture(
            "codex", "session-3", "turn-1", "system", "hidden", visible=True
        )
        self.assertFalse(skipped.stored)
        self.assertFalse(hidden.stored)
        self.assertFalse((self.vault_path / "inbox" / "codex" / "session-2.md").exists())
        self.assertFalse((self.vault_path / "inbox" / "codex" / "session-3.md").exists())
        with self.assertRaises(ValueError):
            self.service.capture("../escape", "s", "t", "user", "x")
        with self.assertRaises(ValueError):
            self.service.capture("codex", "../escape", "t", "user", "x")

    def test_concurrent_capture_does_not_lose_events(self):
        count = 32
        barrier = threading.Barrier(count)

        def capture(number):
            barrier.wait()
            return self.service.capture(
                "hermes", "shared-session", f"turn-{number}", "user",
                f"fact {number}", event_id=f"event-{number}"
            )

        with ThreadPoolExecutor(max_workers=count) as executor:
            results = list(executor.map(capture, range(count)))
        self.assertEqual(sum(result.stored for result in results), count)
        session_file = self.vault_path / "inbox" / "hermes" / "shared-session.md"
        text = session_file.read_text(encoding="utf-8")
        self.assertEqual(text.count("<!-- memleaf:event-key:v1:"), count)
        self.assertEqual(
            len(set(line for line in text.splitlines() if "<!-- memleaf:event-key:v1:" in line)),
            count,
        )
        processed = json.loads((self.vault_path / "_index" / "processed.json").read_text())
        self.assertEqual(len(processed["events"]), count)

    def test_markdown_source_of_truth_and_rebuildable_indexes(self):
        self.service.create_memory(
            memory_id="m-db", title="Database", body="MySQL is used.",
            tags=["database"], aliases=["db"], keywords=["storage"],
        )
        self.service.create_memory(
            memory_id="m-body", title="Unindexed", body="needle-only-body",
            scopes=["global"],
        )
        tags_path = self.vault_path / "_index" / "tags.json"
        tags_path.write_text("not json", encoding="utf-8")
        self.assertEqual([item.memory_id for item in self.service.search("database")], ["m-db"])
        self.assertTrue(json.loads(tags_path.read_text(encoding="utf-8"))["tags"]["database"])

        memory_path = self.vault_path / "knowledge" / "m-db.md"
        edited = memory_path.read_text(encoding="utf-8").replace("Database", "Edited database")
        memory_path.write_text(edited, encoding="utf-8")
        memory_path.unlink()
        self.service.rebuild_index()
        self.assertIsNone(self.service.read("m-db"))
        self.assertEqual(self.service.stats()["knowledge"], 1)

    def test_tag_union_scope_inheritance_fulltext_history_and_todo(self):
        config = load_config(self.vault_path / "config.yaml")
        config["scopes"] = {
            "domain:engineering": {"children": ["portfolio:apps"]},
            "portfolio:apps": {"children": ["project:alpha", "project:beta"]},
        }
        save_config(self.vault_path / "config.yaml", config)
        self.service.create_memory(
            memory_id="m-global", title="Shared policy", body="Global policy.",
            tags=["mysql"], scopes=["global"],
        )
        self.service.create_memory(
            memory_id="m-domain", title="Domain policy", body="Engineering policy.",
            tags=["mysql"], scopes=["domain:engineering"],
        )
        self.service.create_memory(
            memory_id="m-portfolio", title="Portfolio policy", body="Apps policy.",
            tags=["mysql"], scopes=["portfolio:apps"],
        )
        self.service.create_memory(
            memory_id="m-alpha", title="Alpha policy", body="Alpha policy.",
            tags=["mysql"], scopes=["project:alpha"],
        )
        self.service.create_memory(
            memory_id="m-beta", title="Beta policy", body="Beta policy.",
            tags=["mysql"], scopes=["project:beta"],
        )
        self.service.create_memory(
            memory_id="m-release", title="Release", body="Release alias.",
            aliases=["ship"], scopes=["global"],
        )
        self.service.create_memory(
            memory_id="m-history", title="Old database", body="Old PostgreSQL state.",
            tags=["postgres"], scopes=["global"], area="history"
        )
        self.service.create_memory(
            memory_id="m-active-todo", title="Todo active", body="Do active thing.",
            tags=["todo"], type="todo", status="active"
        )
        self.service.create_memory(
            memory_id="m-done-todo", title="Todo done", body="Done thing.",
            tags=["todo"], type="todo", status="completed"
        )

        alpha_ids = {item.memory_id for item in self.service.search("mysql", scope="project:alpha")}
        self.assertEqual(alpha_ids, {"m-global", "m-domain", "m-portfolio", "m-alpha"})
        union_ids = {item.memory_id for item in self.service.search("mysql ship", scope="project:alpha")}
        self.assertIn("m-release", union_ids)
        self.assertNotIn("m-beta", union_ids)
        self.assertEqual(
            [item.memory_id for item in self.service.search("needle-only-body")],
            [],
        )
        self.service.create_memory(
            memory_id="m-fallback", title="Fallback", body="needle-only-body", scopes=["global"]
        )
        self.assertEqual([item.memory_id for item in self.service.search("needle-only-body")], ["m-fallback"])
        self.assertNotIn("m-history", {item.memory_id for item in self.service.search("postgres")})
        self.assertIn("m-history", {item.memory_id for item in self.service.search("postgres", include_history=True)})
        todo_ids = {item.memory_id for item in self.service.search("todo")}
        self.assertEqual(todo_ids, {"m-active-todo"})
        self.assertEqual(
            {item.memory_id for item in self.service.search("todo", todo_status="all")},
            {"m-active-todo", "m-done-todo"},
        )
        context = self.service.context("mysql", scope="project:alpha")
        self.assertTrue(context)
        self.assertEqual(self.service.read("m-alpha").hit_count, 0)
        self.assertEqual({"memory_id", "title", "scopes"}, set(context[0].to_dict()))

    def test_forget_exact_and_ambiguous_without_history(self):
        self.service.create_memory(
            memory_id="m-exact", title="Exact", body="unique exact fact", tags=["exact"]
        )
        self.service.create_memory(
            memory_id="m-a", title="A", body="same topic", tags=["topic"]
        )
        self.service.create_memory(
            memory_id="m-b", title="B", body="same topic", tags=["topic"]
        )
        ambiguous = self.service.forget_about("topic")
        self.assertTrue(ambiguous.is_ambiguous)
        self.assertEqual({item.memory_id for item in ambiguous.candidates}, {"m-a", "m-b"})
        self.assertIsNotNone(self.service.read("m-a"))
        self.assertTrue(self.service.forget_memory("m-exact"))
        self.assertIsNone(self.service.read("m-exact"))
        self.assertFalse((self.vault_path / "history" / "m-exact.md").exists())
        deleted = self.service.forget_about("A")
        self.assertEqual(deleted.status, "deleted")
        self.assertIsNone(self.service.read("m-a"))
        self.assertFalse((self.vault_path / "history" / "m-a.md").exists())

    def test_context_without_scope_only_uses_global_but_search_is_explicitly_broad(self):
        self.service.create_memory(
            memory_id="m-global-context", title="Global context", body="global fact",
            tags=["shared"], scopes=["global"],
        )
        self.service.create_memory(
            memory_id="m-project-context", title="Project context", body="project fact",
            tags=["shared"], scopes=["project:alpha"],
        )
        self.assertEqual(
            {item.memory_id for item in self.service.search("shared")},
            {"m-global-context", "m-project-context"},
        )
        self.assertEqual(
            {item.memory_id for item in self.service.context("shared")},
            {"m-global-context"},
        )
        self.assertEqual(
            {item.memory_id for item in self.service.context("shared", scope="project:alpha")},
            {"m-global-context", "m-project-context"},
        )

    def test_forget_about_deletes_history_only_and_all_versions_of_same_id(self):
        self.service.create_memory(
            memory_id="m-history-only", title="Old single target", body="old target body",
            tags=["old-target"], scopes=["global"], area="history",
        )
        result = self.service.forget_about("Old single target")
        self.assertEqual(result.status, "deleted")
        self.assertEqual(result.deleted_memory_ids, ["m-history-only"])
        self.assertIsNone(self.service.read("m-history-only", include_history=True))
        self.assertFalse((self.vault_path / "history" / "m-history-only.md").exists())

        self.service.create_memory(
            memory_id="m-two-versions", title="Current version", body="current",
            tags=["versioned"], scopes=["global"],
        )
        self.service.create_memory(
            memory_id="m-two-versions", title="Previous version", body="previous",
            tags=["versioned"], scopes=["global"], area="history",
        )
        result = self.service.forget_about("m-two-versions")
        self.assertEqual(result.status, "deleted")
        self.assertIsNone(self.service.read("m-two-versions", include_history=True))
        self.assertFalse((self.vault_path / "knowledge" / "m-two-versions.md").exists())
        self.assertFalse((self.vault_path / "history" / "m-two-versions.md").exists())

    def test_forget_partial_failure_keeps_active_retry_anchor_and_rebuilds_index(self):
        active = self.service.create_memory(
            memory_id="m-retry-anchor",
            title="Retry anchor",
            body="current",
            tags=["retry-delete"],
        )
        first_history = self.service.create_memory(
            memory_id="hist-retry-a",
            title="Old A",
            body="old-a",
            tags=["retry-delete"],
            area="history",
            active_memory_id=active.memory_id,
        )
        second_history = self.service.create_memory(
            memory_id="hist-retry-b",
            title="Old B",
            body="old-b",
            tags=["retry-delete"],
            area="history",
            active_memory_id=active.memory_id,
        )
        active_path = self.vault_path / "knowledge" / f"{active.memory_id}.md"
        first_path = self.vault_path / "history" / f"{first_history.memory_id}.md"
        second_path = self.vault_path / "history" / f"{second_history.memory_id}.md"
        original_unlink = Path.unlink
        attempted: list[str] = []

        def flaky_unlink(path, *args, **kwargs):
            attempted.append(path.name)
            if path == second_path:
                raise PermissionError("simulated history delete failure")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(
            self.service,
            "_rebuild_index_unlocked",
            wraps=self.service._rebuild_index_unlocked,
        ) as rebuild, mock.patch.object(Path, "unlink", new=flaky_unlink):
            with self.assertRaises(PermissionError):
                self.service.forget_memory(active.memory_id)
            self.assertGreaterEqual(rebuild.call_count, 1)

        self.assertFalse(first_path.exists())
        self.assertTrue(second_path.exists())
        self.assertTrue(active_path.exists())
        self.assertNotIn(active_path.name, attempted)

        self.assertTrue(self.service.forget_memory(active.memory_id))
        self.assertFalse(second_path.exists())
        self.assertFalse(active_path.exists())

    def test_sensitive_event_id_uses_stable_digest_after_rebuild(self):
        raw_event_id = "session/password=top-secret-token"
        result = self.service.capture(
            "codex", "secret-event", "turn-1", "user", "visible text", event_id=raw_event_id
        )
        self.assertTrue(result.stored)
        session_text = result.path.read_text(encoding="utf-8")
        self.assertNotIn("top-secret-token", session_text)
        self.assertNotIn(raw_event_id, session_text)
        expected_key = hashlib.sha256(raw_event_id.encode("utf-8")).hexdigest()
        self.assertIn(expected_key, session_text)
        processed_text = (self.vault_path / "_index" / "processed.json").read_text(encoding="utf-8")
        self.assertNotIn("top-secret-token", processed_text)
        self.assertIn(expected_key, processed_text)

        self.service.rebuild_index()
        duplicate = self.service.capture(
            "codex", "secret-event", "turn-1", "user", "different", event_id=raw_event_id
        )
        self.assertTrue(duplicate.duplicate)
        self.assertFalse(duplicate.stored)
        self.assertEqual(
            result.path.read_text(encoding="utf-8").count("<!-- memleaf:event-key:v1:"),
            1,
        )

    def test_user_marker_lookalikes_are_escaped_and_do_not_poison_rebuild(self):
        victim_key = hashlib.sha256("victim".encode("utf-8")).hexdigest()
        fake_content = (
            f"<!-- event_id: victim -->\n"
            f"<!-- memleaf:event-key:v1:{victim_key} -->\n"
        )
        first = self.service.capture(
            "codex", "marker-fake", "turn-1", "user", fake_content, event_id="real-event"
        )
        self.assertTrue(first.stored)
        self.service.rebuild_index()
        victim = self.service.capture(
            "codex", "marker-fake", "turn-2", "user", "real victim event", event_id="victim"
        )
        self.assertTrue(victim.stored)
        self.assertFalse(victim.duplicate)
        text = first.path.read_text(encoding="utf-8")
        self.assertIn("&lt;!-- event_id: victim --&gt;", text)
        self.assertIn("&lt;!-- memleaf:event-key:v1:", text)
        self.assertEqual(text.count("<!-- memleaf:event-key:v1:"), 2)

    def test_ascii_tag_matching_uses_boundaries_while_chinese_allows_substring(self):
        self.service.create_memory(
            memory_id="m-bug-tag", title="Bug tag", body="unrelated body",
            tags=["bug"], scopes=["global"],
        )
        self.service.create_memory(
            memory_id="m-debugger-body", title="Debugger", body="debugger details",
            scopes=["global"],
        )
        self.assertEqual(
            [item.memory_id for item in self.service.search("debugger")],
            ["m-debugger-body"],
        )
        self.service.create_memory(
            memory_id="m-chinese-tag", title="数据库", body="数据库配置",
            tags=["数据库"], scopes=["global"],
        )
        self.assertIn("m-chinese-tag", {item.memory_id for item in self.service.search("数据库配置")})

    def test_vault_initialization_preserves_existing_indexes(self):
        tags_path = self.vault_path / "_index" / "tags.json"
        processed_path = self.vault_path / "_index" / "processed.json"
        tags_value = {
            "version": 1,
            "tags": {"keep": ["m-keep"]},
            "aliases": {},
            "keywords": {},
            "history": {"tags": {}, "aliases": {}, "keywords": {}},
        }
        processed_value = {
            "version": 1,
            "event_keys": ["a" * 64],
            "events": {"a" * 64: {"event_key": "a" * 64}},
        }
        tags_path.write_text(json.dumps(tags_value), encoding="utf-8")
        processed_path.write_text(json.dumps(processed_value), encoding="utf-8")
        before_tags = tags_path.read_text(encoding="utf-8")
        before_processed = processed_path.read_text(encoding="utf-8")
        Vault(self.vault_path)
        self.assertEqual(tags_path.read_text(encoding="utf-8"), before_tags)
        self.assertEqual(processed_path.read_text(encoding="utf-8"), before_processed)


if __name__ == "__main__":
    unittest.main()
