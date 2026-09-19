"""Hermes installer native-file provisioning tests."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.incremental_native import read_comparison
from memleaf.installer import _ensure_hermes_native_files, _rollback_hermes_native_files
from memleaf.native_registration import ensure_hermes_native_sources


class HermesNativeProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memleaf-hermes-native-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "hermes-home"

    def test_missing_standard_files_are_created_empty_and_idempotent(self):
        result, provision = _ensure_hermes_native_files(self.home)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["created"], ["MEMORY.md", "USER.md"])
        for name in ("MEMORY.md", "USER.md"):
            path = self.home / "memories" / name
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), b"")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

        again, second = _ensure_hermes_native_files(self.home)
        self.assertEqual(again["created"], [])
        self.assertEqual(again["existing"], ["MEMORY.md", "USER.md"])
        self.assertEqual(second.created_files, ())
        self.assertFalse(second.directory_created)
        self.assertEqual(len(provision.created_files), 2)

    def test_existing_native_bytes_are_never_rewritten(self):
        memories = self.home / "memories"
        memories.mkdir(parents=True)
        memory = memories / "MEMORY.md"
        user = memories / "USER.md"
        memory.write_bytes(b"# MEMORY\nkeep me\n")
        user.write_bytes(b"# USER\nkeep me too\n")
        before = (memory.read_bytes(), user.read_bytes())

        result, provision = _ensure_hermes_native_files(self.home)

        self.assertEqual(result["created"], [])
        self.assertEqual((memory.read_bytes(), user.read_bytes()), before)
        self.assertEqual(provision.created_files, ())

    def test_unsafe_native_path_fails_without_overwriting(self):
        memories = self.home / "memories"
        memories.mkdir(parents=True)
        bad = memories / "MEMORY.md"
        bad.mkdir()
        with self.assertRaises(RuntimeError):
            _ensure_hermes_native_files(self.home)
        self.assertTrue(bad.is_dir())
        self.assertFalse((memories / "USER.md").exists())

    def test_created_files_can_be_rolled_back_without_touching_existing_file(self):
        memories = self.home / "memories"
        memories.mkdir(parents=True)
        original = memories / "MEMORY.md"
        original.write_bytes(b"existing")
        _, provision = _ensure_hermes_native_files(self.home)
        self.assertTrue((memories / "USER.md").exists())

        self.assertEqual(_rollback_hermes_native_files(provision), "completed")
        self.assertEqual(original.read_bytes(), b"existing")
        self.assertFalse((memories / "USER.md").exists())
        self.assertTrue(memories.exists())

    def test_provisioned_files_make_incremental_native_comparison_available(self):
        _ensure_hermes_native_files(self.home)
        service = Memleaf.initialize(self.root / "vault")
        registration = ensure_hermes_native_sources(service.vault, self.home)
        comparison = read_comparison(service, "hermes")

        self.assertEqual(set(registration["sources"]), {"hermes_memory", "hermes_user"})
        self.assertEqual(len(comparison.guard["sources"]), 2)
        self.assertEqual(comparison.memories, {})


if __name__ == "__main__":
    unittest.main()
