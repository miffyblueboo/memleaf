from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from memleaf.adapters.base import mcp_command
from memleaf.adapters.codex import CodexAdapter


@unittest.skipUnless(
    os.environ.get("MEMLEAF_REAL_CODEX_ACCEPTANCE") == "1",
    "real Codex CLI acceptance is enabled only in the dedicated CI gate",
)
class RealCodexCliAcceptanceTests(unittest.TestCase):
    def test_official_codex_cli_registers_memleaf_idempotently_with_unicode_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-real-codex-") as temporary:
            root = Path(temporary)
            home = root / "用户 Home"
            home.mkdir()
            codex_home = home / ".codex"
            # CODEX_HOME is an existing application state directory in normal
            # Codex installations.  Keep the native acceptance isolated while
            # matching that real precondition.
            codex_home.mkdir()
            vault = root / "共享 Vault"

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["USERPROFILE"] = str(home)
            env["CODEX_HOME"] = str(codex_home)

            adapter = CodexAdapter(
                home=home,
                env=env,
                interpreter=sys.executable,
            )
            detection = adapter.detect()
            self.assertTrue(detection.detected)
            self.assertEqual("high", detection.confidence)
            self.assertIsNotNone(detection.executable)

            version = subprocess.run(
                [detection.executable, "--version"],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                check=False,
            )
            self.assertEqual(0, version.returncode, version.stderr)
            self.assertIn("0.151.0", version.stdout + version.stderr)

            first = adapter.configure(detection, vault, attempt=True)
            self.assertEqual("configured", first.status, first.reason)
            self.assertTrue(first.changed)

            queried = subprocess.run(
                [detection.executable, "mcp", "get", "memleaf", "--json"],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                check=False,
            )
            self.assertEqual(0, queried.returncode, queried.stderr)
            entry = json.loads(queried.stdout)
            transport = entry["transport"]
            expected = mcp_command(vault, interpreter=sys.executable)
            self.assertEqual(expected[0], transport["command"])
            self.assertEqual(expected[1:], transport["args"])

            hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
            self.assertIn("UserPromptSubmit", hooks["hooks"])
            self.assertIn("PreToolUse", hooks["hooks"])
            self.assertIn("Stop", hooks["hooks"])

            second = adapter.configure(adapter.detect(), vault, attempt=True)
            self.assertEqual("already_configured", second.status, second.reason)
            self.assertFalse(second.changed)


if __name__ == "__main__":
    unittest.main()
