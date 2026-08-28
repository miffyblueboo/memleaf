import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.native_index import MAX_NATIVE_BYTES


class StageB3BNativeIndexTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.service = Memleaf(self.root / "vault")

    def tearDown(self):
        self.tempdir.cleanup()

    def set_native(self, sources):
        config = self.service.vault.config()
        config["native_sources"] = sources
        save_config(self.service.vault.config_path, config)

    def read_index(self):
        return json.loads(self.service.vault.native_sources_index_path.read_text(encoding="utf-8"))

    def test_initialization_creates_versioned_empty_index(self):
        self.assertEqual(
            self.read_index(),
            {"version": 1, "sources": {}},
        )

    def test_markdown_index_has_bounded_metadata_without_body(self):
        source = self.root / "native.md"
        secret_sentence = "PRIVATE_NATIVE_SENTENCE_7f4d9c"
        source.write_text(f"# First\n{secret_sentence}\n\n## Second\npublic line\n", encoding="utf-8")
        self.set_native(
            {
                "codex-main": {
                    "agent": "codex",
                    "path": str(source),
                    "share": True,
                }
            }
        )

        result = self.service.refresh_native_sources()
        index_text = self.service.vault.native_sources_index_path.read_text(encoding="utf-8")
        entry = self.read_index()["sources"]["codex-main"]

        self.assertEqual(result["native_sources"], 1)
        self.assertEqual(result["native_segments"], 2)
        self.assertEqual(entry["availability"], "available")
        self.assertEqual(entry["format"], "markdown")
        self.assertTrue(entry["path"].endswith("native.md"))
        self.assertNotIn(secret_sentence, index_text)
        self.assertTrue(all("body" not in segment for segment in entry["segments"]))
        self.assertTrue(all(len(segment["normalized_terms"]) <= 64 for segment in entry["segments"]))
        self.assertTrue(all(len(term) <= 48 for segment in entry["segments"] for term in segment["normalized_terms"]))
        self.assertTrue(all(segment["native_id"].startswith("native-") for segment in entry["segments"]))

    def test_incremental_refresh_skips_unchanged_reads_and_only_rebuilds_changed_source(self):
        first = self.root / "first.txt"
        second = self.root / "second.txt"
        first.write_text("first one\n", encoding="utf-8")
        second.write_text("second one\n", encoding="utf-8")
        self.set_native(
            {
                "one": {"agent": "codex", "path": str(first), "share": True, "format": "text"},
                "two": {"agent": "hermes", "path": str(second), "share": False, "format": "text"},
            }
        )
        original_read_bytes = Path.read_bytes
        reads = []

        def tracked_read(path):
            if path in (first, second):
                reads.append(path)
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", tracked_read):
            self.service.refresh_native_sources()
            reads.clear()
            before = self.read_index()["sources"]
            self.service.refresh_native_sources()
            self.assertEqual(reads, [])
            self.assertEqual(self.read_index()["sources"], before)

            first.write_text("first changed and longer\n", encoding="utf-8")
            reads.clear()
            result = self.service.refresh_native_sources()

        self.assertEqual(reads, [first])
        self.assertEqual(result["changed_sources"], ["one"])
        self.assertEqual(self.read_index()["sources"]["two"], before["two"])

    def test_full_refresh_reads_unchanged_sources_again(self):
        source = self.root / "full.txt"
        source.write_text("full refresh\n", encoding="utf-8")
        self.set_native({"one": {"agent": "codex", "path": str(source), "share": True, "format": "text"}})
        self.service.refresh_native_sources()
        original_read_bytes = Path.read_bytes
        reads = []

        def tracked_read(path):
            if path == source:
                reads.append(path)
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", tracked_read):
            self.service.refresh_native_sources(full=True)
        self.assertEqual(reads, [source])

    def test_missing_decode_symlink_too_large_and_disabled_are_diagnostic(self):
        missing = self.root / "missing.md"
        invalid = self.root / "invalid.txt"
        invalid.write_bytes(b"\xff\xfe")
        target = self.root / "target.md"
        target.write_text("target", encoding="utf-8")
        disabled_target = self.root / "disabled.md"
        disabled_target.write_text("disabled", encoding="utf-8")
        link = self.root / "link.md"
        link.symlink_to(target)
        large = self.root / "large.txt"
        large.write_text("0123456789abcdef", encoding="utf-8")
        unreadable = self.root / "unreadable.txt"
        unreadable.write_text("unreadable", encoding="utf-8")
        self.set_native(
            {
                "missing": {"agent": "codex", "path": str(missing), "share": True},
                "invalid": {"agent": "codex", "path": str(invalid), "share": True, "format": "text"},
                "link": {"agent": "codex", "path": str(link), "share": True},
                "large": {"agent": "codex", "path": str(large), "share": True, "format": "text"},
                "unreadable": {"agent": "codex", "path": str(unreadable), "share": True, "format": "text"},
                "disabled": {"agent": "codex", "path": str(disabled_target), "share": True, "enabled": False},
            }
        )
        original_read_bytes = Path.read_bytes

        def fail_unreadable(path):
            if path == unreadable:
                raise PermissionError("denied")
            return original_read_bytes(path)

        with patch("memleaf.native_index.MAX_NATIVE_BYTES", 10), patch.object(Path, "read_bytes", fail_unreadable):
            result = self.service.refresh_native_sources()
        sources = self.read_index()["sources"]

        self.assertEqual(sources["missing"]["error_category"], "missing")
        self.assertEqual(sources["invalid"]["error_category"], "decode_error")
        self.assertEqual(sources["link"]["error_category"], "unsafe")
        self.assertEqual(sources["large"]["error_category"], "too_large")
        self.assertEqual(sources["unreadable"]["error_category"], "unreadable")
        self.assertEqual(sources["disabled"]["availability"], "disabled")
        self.assertEqual(sources["disabled"]["segments"], [])
        self.assertEqual(result["native_unavailable"], 6)

    def test_config_rejects_unknown_types_duplicate_paths_directories_and_bad_strings(self):
        file_path = self.root / "same.txt"
        file_path.write_text("same", encoding="utf-8")
        config = self.service.vault.config()
        invalid = [
            {"one": {"agent": "codex", "path": str(file_path), "share": "yes"}},
            {"one": {"agent": "codex", "path": str(file_path), "share": True, "wat": 1}},
            {
                "one": {"agent": "codex", "path": str(file_path), "share": True},
                "two": {"agent": "hermes", "path": str(file_path), "share": True},
            },
            {"one": {"agent": "codex", "path": str(self.root), "share": True}},
            {"one\n": {"agent": "codex", "path": str(file_path), "share": True}},
            {"one": {"agent": "codex", "path": str(file_path) + "\n", "share": True}},
        ]
        for native_sources in invalid:
            with self.subTest(native_sources=native_sources):
                candidate = dict(config)
                candidate["native_sources"] = native_sources
                with self.assertRaises(ValueError):
                    save_config(self.service.vault.config_path, candidate)

    def test_config_removal_drops_old_source_entry_and_preserves_disabled_metadata(self):
        first = self.root / "first.md"
        second = self.root / "second.md"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        self.set_native(
            {
                "first": {"agent": "codex", "path": str(first), "share": True},
                "second": {"agent": "hermes", "path": str(second), "share": True, "enabled": False},
            }
        )
        self.service.refresh_native_sources()
        self.set_native({"second": {"agent": "hermes", "path": str(second), "share": True, "enabled": False}})

        result = self.service.refresh_native_sources()
        sources = self.read_index()["sources"]

        self.assertEqual(set(sources), {"second"})
        self.assertEqual(sources["second"]["availability"], "disabled")
        self.assertEqual(result["native_sources"], 1)

    def test_text_segments_and_locator_refresh_stale_file(self):
        source = self.root / "notes.md"
        source.write_text("# Notes\nline one\nline two\n", encoding="utf-8")
        self.set_native({"notes": {"agent": "codex", "path": str(source), "share": True}})
        self.service.refresh_native_sources()
        old_entry = self.read_index()["sources"]["notes"]
        old_segment = old_entry["segments"][0]

        source.write_text("# Notes\nline one\nline two\n# New\nnew line\n", encoding="utf-8")
        content = self.service.read_native_segment("notes", old_segment["native_id"])

        self.assertEqual(content, "# Notes\nline one\nline two")
        self.assertNotEqual(self.read_index()["sources"]["notes"]["size"], old_entry["size"])

    def test_locator_rejects_tampered_out_of_bounds_metadata(self):
        source = self.root / "locator.md"
        source.write_text("# Heading\nbody\n", encoding="utf-8")
        self.set_native({"locator": {"agent": "codex", "path": str(source), "share": True}})
        self.service.refresh_native_sources()
        index = self.read_index()
        segment = index["sources"]["locator"]["segments"][0]
        segment["start_line"] = 999
        self.service.vault.native_sources_index_path.write_text(json.dumps(index), encoding="utf-8")

        self.assertIsNone(self.service.read_native_segment("locator", segment["native_id"]))

    def test_concurrent_refresh_keeps_all_sources_and_rebuild_stats(self):
        first = self.root / "first.txt"
        second = self.root / "second.txt"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        self.set_native(
            {
                "first": {"agent": "codex", "path": str(first), "share": True, "format": "text"},
                "second": {"agent": "hermes", "path": str(second), "share": True, "format": "text"},
            }
        )
        errors = []

        def refresh():
            try:
                self.service.refresh_native_sources()
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=refresh, daemon=True) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2.0)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(set(self.read_index()["sources"]), {"first", "second"})

        result = self.service.rebuild_index()
        stats = self.service.stats()
        self.assertEqual(result["native_sources"], 2)
        self.assertEqual(result["native_segments"], 2)
        self.assertEqual(stats["native_sources"], 2)
        self.assertEqual(stats["native_segments"], 2)
        self.assertEqual(stats["native_unavailable"], 0)


if __name__ == "__main__":
    unittest.main()
