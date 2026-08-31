from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from memleaf.adapters.base import CommandResult, host_event_command, mcp_command, merge_hook_config
from memleaf.adapters.codex import CodexAdapter, _codex_hook_definition
from memleaf.installer import _select_codex_vault_path


class CodexRunner:
    def __init__(self, vault: Path, interpreter: Path):
        self.vault = vault
        self.interpreter = interpreter
        self.configured = False
        self.calls: list[list[str]] = []

    def __call__(self, argv, env=None):
        command = list(argv)
        self.calls.append(command)
        if "get" in command:
            if not self.configured:
                return CommandResult(1, stderr="server not found")
            expected = mcp_command(self.vault, interpreter=self.interpreter)
            return CommandResult(
                0,
                json.dumps(
                    {
                        "name": "memleaf",
                        "enabled": True,
                        "transport": {
                            "type": "stdio",
                            "command": expected[0],
                            "args": expected[1:],
                        },
                    }
                ),
            )
        if "add" in command:
            self.configured = True
        return CommandResult(0)


class ExistingVaultAdapter:
    def __init__(self, vault: Path | None):
        self.vault = vault.resolve() if vault is not None else None

    def configured_vault(self, detection):
        return self.vault


class CodexInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="memleaf-codex-install-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "用户 Home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.codex_name = "codex.exe" if os.name == "nt" else "codex"
        self.codex = self._executable(self.bin / self.codex_name)
        self.interpreter = self._executable(self.root / "Python Runtime" / "python")
        self.vault = self.root / "项目 Vault"

    @staticmethod
    def _executable(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return path

    def env(self, **extra):
        return {"HOME": str(self.home), "PATH": str(self.bin), **extra}

    def test_explicit_cli_path_wins_and_invalid_explicit_path_fails_closed(self):
        explicit = self._executable(self.root / "Codex App" / self.codex_name)
        adapter = CodexAdapter(home=self.home, env=self.env(CODEX_CLI_PATH=str(explicit)))
        self.assertEqual(str(explicit.resolve()), adapter.detect().executable)

        invalid = CodexAdapter(
            home=self.home,
            env=self.env(CODEX_CLI_PATH=str(self.root / "missing-codex")),
        ).detect()
        self.assertFalse(invalid.detected)
        self.assertEqual("diagnostic", invalid.status)

    def test_windows_path_detection_accepts_official_npm_command_launcher(self):
        npm_bin = self.root / "npm-bin"
        launcher = self._executable(npm_bin / "codex.cmd")
        adapter = CodexAdapter(
            home=self.home,
            env={"HOME": str(self.home), "PATH": str(npm_bin)},
            known_paths=(),
            platform="nt",
        )
        self.assertEqual(str(launcher.resolve()), adapter.detect().executable)

    def test_windows_known_runtime_and_hook_command_support_spaces_and_unicode(self):
        local = self.root / "Local App Data"
        runtime = self._executable(local / "Programs" / "Codex" / "codex.exe")
        adapter = CodexAdapter(
            home=self.home,
            env={"HOME": str(self.home), "PATH": "", "LOCALAPPDATA": str(local)},
            platform="nt",
        )
        self.assertEqual(str(runtime.resolve()), adapter.detect().executable)

        command = host_event_command(
            "codex",
            "Stop",
            self.vault,
            interpreter=self.interpreter,
            platform="nt",
        )
        self.assertNotIn('"', command)
        self.assertIn("powershell.exe", command)
        self.assertIn("-EncodedCommand", command)
        self.assertNotIn("项目 Vault", command)
        self.assertNotIn("Python Runtime", command)

    def test_custom_codex_home_keeps_config_and_hooks_together(self):
        codex_home = self.root / "自定义 Codex Home"
        codex_home.mkdir()
        adapter = CodexAdapter(
            home=self.home,
            env=self.env(CODEX_HOME=str(codex_home)),
            runner=CodexRunner(self.vault, self.interpreter),
            interpreter=self.interpreter,
        )
        detection = adapter.detect()
        self.assertTrue(os.path.samefile(Path(detection.config_path).parent, codex_home))
        result = adapter.configure(detection, self.vault)
        self.assertEqual("configured", result.status)
        self.assertTrue((codex_home / "hooks.json").is_file())
        self.assertFalse((self.home / ".codex" / "hooks.json").exists())

    @unittest.skipUnless(os.name == "nt", "requires native cmd.exe parsing")
    def test_windows_quote_free_hook_command_executes_with_space_and_unicode_paths(self):
        probe = self.root / "Python Runtime" / "python-probe.cmd"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(
            "@echo off\r\n"
            "echo %~7\r\n",
            encoding="utf-8",
        )
        command = host_event_command(
            "codex",
            "Stop",
            self.vault,
            interpreter=probe,
            platform="nt",
        )
        self.assertNotIn('"', command)
        completed = subprocess.run(
            ["cmd.exe", "/D", "/S", "/C", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(str(self.vault.resolve()), completed.stdout.strip())

    def test_hook_definition_has_compact_restore_and_platform_commands(self):
        hooks = _codex_hook_definition(self.vault, interpreter=self.interpreter)
        self.assertEqual("compact", hooks["SessionStart"][0]["matcher"])
        for groups in hooks.values():
            for group in groups:
                for hook in group["hooks"]:
                    self.assertIn("command", hook)
                    self.assertIn("commandWindows", hook)
                    self.assertIn(str(self.interpreter), hook["command"])
        self.assertEqual(
            r"^mcp__memleaf__(search|read)$",
            hooks["PreToolUse"][0]["matcher"],
        )

    def test_existing_memleaf_hook_gains_only_missing_windows_command(self):
        path = self.home / ".codex" / "hooks.json"
        path.parent.mkdir()
        definition = _codex_hook_definition(self.vault, interpreter=self.interpreter)
        old = json.loads(json.dumps(definition))
        for groups in old.values():
            for group in groups:
                for hook in group["hooks"]:
                    hook.pop("commandWindows")
        path.write_text(json.dumps({"hooks": old}), encoding="utf-8")

        result = merge_hook_config(path, definition, container_key="hooks")

        self.assertEqual("configured", result.status)
        current = json.loads(path.read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(definition, current)

    def test_configure_preserves_existing_config_and_hooks_and_is_idempotent(self):
        codex_home = self.home / ".codex"
        codex_home.mkdir()
        config = codex_home / "config.toml"
        config.write_text(
            'model = "deepseek-chat"\n'
            'model_provider = "custom"\n'
            'sandbox_mode = "workspace-write"\n'
            'approval_policy = "on-request"\n'
            '[profiles.work]\nmodel = "deepseek-reasoner"\n'
            '[mcp_servers.other]\ncommand = "other-mcp"\n',
            encoding="utf-8",
        )
        hooks_path = codex_home / "hooks.json"
        original_hook = {"type": "command", "command": "user-tool", "timeout": 10}
        hooks_path.write_text(
            json.dumps({"custom": {"kept": True}, "hooks": {"Stop": [{"hooks": [original_hook]}]}}),
            encoding="utf-8",
        )
        config_before = config.read_bytes()
        runner = CodexRunner(self.vault, self.interpreter)
        adapter = CodexAdapter(
            home=self.home,
            env=self.env(),
            runner=runner,
            interpreter=self.interpreter,
        )

        first = adapter.configure(adapter.detect(), self.vault)
        hooks_after_first = hooks_path.read_bytes()
        second = adapter.configure(adapter.detect(), self.vault)

        self.assertEqual("configured", first.status)
        self.assertEqual("already_configured", second.status)
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(config_before, config.read_bytes())
        self.assertEqual(hooks_after_first, hooks_path.read_bytes())
        document = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertEqual({"kept": True}, document["custom"])
        self.assertEqual(original_hook, document["hooks"]["Stop"][0]["hooks"][0])
        self.assertEqual(2, len(document["hooks"]["Stop"]))
        add_call = next(call for call in runner.calls if "add" in call)
        self.assertEqual(mcp_command(self.vault, interpreter=self.interpreter), add_call[5:])

    def test_inline_hooks_are_diagnostic_and_unchanged(self):
        codex_home = self.home / ".codex"
        codex_home.mkdir()
        config = codex_home / "config.toml"
        config.write_text('[hooks]\nenabled = true\nmodel = "keep"\n', encoding="utf-8")
        before = config.read_bytes()
        runner = CodexRunner(self.vault, self.interpreter)
        adapter = CodexAdapter(
            home=self.home,
            env=self.env(),
            runner=runner,
            interpreter=self.interpreter,
        )
        result = adapter.configure(adapter.detect(), self.vault)
        self.assertEqual("diagnostic", result.status)
        self.assertTrue(result.user_action_required)
        self.assertEqual([], runner.calls)
        self.assertEqual(before, config.read_bytes())
        self.assertFalse((codex_home / "hooks.json").exists())

    def test_multi_host_vault_selection_reuses_unique_path_and_rejects_conflict(self):
        hermes_home = self.root / "hermes"
        hermes_home.mkdir()
        hermes_vault = self.root / "shared-vault"
        (hermes_home / "memleaf.json").write_text(
            json.dumps({"vault": str(hermes_vault)}), encoding="utf-8"
        )
        selected, source = _select_codex_vault_path(
            home=self.home,
            hermes_home=hermes_home,
            adapter=ExistingVaultAdapter(hermes_vault),
            detection=object(),
            env={},
        )
        self.assertEqual(hermes_vault.resolve(), selected)
        self.assertEqual("hermes_config+codex_config", source)

        with self.assertRaisesRegex(RuntimeError, "vault_conflict"):
            _select_codex_vault_path(
                home=self.home,
                hermes_home=hermes_home,
                adapter=ExistingVaultAdapter(self.root / "other-vault"),
                detection=object(),
                env={},
            )


if __name__ == "__main__":
    unittest.main()
