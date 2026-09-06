from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one occurrence, found {count}: {old!r}")
    write(path, text.replace(old, new, 1))


# Inspection snapshots must include runtime state but remain read-only.
path = "src/memleaf/inspection.py"
text = read(path)
text = text.replace(
    '_AREAS = frozenset({"knowledge", "history", "inbox", "_index"})',
    '_AREAS = frozenset({"knowledge", "history", "inbox", "_index", "_state"})',
)
text = text.replace('"_index/processed.json"', '"_state/processed.json"')
if '"_index/processed.json"' in text:
    raise SystemExit("inspection still reads processed state from _index")
write(path, text)

# Remove internal index terminology for host activation state.
for root_name in ("src", "tests"):
    for target in (ROOT / root_name).rglob("*.py"):
        text = target.read_text(encoding="utf-8")
        updated = text.replace("update_agents_index", "update_agents_state")
        updated = updated.replace("agent_index_path", "agent_state_path")
        updated = updated.replace("agents_index_path", "agents_state_path")
        updated = updated.replace("agents_index_written", "agents_state_written")
        if updated != text:
            target.write_text(updated, encoding="utf-8")

path = "src/memleaf/cli.py"
text = read(path).replace("Clear stale activation claims in our own index only.", "Clear stale activation claims in our own state only.")
write(path, text)

# Internal callers now have explicit state names, so remove misleading aliases.
path = "src/memleaf/vault.py"
text = read(path)
text, count_processed = re.subn(
    r'\n    @property\n    def processed_index_path\(self\) -> Path:\n        """Deprecated compatibility alias; runtime state moved in v0\.2\.28\."""\n        return self\.processed_state_path\n',
    "",
    text,
    count=1,
)
text, count_agents = re.subn(
    r'\n    @property\n    def agents_index_path\(self\) -> Path:\n        """Deprecated compatibility alias; host activation is runtime state\."""\n        return self\.agents_state_path\n',
    "",
    text,
    count=1,
)
if count_processed != 1 or count_agents != 1:
    raise SystemExit(f"vault compatibility alias removal failed: {count_processed}/{count_agents}")
write(path, text)

# The state migration must serialize against v0.2.27 lock files while copying.
path = "src/memleaf/state_layout.py"
text = read(path)
text = text.replace("import hashlib\n", "import hashlib\nfrom contextlib import ExitStack\n", 1)
text = text.replace(
    "from .locking import atomic_unlink, atomic_write_bytes, atomic_write_json, read_json",
    "from .locking import VaultLock, atomic_unlink, atomic_write_bytes, atomic_write_json, read_json",
    1,
)
old_cleanup = '''    for name in _LEGACY_LOCK_FILES:\n        path = index_root / name\n        if path.is_symlink():\n            raise StateLayoutError(f"unsafe legacy {name} path")\n        if path.exists():\n            try:\n                atomic_unlink(path)\n            except OSError as error:\n                raise StateLayoutError(\n                    "legacy memleaf process still owns the old vault lock; stop old processes and retry"\n                ) from error\n'''
if old_cleanup not in text:
    raise SystemExit("state_layout legacy lock cleanup pattern changed")
text = text.replace(old_cleanup, "", 1)
text = text.replace("def migrate_state_layout(vault: Any) -> dict[str, Any]:", "def _migrate_state_layout_locked(vault: Any) -> dict[str, Any]:", 1)
wrapper = r'''


def migrate_state_layout(vault: Any) -> dict[str, Any]:
    """Migrate while also honoring lock files used by pre-v0.2.28 writers.

    The new Vault lock is already held by the caller. Existing legacy locks
    are acquired in a fixed order before any legacy state is copied. This
    prevents an already-running old worker from mutating Markdown/state during
    migration. Starting an old binary after upgrade is unsupported; callers
    should stop older host processes before upgrading a live Vault.
    """

    index_root = Path(vault.index_path)
    legacy_locks: list[Path] = []
    for name in _LEGACY_LOCK_FILES:
        path = index_root / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise StateLayoutError(f"unsafe legacy {name} path")
        if path.exists():
            legacy_locks.append(path)

    with ExitStack() as stack:
        for path in legacy_locks:
            stack.enter_context(VaultLock(path))
        result = _migrate_state_layout_locked(vault)

    # Remove obsolete lock files only after their handles are released; this
    # matters on Windows. State files were already committed and verified.
    for path in legacy_locks:
        if path.is_symlink():
            raise StateLayoutError("unsafe legacy lock path")
        if path.exists():
            try:
                atomic_unlink(path)
            except OSError as error:
                raise StateLayoutError(
                    "cannot retire legacy Vault lock; stop old processes and retry"
                ) from error
    return result
'''
text += wrapper
write(path, text)

