from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf
from memleaf.budget import MAX_CONTEXT_CHARS, MAX_CONTEXT_ITEMS
from memleaf.config import save_config
from memleaf.models import MemoryVersionError


class ContextBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.service = Memleaf(Path(self.tempdir.name) / "vault")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_context_caps_more_than_one_hundred_matches_and_smaller_limit(self) -> None:
        for index in range(101):
            self.service.create_memory(
                memory_id=f"bulk-{index}",
                title=f"Bulk memory {index}",
                body=f"shared recall topic {index}",
                tags=["shared-recall"],
            )

        bounded = self.service.context("shared-recall", limit=99)
        smaller = self.service.context("shared-recall", limit=2)
        defaulted = self.service.context("shared-recall")
        empty = self.service.context("shared-recall", limit=0)

        self.assertEqual(MAX_CONTEXT_ITEMS, len(bounded))
        self.assertEqual(2, len(smaller))
        self.assertEqual(MAX_CONTEXT_ITEMS, len(defaulted))
        self.assertEqual([], empty)
        self.assertEqual(101, len(self.service.search("shared-recall")))
        self.assertEqual(0, sum(memory.hit_count for memory in self.service.search("shared-recall")))
        self.assertTrue(all(set(entry.to_dict()) == {"memory_id", "title", "scopes"} for entry in bounded))

    def test_context_applies_cumulative_budget_to_complete_items(self) -> None:
        for index in range(6):
            self.service.create_memory(
                memory_id=f"cumulative-{index}",
                title=f"累计预算 {index}",
                body="cumulative-budget " + "中文内容" * 280,
                sources=[{"source": "来源", "detail": "metadata" * 30}],
            )

        result = self.service.context("cumulative-budget")
        encoded = json.dumps(
            [memory.to_dict() for memory in result],
            ensure_ascii=False,
            separators=(",", ":"),
        )

        self.assertGreater(len(result), 0)
        self.assertLessEqual(len(result), MAX_CONTEXT_ITEMS)
        self.assertLessEqual(len(encoded), MAX_CONTEXT_CHARS)
        self.assertNotIn("cumulative-budget", encoded)
        self.assertNotIn("sources", encoded)
        self.assertTrue(all(set(entry.to_dict()) == {"memory_id", "title", "scopes"} for entry in result))

    def test_context_skips_oversized_body_and_metadata_without_truncating(self) -> None:
        large_body = self.service.create_memory(
            memory_id="large-body",
            title="Large body",
            body="budget-match " + "x" * MAX_CONTEXT_CHARS,
        )
        large_metadata = self.service.create_memory(
            memory_id="large-metadata",
            title="Large metadata",
            body="budget-match metadata",
            extra_field="m" * MAX_CONTEXT_CHARS,
        )
        small = self.service.create_memory(
            memory_id="small-body",
            title="中文记忆",
            body="budget-match 预算匹配的完整正文",
        )

        result = self.service.context("budget-match", limit=99)
        result_ids = {memory.memory_id for memory in result}

        self.assertIn(small.memory_id, result_ids)
        self.assertIn(large_body.memory_id, result_ids)
        self.assertIn(large_metadata.memory_id, result_ids)
        self.assertEqual(0, self.service.read(large_body.memory_id).hit_count)
        self.assertEqual(0, self.service.read(large_metadata.memory_id).hit_count)
        self.assertEqual(0, self.service.read(small.memory_id).hit_count)
        self.assertTrue(all(set(entry.to_dict()) == {"memory_id", "title", "scopes"} for entry in result))
        self.assertLessEqual(
            len(json.dumps([entry.to_dict() for entry in result], ensure_ascii=False, separators=(",", ":"))),
            MAX_CONTEXT_CHARS,
        )

    def test_empty_context_query_does_not_scan_or_inject_vault(self) -> None:
        memory = self.service.create_memory(
            memory_id="global-memory",
            title="Global memory",
            body="would otherwise match",
        )

        self.assertEqual([], self.service.context("   "))
        self.assertEqual([], self.service.context([" ", "\t"], limit=99))
        self.assertEqual(0, self.service.read(memory.memory_id).hit_count)

    def test_search_directory_is_opt_in_and_bounded(self) -> None:
        for index in range(8):
            self.service.create_memory(
                memory_id=f"directory-{index}",
                title=f"目录标题 {index}",
                body=f"directory query {index}",
                tags=["directory"],
            )
        full = self.service.search("directory")
        directory = self.service.search("directory", view="directory")
        self.assertEqual(8, len(full))
        self.assertEqual(MAX_CONTEXT_ITEMS, len(directory))
        self.assertTrue(all(set(item.to_dict()) == {"memory_id", "title", "scopes"} for item in directory))
        self.assertLessEqual(
            len(json.dumps([item.to_dict() for item in directory], ensure_ascii=False, separators=(",", ":"))),
            MAX_CONTEXT_CHARS,
        )
        with self.assertRaises(ValueError):
            self.service.search("directory", view="unknown")

    def test_generator_query_is_reused_for_scope_resolution(self) -> None:
        config = self.service.vault.config()
        config["scopes"] = {"project:alpha": {"aliases": ["alpha"]}}
        save_config(self.service.vault.config_path, config)
        self.service.create_memory(
            memory_id="generator-global",
            title="Generator global",
            body="generator topic",
            scopes=["global"],
        )
        alpha = self.service.create_memory(
            memory_id="generator-alpha",
            title="Generator alpha",
            body="generator topic",
            scopes=["project:alpha"],
        )

        query = (part for part in ("alpha", "generator", "topic"))
        result = self.service.context(query)

        self.assertIn(alpha.memory_id, {memory.memory_id for memory in result})

    def test_search_and_read_remain_full_content_paths(self) -> None:
        body = "full search body " + "z" * (MAX_CONTEXT_CHARS + 100)
        memory = self.service.create_memory(
            memory_id="full-memory",
            title="Full memory",
            body=body,
        )

        self.assertEqual([memory.memory_id], [item.memory_id for item in self.service.search("full search")])
        self.assertEqual(body, self.service.read(memory.memory_id).body)

    def test_read_page_reassembles_body_and_counts_only_active_first_page(self) -> None:
        body = "首行\n" + "分页正文-" * 900
        memory = self.service.create_memory(
            memory_id="paged-memory",
            title="分页记忆",
            body=body,
            sources=[{"source": "visible-event"}],
            updated="2026-08-27T00:00:00Z",
        )
        before = self.service.read(memory.memory_id)
        self.assertEqual(0, before.hit_count)
        self.assertEqual("2026-08-27T00:00:00Z", before.updated)
        self.assertEqual([{"source": "visible-event"}], before.sources)

        directory = self.service.context("分页正文")
        self.assertEqual([memory.memory_id], [entry.memory_id for entry in directory])
        self.assertEqual(0, self.service.read(memory.memory_id).hit_count)

        first = self.service.read_page(memory.memory_id, max_chars=37)
        self.assertIsNotNone(first)
        self.assertEqual(
            {"memory_id", "title", "scopes", "body", "offset", "next_offset", "has_more", "total_chars", "version"},
            set(first),
        )
        self.assertLessEqual(len(first["body"]), 37)
        self.assertEqual(1, self.service.read(memory.memory_id).hit_count)
        version = first["version"]
        pages = [first["body"]]
        page = first
        while page["has_more"]:
            page = self.service.read_page(
                memory.memory_id,
                offset=page["next_offset"],
                max_chars=37,
                expected_version=version,
            )
            pages.append(page["body"])
        self.assertEqual(body, "".join(pages))
        self.assertEqual(1, self.service.read(memory.memory_id).hit_count)
        self.assertEqual(
            {"memory_id", "title", "scopes", "body", "offset", "next_offset", "has_more", "total_chars", "version"},
            set(page),
        )

        after = self.service.read(memory.memory_id)
        self.assertEqual("2026-08-27T00:00:00Z", after.updated)
        self.assertEqual([{"source": "visible-event"}], after.sources)
        end = self.service.read_page(memory.memory_id, offset=len(body), expected_version=version)
        self.assertEqual("", end["body"])
        self.assertEqual(1, self.service.read(memory.memory_id).hit_count)

        with self.assertRaises(MemoryVersionError):
            self.service.read_page(memory.memory_id, expected_version="stale")
        with self.assertRaises(ValueError):
            self.service.read_page(memory.memory_id, max_chars=2001)
        with self.assertRaises(ValueError):
            self.service.read_page(memory.memory_id, offset=-1)

    def test_read_page_history_is_read_only_and_native_ids_are_optional(self) -> None:
        history = self.service.create_memory(
            memory_id="history-page",
            title="历史分页",
            body="历史正文",
            hit_count=4,
            area="history",
        )
        result = self.service.read_page(history.memory_id, include_history=True)
        self.assertEqual("历史正文", result["body"])
        self.assertEqual(4, self.service.read(history.memory_id, include_history=True).hit_count)
        self.assertIsNone(self.service.read_page(history.memory_id))

    def test_read_page_version_detects_source_update_without_extra_hit(self) -> None:
        memory = self.service.create_memory(
            memory_id="versioned-page",
            title="版本分页",
            body="稳定正文-" * 500,
        )
        first = self.service.read_page(memory.memory_id, max_chars=37)
        self.assertEqual(1, self.service.read(memory.memory_id).hit_count)
        path = self.service.vault.knowledge_path / "versioned-page.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("稳定正文-", "更新正文-"),
            encoding="utf-8",
        )
        with self.assertRaises(MemoryVersionError):
            self.service.read_page(
                memory.memory_id,
                offset=first["next_offset"],
                max_chars=37,
                expected_version=first["version"],
            )
        self.assertEqual(1, self.service.read(memory.memory_id).hit_count)


if __name__ == "__main__":
    unittest.main()
