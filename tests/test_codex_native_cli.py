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


class _RecordingSubprocessRunner:
    def __init__(self) -> None:
        self.successful_gets: list[dict] = []

    def __call__(self, argv, env=None):
        completed = subprocess.run(
            list(argv),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
        )
        command = list(argv)
        if (
            completed.returncode == 0
            and len(command) >= 5
            and command[1:4] == ["mcp", "get", "memleaf"]
        ):
            try:
                payload = json.loads(completed.stdout)
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                self.successful_gets.append(payload)
        return completed


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

            runner = _RecordingSubprocessRunner()
            adapter = CodexAdapter(
                home=home,
                env=env,
                runner=runner,
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
            if second.status != "already_configured":
                observed = (
                    runner.successful_gets[-1].get("transport")
                    if runner.successful_gets
                    else None
                )
                self.fail(
                    "second Codex configure was not idempotent: "
                    f"status={second.status!r} reason={second.reason!r} "
                    f"observed_transport={json.dumps(observed, ensure_ascii=True, sort_keys=True)} "
                    f"expected_command={expected[0]!r} expected_args={expected[1:]!r}"
                )
            self.assertFalse(second.changed)


if __name__ == "__main__":
    unittest.main()