# Keep the config migration document aligned with the actual API cleanup.
path = "docs/config-migrations.md"
text = read(path)
text = text.replace(
    "- `Vault.processed_index_path` and `Vault.agents_index_path` remain narrow Python compatibility aliases for v0.2.28, but both resolve to `_state/`; internal code uses the new state names. They are candidates for removal in a future major cleanup.\n",
    "- The old internal `processed_index_path` / `agents_index_path` names are removed in v0.2.28; runtime code uses explicit `_state/` properties.\n",
)
text = text.replace(
    "On first v0.2.28 Vault use, runtime correctness state is migrated from `_index/` to `_state/` by one centralized layout owner.",
    "On first v0.2.28 Vault use, runtime correctness state is migrated from `_index/` to `_state/` by one centralized layout owner. Existing v0.2.27 Vault/retrieval lock files are acquired during migration so an already-running old worker cannot mutate the copied state concurrently.",
)
write(path, text)

# Tests for legacy lock handoff and inspection state location.
path = "tests/test_state_layout_v028.py"
text = read(path)
needle = '''    def test_repeat_migration_is_idempotent(self) -> None:\n        self._make_legacy()\n        first = Vault(self.root)\n        before = first.processed_state_path.read_bytes()\n        second = Vault(self.root)\n        self.assertEqual(before, second.processed_state_path.read_bytes())\n        self.assertTrue(second.state_layout_path.exists())\n\n'''
addition = '''    def test_migration_acquires_existing_legacy_locks(self) -> None:\n        self._make_legacy()\n        for name in ("vault.lock", "retrieval_gate.lock"):\n            (self.root / "_index" / name).write_text("", encoding="utf-8")\n        entered: list[str] = []\n\n        class RecordingLock:\n            def __init__(self, lock_path):\n                self.path = Path(lock_path)\n\n            def __enter__(self):\n                entered.append(self.path.name)\n                return self\n\n            def __exit__(self, exc_type, exc, tb):\n                return False\n\n        with mock.patch("memleaf.state_layout.VaultLock", RecordingLock):\n            vault = Vault(self.root)\n        self.assertEqual(entered, ["vault.lock", "retrieval_gate.lock"])\n        self.assertFalse((vault.index_path / "vault.lock").exists())\n        self.assertFalse((vault.index_path / "retrieval_gate.lock").exists())\n\n'''
if needle not in text:
    raise SystemExit("state layout insertion point changed")
text = text.replace(needle, needle + addition, 1)
write(path, text)

inspection_test = r'''from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memleaf.inspection import audit_vault
from memleaf.vault import Vault
from memleaf.locking import atomic_write_json


class InspectionStateLayoutV028Tests(unittest.TestCase):
    def test_audit_reads_pending_state_from_state_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-inspection-state-") as temporary:
            vault = Vault(Path(temporary) / "vault")
            processed = json.loads(vault.processed_state_path.read_text(encoding="utf-8"))
            processed["pending_operations"] = {"op-1": {"status": "prepared"}}
            atomic_write_json(vault.processed_state_path, processed)
            result = audit_vault(vault.root)
            self.assertTrue(any(item.get("kind") == "pending_operations" for item in result["issues"]))

    def test_snapshot_includes_state_but_never_lock_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-inspection-state-") as temporary:
            vault = Vault(Path(temporary) / "vault")
            from memleaf.inspection import _snapshot
            snapshot = _snapshot(vault.root)
            self.assertIn("_state/processed.json", snapshot)
            self.assertNotIn("_state/vault.lock", snapshot)


if __name__ == "__main__":
    unittest.main()
'''
write("tests/test_inspection_state_v028.py", inspection_test)

# Ensure old internal state names no longer survive outside intentional migration docs/tests.
for target in (ROOT / "src" / "memleaf").rglob("*.py"):
    text = target.read_text(encoding="utf-8")
    if target.name != "config.py" and any(name in text for name in ("processed_index_path", "agents_index_path")):
        raise SystemExit(f"old state API name remains: {target}")

print("v0.2.28 phase-2 cleanup patch applied")
