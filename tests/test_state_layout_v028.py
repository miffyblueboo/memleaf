from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memleaf import Memleaf
from memleaf.host_runtime import HostRuntime
from memleaf.locking import atomic_write_json
from memleaf.retrieval_gate import begin_turn, validate_turn
from memleaf.state_layout import StateLayoutError
from memleaf.vault import Vault


class StateLayoutV028Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="memleaf-state-layout-")
        self.root = Path(self.tempdir.name) / "vault"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def _legacy_processed() -> dict:
        return {
            "version": 1,
            "event_keys": ["a" * 64],
            "events": {"a" * 64: {"event_key": "a" * 64}},
            "sessions": {
                "codex/s1": {
                    "watermark": 7,
                    "processed_watermark": 7,
                    "lineage_parent_session_id": "parent",
                    "processed_turns": [{"turn_key": "t", "turn_index": 7, "event_keys": ["a" * 64]}],
                }
            },
            "pending_turn_plans": {"plan": {"status": "pending"}},
            "pending_operations": {"op": {"status": "prepared"}},
        }

    def _make_legacy(self) -> None:
        index = self.root / "_index"
        index.mkdir(parents=True, exist_ok=True)
        (self.root / "knowledge").mkdir(exist_ok=True)
        (self.root / "history").mkdir(exist_ok=True)
        (self.root / "inbox").mkdir(exist_ok=True)
        atomic_write_json(index / "processed.json", self._legacy_processed())
        atomic_write_json(index / "agents.json", {"version": 1, "agents": {"codex": {"hook_activation_status": "active"}}})
        atomic_write_json(index / "host_ingest.json", {"version": 2, "hosts": {"codex": {"s1": {"process_pending": True}}}, "transcripts": {}})
        atomic_write_json(index / "retrieval_gate.json", {"version": 1, "entries": {"rtv-old": {"status": "FOUND"}}})
        atomic_write_json(index / "compaction.json", {"version": 1, "transaction_id": "tx", "phase": "staged", "sources": [], "replacements": [], "histories": []})
        staging = index / ".compaction-staging" / "tx"
        staging.mkdir(parents=True)
        (staging / "original.md").write_text("original", encoding="utf-8")

    def test_old_vault_migrates_all_runtime_state(self) -> None:
        self._make_legacy()
        vault = Vault(self.root)
        self.assertEqual(json.loads(vault.processed_state_path.read_text()), self._legacy_processed())
        self.assertEqual(json.loads(vault.agents_state_path.read_text())["agents"]["codex"]["hook_activation_status"], "active")
        self.assertTrue(vault.host_ingest_path.is_file())
        self.assertTrue(vault.retrieval_gate_state_path.is_file())
        self.assertTrue(vault.compaction_journal_path.is_file())
        self.assertEqual((vault.compaction_staging_root / "tx" / "original.md").read_text(), "original")
        for name in ("processed.json", "agents.json", "host_ingest.json", "retrieval_gate.json", "compaction.json"):
            self.assertFalse((vault.index_path / name).exists())
        self.assertFalse((vault.index_path / ".compaction-staging").exists())
        self.assertTrue(vault.state_layout_path.is_file())

    def test_migration_crash_before_marker_is_idempotently_recovered(self) -> None:
        self._make_legacy()
        real_atomic = atomic_write_json

        def fail_marker(path, value, mode=0o600):
            if Path(path).name == "layout.json":
                raise OSError("simulated crash")
            return real_atomic(path, value, mode=mode)

        with mock.patch("memleaf.state_layout.atomic_write_json", side_effect=fail_marker):
            with self.assertRaises(OSError):
                Vault(self.root)
        self.assertTrue((self.root / "_state" / "processed.json").is_file())
        self.assertTrue((self.root / "_state" / "processed.json").is_file())
        vault = Vault(self.root)
        self.assertEqual(json.loads(vault.processed_state_path.read_text()), self._legacy_processed())
        self.assertFalse((vault.index_path / "processed.json").exists())

    def test_repeat_migration_is_idempotent(self) -> None:
        self._make_legacy()
        first = Vault(self.root)
        before = first.processed_state_path.read_bytes()
        second = Vault(self.root)
        self.assertEqual(before, second.processed_state_path.read_bytes())
        self.assertTrue(second.state_layout_path.exists())

    def test_migration_acquires_existing_legacy_locks(self) -> None:
        self._make_legacy()
        for name in ("vault.lock", "retrieval_gate.lock"):
            (self.root / "_index" / name).write_text("", encoding="utf-8")
        entered: list[str] = []

        class RecordingLock:
            def __init__(self, lock_path):
                self.path = Path(lock_path)

            def __enter__(self):
                entered.append(self.path.name)
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with mock.patch("memleaf.state_layout.VaultLock", RecordingLock):
            vault = Vault(self.root)
        self.assertEqual(entered, ["vault.lock", "retrieval_gate.lock"])
        self.assertFalse((vault.index_path / "vault.lock").exists())
        self.assertFalse((vault.index_path / "retrieval_gate.lock").exists())

    def test_pre_marker_old_and_new_conflict_fails_closed(self) -> None:
        self._make_legacy()
        state = self.root / "_state"
        state.mkdir(parents=True)
        different = self._legacy_processed()
        different["sessions"]["codex/s1"]["watermark"] = 99
        atomic_write_json(state / "processed.json", different)
        with self.assertRaisesRegex(StateLayoutError, "conflicting legacy and current"):
            Vault(self.root)

    def test_corrupt_old_state_fails_closed(self) -> None:
        self._make_legacy()
        (self.root / "_index" / "processed.json").write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(StateLayoutError, "invalid processed.json"):
            Vault(self.root)

    def test_completed_marker_makes_new_state_authoritative(self) -> None:
        self.root.mkdir(parents=True)
        (self.root / "_index").mkdir()
        (self.root / "_state").mkdir()
        new_value = self._legacy_processed()
        old_value = self._legacy_processed()
        old_value["sessions"]["codex/s1"]["watermark"] = 999
        atomic_write_json(self.root / "_state" / "processed.json", new_value)
        atomic_write_json(self.root / "_index" / "processed.json", old_value)
        atomic_write_json(self.root / "_state" / "layout.json", {"version": 1, "complete": True, "migrated": ["processed.json"]})
        vault = Vault(self.root)
        self.assertEqual(json.loads(vault.processed_state_path.read_text()), new_value)
        self.assertFalse((vault.index_path / "processed.json").exists())

    def test_fresh_install_uses_separate_index_and_state(self) -> None:
        vault = Vault(self.root)
        self.assertTrue(vault.index_path.is_dir())
        self.assertTrue(vault.state_path.is_dir())
        self.assertEqual({p.name for p in vault.index_path.iterdir() if p.is_file()}, {"tags.json", "native_sources.json"})
        self.assertTrue(vault.processed_state_path.is_file())
        self.assertTrue(vault.agents_state_path.is_file())
        self.assertTrue(vault.lock_path.parent == vault.state_path)

    def test_rebuild_index_never_rewrites_runtime_state(self) -> None:
        service = Memleaf(self.root)
        service.create_memory(memory_id="m1", title="One", body="body", tags=["tag"])
        service.capture("codex", "s1", "t1", "user", "remember me")
        service.session_lineage("codex", "child", parent_session_id="s1")
        processed = json.loads(service.vault.processed_state_path.read_text())
        processed["pending_turn_plans"] = {"p": {"status": "pending"}}
        processed["pending_operations"] = {"o": {"status": "prepared"}}
        atomic_write_json(service.vault.processed_state_path, processed)
        retrieval_id = begin_turn(service.vault, "codex", "s1", "t1")
        self.assertEqual(validate_turn(service.vault, retrieval_id)["status"], "NOT_SEARCHED")
        HostRuntime(service, "codex")._set_process_pending("s1", True)
        before = {
            path.name: path.read_bytes()
            for path in service.vault.state_path.iterdir()
            if path.is_file() and path.name not in {"vault.lock", "retrieval_gate.lock"}
        }
        service.rebuild_index()
        after = {
            path.name: path.read_bytes()
            for path in service.vault.state_path.iterdir()
            if path.is_file() and path.name not in {"vault.lock", "retrieval_gate.lock"}
        }
        self.assertEqual(before, after)

    def test_deleting_index_then_rebuild_preserves_state(self) -> None:
        service = Memleaf(self.root)
        service.create_memory(memory_id="m1", title="One", body="body", tags=["tag"])
        service.capture("hermes", "s", "t", "user", "x")
        before = service.vault.processed_state_path.read_bytes()
        shutil.rmtree(service.vault.index_path)
        result = service.rebuild_index()
        self.assertEqual(result["knowledge"], 1)
        self.assertEqual(before, service.vault.processed_state_path.read_bytes())
        self.assertTrue(service.vault.tags_index_path.is_file())
        self.assertTrue(service.vault.native_sources_index_path.is_file())


if __name__ == "__main__":
    unittest.main()
