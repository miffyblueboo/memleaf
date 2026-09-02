from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.native_registration import ensure_hermes_native_sources


class HermesNativeRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="memleaf-hermes-native-")
        self.root = Path(self.tempdir.name)
        self.service = Memleaf.initialize(self.root / "vault")
        self.hermes_home = self.root / "Hermes Home 测试"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def config(self):
        return self.service.vault.config()

    def test_registers_missing_files_privately_without_creating_them(self) -> None:
        result = ensure_hermes_native_sources(self.service.vault, self.hermes_home)

        self.assertTrue(result["changed"])
        config = self.config()
        memory = config["native_sources"][result["sources"]["hermes_memory"]]
        user = config["native_sources"][result["sources"]["hermes_user"]]
        for source, filename in ((memory, "MEMORY.md"), (user, "USER.md")):
            self.assertEqual(source["agent"], "hermes")
            self.assertIs(source["share"], False)
            self.assertEqual(source["format"], "markdown")
            self.assertEqual(Path(source["path"]), (self.hermes_home / "memories" / filename).resolve())
            self.assertFalse(Path(source["path"]).exists())
        self.assertEqual(result["index"]["native_sources"], 2)
        self.assertEqual(result["index"]["native_unavailable"], 2)

    def test_repeat_is_byte_idempotent_and_preserves_custom_source(self) -> None:
        custom = self.root / "custom.md"
        custom.write_text("# custom\nkeep me\n", encoding="utf-8")
        config = self.config()
        config["native_sources"] = {
            "custom": {"agent": "codex", "path": str(custom), "share": True, "format": "markdown"}
        }
        save_config(self.service.vault.config_path, config)

        first = ensure_hermes_native_sources(self.service.vault, self.hermes_home)
        before = self.service.vault.config_path.read_bytes()
        second = ensure_hermes_native_sources(self.service.vault, self.hermes_home)

        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(before, self.service.vault.config_path.read_bytes())
        current = self.config()["native_sources"]
        self.assertEqual(current["custom"]["path"], str(custom))
        self.assertTrue(current["custom"]["share"])

    def test_existing_equivalent_path_under_custom_id_is_reused(self) -> None:
        memory_path = self.hermes_home / "memories" / "MEMORY.md"
        config = self.config()
        config["native_sources"] = {
            "my_hermes_notes": {
                "agent": "hermes",
                "path": str(memory_path),
                "share": False,
                "format": "markdown",
            }
        }
        save_config(self.service.vault.config_path, config)

        result = ensure_hermes_native_sources(self.service.vault, self.hermes_home)

        self.assertEqual(result["sources"]["hermes_memory"], "my_hermes_notes")
        current = self.config()["native_sources"]
        self.assertIn("my_hermes_notes", current)
        self.assertNotIn("hermes_memory", current)
        self.assertIn(result["sources"]["hermes_user"], current)

    def test_canonical_id_conflict_uses_stable_builtin_fallback(self) -> None:
        other = self.root / "other.md"
        config = self.config()
        config["native_sources"] = {
            "hermes_memory": {
                "agent": "codex",
                "path": str(other),
                "share": False,
                "format": "markdown",
            }
        }
        save_config(self.service.vault.config_path, config)

        first = ensure_hermes_native_sources(self.service.vault, self.hermes_home)
        second = ensure_hermes_native_sources(self.service.vault, self.hermes_home)

        self.assertEqual(first["sources"]["hermes_memory"], "hermes_memory_builtin")
        self.assertEqual(second["sources"]["hermes_memory"], "hermes_memory_builtin")
        current = self.config()["native_sources"]
        self.assertEqual(current["hermes_memory"]["agent"], "codex")
        self.assertIn("hermes_memory_builtin", current)

    def test_same_path_with_share_true_fails_closed_without_rewrite(self) -> None:
        memory_path = self.hermes_home / "memories" / "MEMORY.md"
        config = self.config()
        config["native_sources"] = {
            "unsafe_shared": {
                "agent": "hermes",
                "path": str(memory_path),
                "share": True,
                "format": "markdown",
            }
        }
        save_config(self.service.vault.config_path, config)
        before = self.service.vault.config_path.read_bytes()

        with self.assertRaises(RuntimeError):
            ensure_hermes_native_sources(self.service.vault, self.hermes_home)

        self.assertEqual(before, self.service.vault.config_path.read_bytes())

    def test_existing_native_files_remain_byte_identical(self) -> None:
        memories = self.hermes_home / "memories"
        memories.mkdir(parents=True)
        memory_path = memories / "MEMORY.md"
        user_path = memories / "USER.md"
        memory_path.write_text("# Notes\nprivate project fact\n", encoding="utf-8")
        user_path.write_text("# User\nprivate preference\n", encoding="utf-8")
        before = {path: path.read_bytes() for path in (memory_path, user_path)}

        ensure_hermes_native_sources(self.service.vault, self.hermes_home)
        self.service.refresh_native_sources()
        self.service.context("private project fact", source="codex", session_id="s")

        for path, payload in before.items():
            self.assertEqual(payload, path.read_bytes())
        # share:false keeps Hermes-native content out of another agent's context.
        self.assertEqual([], self.service.context("private preference", source="codex", session_id="s"))


if __name__ == "__main__":
    unittest.main()
