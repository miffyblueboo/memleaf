from __future__ import annotations

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
