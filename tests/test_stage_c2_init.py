from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from memleaf.adapters.antigravity import AntigravityAdapter
from memleaf.adapters.base import (
    CommandResult,
    mark_hook_active,
    mcp_command,
    run_argv,
    update_agents_index,
)
from memleaf.adapters.codex import CodexAdapter
from memleaf.adapters.hermes import HermesAdapter
from memleaf.vault import Vault


PYTHON = sys.executable


class FakeRunner:
    def __init__(self, *, vault: Path | None = None, configured: bool = False, fail_add: bool = False):
        self.vault = vault
        self.configured = configured
        self.fail_add = fail_add
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []

    def __call__(self, argv, env=None, input_text=None):
        command = list(argv)
        self.calls.append(command)
        self.inputs.append(input_text)
        if "get" in command:
            if self.configured:
                return CommandResult(
                    0,
                    json.dumps(
                        {
                            "name": "memleaf",
                            "command": mcp_command(self.vault)[0],
                            "args": mcp_command(self.vault)[1:],
                        }
                    ),
                )
            return CommandResult(1, stderr="server not found")
        if "list" in command:
            if self.configured:
                return CommandResult(
                    0,
                    f"memleaf: memleaf-mcp --vault {self.vault.resolve()}\n",
                )
            return CommandResult(0, "")
        if "test" in command:
            return CommandResult(0, "Connected (12ms)\nTools discovered: 11\n")
        if "add" in command:
            if self.fail_add:
                return CommandResult(1, stderr="failed")
            self.configured = True
        return CommandResult(0)


class StaticCodexGetRunner:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[list[str]] = []

    def __call__(self, argv, env=None):
        command = list(argv)
        self.calls.append(command)
        if "get" in command:
            return CommandResult(0, json.dumps(self.payload))
        return CommandResult(0)


class StageC2InitTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.vault = self.root / "vault"

    def tearDown(self):
        self.tempdir.cleanup()

    def make_executable(self, name: str, body: str = "#!/bin/sh\nexit 0\n") -> Path:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o700)
        return path

    def env(self):
        return {"HOME": str(self.home), "PATH": str(self.bin)}

    def test_detection_true_false_and_uncertain(self):
        self.make_executable("codex")
        detected = CodexAdapter(home=self.home, env=self.env()).detect()
        self.assertTrue(detected.detected)
        self.assertEqual("high", detected.confidence)

        absent = HermesAdapter(home=self.home, env=self.env()).detect()
        self.assertFalse(absent.detected)
        self.assertEqual("none", absent.confidence)

        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("# existing user config\n", encoding="utf-8")
        uncertain = CodexAdapter(home=self.home, env={"HOME": str(self.home), "PATH": ""}).detect()
        self.assertTrue(uncertain.detected)
        self.assertEqual("medium", uncertain.confidence)

    def test_codex_dry_run_has_no_runner_or_backup(self):
        self.make_executable("codex")
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("token = 'do-not-copy-to-result'\n", encoding="utf-8")
        runner = FakeRunner(vault=self.vault)
        adapter = CodexAdapter(home=self.home, env=self.env(), runner=runner)
        result = adapter.configure(adapter.detect(), self.vault, dry_run=True)
        self.assertEqual("would_configure", result.status)
        self.assertTrue(result.dry_run)
        self.assertFalse(result.changed)
        self.assertEqual("pending_user_review", result.hook_trust_status)
        self.assertTrue(result.user_action_required)
        self.assertIn("/hooks", result.user_action)
        self.assertNotIn("bypass", result.user_action.lower())
        self.assertEqual([], runner.calls)
        self.assertEqual(["mcp", "add", "memleaf", "--"], result.command[1:5])
        self.assertEqual([], list(config.parent.glob("*.memleaf.bak.*")))

    def test_codex_argv_backup_and_cli_failure(self):
        self.make_executable("codex")
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("unknown = true\n", encoding="utf-8")
        runner = FakeRunner(vault=self.vault)
        adapter = CodexAdapter(home=self.home, env=self.env(), runner=runner)
        result = adapter.configure(adapter.detect(), self.vault)
        self.assertEqual("configured", result.status)
        self.assertTrue(result.changed)
        self.assertEqual("pending_user_review", result.hook_trust_status)
        self.assertTrue(result.user_action_required)
        self.assertIn("/hooks", result.user_action)
        self.assertEqual(["mcp", "get", "memleaf", "--json"], runner.calls[0][1:])
        self.assertEqual(
            ["mcp", "add", "memleaf", "--", *mcp_command(self.vault)],
            runner.calls[1][1:],
        )
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(config.read_text(encoding="utf-8"), Path(result.backup_path).read_text(encoding="utf-8"))

        failing = FakeRunner(vault=self.vault, fail_add=True)
        failure = CodexAdapter(home=self.home, env=self.env(), runner=failing).configure(
            CodexAdapter(home=self.home, env=self.env(), runner=failing).detect(), self.vault
        )
        self.assertEqual("failure", failure.status)
        self.assertIsNone(failure.hook_trust_status)
        self.assertIsNone(failure.user_action_required)
        self.assertNotIn("user_action", failure.to_dict())
        self.assertIsNotNone(failure.backup_path)
        self.assertTrue(Path(failure.backup_path).exists())

    def test_codex_existing_conflict_and_repeat_are_safe(self):
        self.make_executable("codex")
        runner = FakeRunner(vault=self.vault)
        adapter = CodexAdapter(home=self.home, env=self.env(), runner=runner)
        first = adapter.configure(adapter.detect(), self.vault)
        second = adapter.configure(adapter.detect(), self.vault)
        self.assertEqual("configured", first.status)
        self.assertEqual("already_configured", second.status)
        for result in (first, second):
            self.assertEqual("pending_user_review", result.hook_trust_status)
            self.assertTrue(result.user_action_required)
            self.assertIn("/hooks", result.user_action)
        self.assertEqual(3, len(runner.calls))

        conflict_runner = FakeRunner(vault=self.root / "other", configured=True)
        conflict = CodexAdapter(home=self.home, env=self.env(), runner=conflict_runner).configure(
            CodexAdapter(home=self.home, env=self.env(), runner=conflict_runner).detect(), self.vault
        )
        self.assertEqual("diagnostic", conflict.status)
        self.assertFalse(conflict.changed)
        self.assertEqual(1, len(conflict_runner.calls))

    def test_codex_hook_activation_survives_init_and_rearms_on_definition_change(self):
        self.make_executable("codex")
        vault = Vault.initialize(self.vault)
        runner = FakeRunner(vault=self.vault)
        adapter = CodexAdapter(home=self.home, env=self.env(), runner=runner)

        first = adapter.configure(adapter.detect(), self.vault)
        self.assertEqual("pending_user_review", first.hook_activation_status)
        self.assertTrue(update_agents_index(vault.agents_index_path, {"codex": first.to_dict()}))
        self.assertTrue(mark_hook_active(self.vault, "codex"))

        active = adapter.configure(adapter.detect(), self.vault)
        self.assertEqual("active", active.hook_activation_status)
        self.assertEqual("trusted", active.hook_trust_status)
        self.assertFalse(active.user_action_required)
        self.assertIsNone(active.user_action)

        index = json.loads(vault.agents_index_path.read_text(encoding="utf-8"))
        index["agents"]["codex"]["hook_definition_hash"] = "changed-definition"
        vault.agents_index_path.write_text(json.dumps(index), encoding="utf-8")
        pending = adapter.configure(adapter.detect(), self.vault)
        self.assertEqual("pending_user_review", pending.hook_activation_status)
        self.assertTrue(pending.user_action_required)
        self.assertIn("/hooks", pending.user_action)

    def test_codex_current_nested_stdio_transport_is_already_configured(self):
        self.make_executable("codex")
        runner = StaticCodexGetRunner(
            {
                "name": "memleaf",
                "enabled": True,
                "transport": {
                    "type": "stdio",
                    "command": mcp_command(self.vault)[0],
                    "args": mcp_command(self.vault)[1:],
                    "env": None,
                },
            }
        )
        adapter = CodexAdapter(home=self.home, env=self.env(), runner=runner)

        first = adapter.configure(adapter.detect(), self.vault)
        second = adapter.configure(adapter.detect(), self.vault)

        self.assertEqual("already_configured", first.status)
        self.assertEqual("already_configured", second.status)
        self.assertEqual(2, len(runner.calls))
        self.assertTrue(all("get" in command for command in runner.calls))
        self.assertFalse(any("add" in command for command in runner.calls))

    def test_codex_nested_transport_requires_stdio_and_exact_command_args(self):
        self.make_executable("codex")
        expected = mcp_command(self.vault)
        valid_args = expected[1:]
        invalid_transports = (
            {"command": expected[0], "args": valid_args},
            {"type": "http", "command": expected[0], "args": valid_args},
            {"type": "stdio", "command": "other", "args": valid_args},
            {"type": "stdio", "command": expected[0], "args": valid_args + ["extra"]},
        )
        for transport in invalid_transports:
            with self.subTest(transport=transport):
                runner = StaticCodexGetRunner(
                    {"name": "memleaf", "enabled": True, "transport": transport}
                )
                adapter = CodexAdapter(home=self.home, env=self.env(), runner=runner)
                result = adapter.configure(adapter.detect(), self.vault)
                self.assertEqual("diagnostic", result.status)
                self.assertFalse(result.changed)
                self.assertEqual(1, len(runner.calls))
                self.assertNotIn("add", runner.calls[0])

    def test_runner_internal_type_error_is_not_retried(self):
        calls = []

        def runner(argv, env=None):
            calls.append(list(argv))
            raise TypeError("runner body failure")

        with self.assertRaises(TypeError):
            run_argv(runner, ["fake", "mcp"], env=self.env())
        self.assertEqual(1, len(calls))

    def test_hermes_argv_backup_existing_and_conflict(self):
        self.make_executable("hermes")
        config = self.home / ".hermes" / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("other: keep\nmcp_servers:\n  other:\n    command: other\n", encoding="utf-8")
        runner = FakeRunner(vault=self.vault)
        adapter = HermesAdapter(home=self.home, env=self.env(), runner=runner)
        result = adapter.configure(adapter.detect(), self.vault)
        self.assertEqual("configured", result.status)
        self.assertEqual(
            [
                "mcp", "add", "memleaf", "--command", "memleaf-mcp",
                "--args", "--vault", str(self.vault.resolve())
            ],
            runner.calls[1][1:],
        )
        self.assertEqual([None, "\n", None], runner.inputs)
        self.assertIsNotNone(result.backup_path)
        self.assertTrue(Path(result.backup_path).exists())

        config.write_text(
            "mcp_servers:\n  memleaf:\n    command: other\n    args: [--vault, other]\n",
            encoding="utf-8",
        )
        conflict_runner = FakeRunner(vault=self.vault)
        conflict = HermesAdapter(home=self.home, env=self.env(), runner=conflict_runner).configure(
            HermesAdapter(home=self.home, env=self.env(), runner=conflict_runner).detect(), self.vault
        )
        self.assertEqual("diagnostic", conflict.status)
        self.assertFalse(conflict.changed)

    def test_hermes_zero_add_exit_without_persisted_entry_is_failure(self):
        self.make_executable("hermes")
        config = self.home / ".hermes" / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            "mcp_servers:\n  other:\n    command: other\n",
            encoding="utf-8",
        )

        class NoopAddRunner(FakeRunner):
            def __call__(self, argv, env=None, input_text=None):
                command = list(argv)
                if "add" in command:
                    self.calls.append(command)
                    self.inputs.append(input_text)
                    return CommandResult(0)
                return super().__call__(command, env=env, input_text=input_text)

        runner = NoopAddRunner(vault=self.vault)
        adapter = HermesAdapter(home=self.home, env=self.env(), runner=runner)
        result = adapter.configure(adapter.detect(), self.vault)

        self.assertEqual("failure", result.status)
        self.assertFalse(result.changed)
        self.assertEqual("\n", runner.inputs[1])
        self.assertNotIn("memleaf-mcp", config.read_text(encoding="utf-8"))

    def test_hermes_correct_existing_is_noop(self):
        self.make_executable("hermes")
        config = self.home / ".hermes" / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            "mcp_servers:\n  memleaf:\n    command: memleaf-mcp\n    args:\n      - --vault\n      - "
            + str(self.vault.resolve())
            + "\n",
            encoding="utf-8",
        )
        runner = FakeRunner(vault=self.vault)
        adapter = HermesAdapter(home=self.home, env=self.env(), runner=runner)
        result = adapter.configure(adapter.detect(), self.vault)
        self.assertEqual("already_configured", result.status)
        self.assertFalse(result.changed)
        self.assertEqual(1, len(runner.calls))

    def test_hermes_mcp_lifecycle_and_test_use_official_commands(self):
        self.make_executable("hermes")
        runner = FakeRunner(vault=self.vault)
        adapter = HermesAdapter(home=self.home, env=self.env(), runner=runner)
        detection = adapter.detect()

        self.assertTrue(adapter.configure_mcp_lifecycle(detection))
        self.assertTrue(adapter.test_mcp(detection))
        self.assertEqual(
            [
                "config",
                "set",
                "mcp_servers.memleaf.lazy",
                "true",
            ],
            runner.calls[-3][1:],
        )
        self.assertEqual(
            [
                "config",
                "set",
                "mcp_servers.memleaf.idle_timeout_seconds",
                "60",
            ],
            runner.calls[-2][1:],
        )
        self.assertEqual(["mcp", "test", "memleaf"], runner.calls[-1][1:])

    def test_hermes_stale_entry_migration_rejects_unknown_command_and_vault(self):
        self.make_executable("hermes")
        config = self.home / ".hermes" / "config.yaml"
        config.parent.mkdir(parents=True)
        expected_vault = str(self.vault.resolve())
        cases = (
            "evil-command",
            "memleaf-mcp",
        )
        for command in cases:
            with self.subTest(command=command):
                other_vault = expected_vault if command == "evil-command" else str(
                    (self.root / "other-vault").resolve()
                )
                config.write_text(
                    "mcp_servers:\n  memleaf:\n"
                    f"    command: {command}\n"
                    "    args:\n      - --vault\n"
                    f"      - {other_vault}\n",
                    encoding="utf-8",
                )
                original = config.read_bytes()
                runner = FakeRunner(vault=self.vault)
                adapter = HermesAdapter(
                    home=self.home,
                    env=self.env(),
                    runner=runner,
                    memleaf_command=str(self.root / "memleaf" / "bin" / "memleaf-mcp"),
                )
                result = adapter.configure(adapter.detect(), self.vault)
                self.assertEqual("diagnostic", result.status)
                self.assertEqual(1, len(runner.calls))
                self.assertEqual(original, config.read_bytes())

    def test_hermes_stale_entry_migration_accepts_enabled_true_only(self):
        self.make_executable("hermes")
        config = self.home / ".hermes" / "config.yaml"
        config.parent.mkdir(parents=True)
        vault = str(self.vault.resolve())
        target = str((self.root / "memleaf" / "bin" / "memleaf-mcp").resolve())

        class MigratingRunner(FakeRunner):
            def __call__(self, argv, env=None, input_text=None):
                result = super().__call__(argv, env=env, input_text=input_text)
                command = list(argv)
                if "add" in command and result.returncode == 0:
                    config.write_text(
                        "mcp_servers:\n  memleaf:\n"
                        f"    command: {command[5]}\n"
                        "    args:\n      - --vault\n"
                        f"      - {vault}\n",
                        encoding="utf-8",
                    )
                return result

        for enabled in (True, "true", "1", "yes", "on"):
            with self.subTest(enabled=enabled):
                config.write_text(
                    "mcp_servers:\n  memleaf:\n"
                    "    command: memleaf-mcp\n"
                    "    args:\n      - --vault\n"
                    f"      - {vault}\n"
                    f"    enabled: {str(enabled).lower()}\n",
                    encoding="utf-8",
                )
                runner = MigratingRunner(vault=self.vault)
                adapter = HermesAdapter(
                    home=self.home,
                    env=self.env(),
                    runner=runner,
                    memleaf_command=target,
                )
                result = adapter.configure(adapter.detect(), self.vault)
                self.assertEqual("configured", result.status)
                self.assertEqual("y\n\n", runner.inputs[1])

        for extra in (
            "    enabled: false\n",
            "    tools:\n      exclude: [forget_about]\n",
        ):
            with self.subTest(extra=extra):
                config.write_text(
                    "mcp_servers:\n  memleaf:\n"
                    "    command: memleaf-mcp\n"
                    "    args:\n      - --vault\n"
                    f"      - {vault}\n"
                    + extra,
                    encoding="utf-8",
                )
                original = config.read_bytes()
                runner = FakeRunner(vault=self.vault)
                adapter = HermesAdapter(
                    home=self.home,
                    env=self.env(),
                    runner=runner,
                    memleaf_command=target,
                )
                result = adapter.configure(adapter.detect(), self.vault)
                self.assertEqual("diagnostic", result.status)
                self.assertEqual(1, len(runner.calls))
                self.assertEqual(original, config.read_bytes())

    def test_antigravity_merge_backup_permissions_and_unknown_keys(self):
        config = self.home / ".gemini" / "config" / "mcp_config.json"
        config.parent.mkdir(parents=True)
        original = {"other": {"secret": "kept"}, "mcpServers": {"other": {"x": 1}}}
        config.write_text(
            json.dumps(original),
            encoding="utf-8",
        )
        config.chmod(0o640)
        adapter = AntigravityAdapter(home=self.home, env=self.env())
        result = adapter.configure(adapter.detect(), self.vault)
        self.assertEqual("configured", result.status)
        self.assertTrue(result.changed)
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(0o640, stat.S_IMODE(config.stat().st_mode))
        data = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual({"secret": "kept"}, data["other"])
        self.assertEqual({"x": 1}, data["mcpServers"]["other"])
        self.assertEqual(
            {"command": "memleaf-mcp", "args": ["--vault", str(self.vault.resolve())]},
            data["mcpServers"]["memleaf"],
        )
        self.assertEqual(original, json.loads(Path(result.backup_path).read_text(encoding="utf-8")))

        before = config.read_text(encoding="utf-8")
        repeat = adapter.configure(adapter.detect(), self.vault)
        self.assertEqual("already_configured", repeat.status)
        self.assertEqual(before, config.read_text(encoding="utf-8"))

    def test_antigravity_hook_activation_survives_idempotent_init(self):
        config = self.home / ".gemini" / "config" / "mcp_config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        vault = Vault.initialize(self.vault)
        adapter = AntigravityAdapter(home=self.home, env=self.env())

        first = adapter.configure(adapter.detect(), self.vault)
        self.assertEqual("pending_restart", first.hook_activation_status)
        self.assertTrue(first.user_action_required)
        self.assertIn("quit and reopen", first.user_action)
        self.assertTrue(update_agents_index(vault.agents_index_path, {"antigravity": first.to_dict()}))
        self.assertTrue(mark_hook_active(self.vault, "antigravity"))

        active = adapter.configure(adapter.detect(), self.vault)
        self.assertEqual("active", active.hook_activation_status)
        self.assertFalse(active.user_action_required)
        self.assertIsNone(active.user_action)

        index = json.loads(vault.agents_index_path.read_text(encoding="utf-8"))
        index["agents"]["antigravity"]["hook_definition_hash"] = "changed-definition"
        vault.agents_index_path.write_text(json.dumps(index), encoding="utf-8")
        pending = adapter.configure(adapter.detect(), self.vault)
        self.assertEqual("pending_restart", pending.hook_activation_status)
        self.assertTrue(pending.user_action_required)

    def test_antigravity_dry_run_invalid_conflict_and_symlink(self):
        config = self.home / ".gemini" / "config" / "mcp_config.json"
        config.parent.mkdir(parents=True)
        config.write_text("{not-json", encoding="utf-8")
        adapter = AntigravityAdapter(home=self.home, env=self.env())
        invalid = adapter.detect()
        self.assertFalse(invalid.detected)
        self.assertEqual("diagnostic", adapter.configure(invalid, self.vault, attempt=True).status)

        config.write_text(json.dumps({"mcpServers": {"memleaf": {"command": "other"}}}), encoding="utf-8")
        conflict = adapter.configure(adapter.detect(), self.vault)
        self.assertEqual("diagnostic", conflict.status)
        self.assertFalse(conflict.changed)

        target = self.root / "target.json"
        target.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        config.unlink()
        config.symlink_to(target)
        symlink_detection = adapter.detect()
        self.assertFalse(symlink_detection.detected)
        symlink_result = adapter.configure(symlink_detection, self.vault, attempt=True)
        self.assertEqual("diagnostic", symlink_result.status)
        self.assertTrue(config.is_symlink())

    def test_vault_fresh_agents_index(self):
        vault = Vault.initialize(self.vault)
        self.assertTrue(vault.agents_index_path.exists())
        self.assertEqual({"version": 1, "agents": {}}, json.loads(vault.agents_index_path.read_text(encoding="utf-8")))

    def run_cli(self, *arguments, home: Path | None = None):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        environment["HOME"] = str(home or self.home)
        environment["PATH"] = str(self.bin)
        return subprocess.run(
            [PYTHON, "-m", "memleaf.cli", *arguments],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cli_dry_run_and_disable_flags_have_no_vault_side_effect(self):
        result = self.run_cli(
            "init", "--vault", str(self.vault), "--all", "--no-codex", "--dry-run", "--json"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output["dry_run"])
        self.assertFalse(output["agents_index_written"])
        self.assertEqual("disabled", output["agents"]["codex"]["status"])
        self.assertEqual("diagnostic", output["agents"]["hermes"]["status"])
        self.assertFalse(self.vault.exists())

    def test_cli_fresh_noninteractive_init_reports_missing_model(self):
        help_result = self.run_cli("--help")
        self.assertEqual(0, help_result.returncode)
        self.assertIn("init", help_result.stdout)

        result = self.run_cli("init", "--vault", str(self.vault), "--json")
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertEqual(1, len(result.stdout.strip().splitlines()))
        output = json.loads(result.stdout)
        self.assertEqual("failure", output["model"]["status"])
        self.assertTrue(self.vault.exists())
        self.assertTrue(output["agents_index_written"])
        index = json.loads((self.vault / "_index" / "agents.json").read_text(encoding="utf-8"))
        self.assertEqual(output["agents"], index["agents"])

    def test_cli_subprocess_configures_only_hermes_with_codex_present(self):
        self.make_executable(
            "codex",
            "#!/bin/sh\nif [ \"$2\" = \"get\" ]; then echo 'server not found' >&2; exit 1; fi\nexit 0\n",
        )
        self.make_executable(
            "hermes",
            "#!/bin/sh\n"
            "if [ \"$1\" = \"config\" ] && [ \"$2\" = \"env-path\" ]; then echo \"$HOME/.hermes/.env\"; exit 0; fi\n"
            "if [ \"$1\" = \"config\" ] && [ \"$2\" = \"get\" ] && [ \"$3\" = \"model\" ]; then echo '{\"default\":\"test-flash\",\"provider\":\"openai\",\"base_url\":\"https://example.test/v1\"}'; exit 0; fi\n"
            "if [ \"$1\" = \"config\" ] && [ \"$2\" = \"get\" ] && [ \"$3\" = \"custom_providers\" ]; then echo '{}'; exit 0; fi\n"
            "if [ \"$2\" = \"list\" ]; then exit 0; fi\n"
            "if [ \"$2\" = \"add\" ]; then\n"
            "  /bin/mkdir -p \"$HOME/.hermes\"\n"
            "  printf 'mcp_servers:\\n  memleaf:\\n    command: memleaf-mcp\\n    args:\\n      - --vault\\n      - %s\\n' \"$8\" > \"$HOME/.hermes/config.yaml\"\n"
            "fi\n"
            "exit 0\n",
        )
        hermes_home = self.home / ".hermes"
        hermes_home.mkdir()
        (hermes_home / ".env").write_text("OPENAI_API_KEY=stage-c2-secret\n", encoding="utf-8")
        antigravity = self.home / ".gemini" / "config" / "mcp_config.json"
        antigravity.parent.mkdir(parents=True)
        antigravity.write_text(
            json.dumps({"mcpServers": {"dbx": {"command": "dbx"}}}),
            encoding="utf-8",
        )
        antigravity_before = antigravity.read_bytes()
        result = self.run_cli("init", "--vault", str(self.vault), "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual("disabled", output["agents"]["codex"]["status"])
        self.assertEqual("disabled", output["agents"]["codex"]["hook_trust_status"])
        self.assertFalse(output["agents"]["codex"]["user_action_required"])
        self.assertFalse((self.home / ".codex").exists())
        self.assertEqual("configured", output["agents"]["hermes"]["status"])
        self.assertEqual("disabled", output["agents"]["antigravity"]["status"])
        self.assertEqual("disabled", output["agents"]["antigravity"]["hook_activation_status"])
        self.assertFalse(output["agents"]["antigravity"]["detected"])
        self.assertFalse(output["agents"]["antigravity"]["user_action_required"])
        self.assertEqual(antigravity_before, antigravity.read_bytes())
        self.assertFalse((self.home / ".gemini" / "config" / "hooks.json").exists())
        self.assertEqual("configured", output["model"]["status"])
        self.assertEqual("test-flash", output["model"]["selected"]["model"])
        self.assertNotIn("stage-c2-secret", result.stdout + result.stderr)

    def test_cli_all_keeps_antigravity_configuration_untouched(self):
        antigravity = self.home / ".gemini" / "config" / "mcp_config.json"
        antigravity.parent.mkdir(parents=True)
        antigravity.write_text(
            json.dumps({"mcpServers": {"dbx": {"command": "dbx"}}}),
            encoding="utf-8",
        )
        antigravity_before = antigravity.read_bytes()
        vault = Vault.initialize(self.vault)
        vault.agents_index_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "agents": {
                        "antigravity": {
                            "status": "configured",
                            "hook_activation_status": "active",
                            "hook_definition_hash": "old-definition",
                            "user_action_required": True,
                            "user_action": "reopen Antigravity",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        result = self.run_cli(
            "init",
            "--vault",
            str(self.vault),
            "--all",
            "--no-codex",
            "--no-hermes",
            "--no-model-discovery",
            "--json",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual("disabled", output["agents"]["antigravity"]["status"])
        self.assertEqual("disabled", output["agents"]["antigravity"]["hook_activation_status"])
        self.assertIn("no detection or configuration performed", output["agents"]["antigravity"]["reason"])
        self.assertEqual(antigravity_before, antigravity.read_bytes())
        self.assertFalse((self.home / ".gemini" / "config" / "hooks.json").exists())
        indexed = json.loads((self.vault / "_index" / "agents.json").read_text(encoding="utf-8"))
        self.assertEqual("disabled", indexed["agents"]["antigravity"]["status"])
        self.assertEqual("disabled", indexed["agents"]["antigravity"]["hook_activation_status"])
        self.assertEqual("", indexed["agents"]["antigravity"]["hook_definition_hash"])
        self.assertFalse(indexed["agents"]["antigravity"]["user_action_required"])
        self.assertEqual("", indexed["agents"]["antigravity"]["user_action"])

    def test_cli_all_ignores_codex_config_models_and_executable(self):
        self.make_executable("codex", '#!/bin/sh\necho called > "$HOME/codex-called"\nexit 1\n')
        codex_home = self.home / ".codex"
        codex_home.mkdir()
        config = codex_home / "config.toml"
        config.write_text(
            'model = "test-mini"\nmodel_provider = "custom"\n'
            '[model_providers.custom]\nname = "custom"\n'
            'base_url = "https://example.test/v1"\n'
            'wire_api = "chat"\nexperimental_bearer_token = "synthetic-token"\n',
            encoding="utf-8",
        )
        hooks = codex_home / "hooks.json"
        hooks.write_text('{"hooks":{"Stop":[]}}', encoding="utf-8")
        before = {path: path.read_bytes() for path in (config, hooks)}

        result = self.run_cli("init", "--vault", str(self.vault), "--all", "--json")

        self.assertEqual(2, result.returncode, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual("disabled", output["agents"]["codex"]["status"])
        self.assertFalse(output["agents"]["codex"]["detected"])
        self.assertEqual("failure", output["model"]["status"])
        self.assertFalse((self.home / "codex-called").exists())
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual({"config.toml", "hooks.json"}, {path.name for path in codex_home.iterdir()})

    def test_cli_failure_returns_nonzero_after_printing_complete_json(self):
        self.make_executable(
            "codex",
            "#!/bin/sh\nif [ \"$2\" = \"get\" ]; then echo 'server not found' >&2; exit 1; fi\nexit 1\n",
        )
        result = self.run_cli(
            "init",
            "--vault",
            str(self.vault),
            "--no-hermes",
            "--no-antigravity",
            "--json",
        )
        self.assertEqual(2, result.returncode)
        self.assertEqual(1, len(result.stdout.strip().splitlines()))
        output = json.loads(result.stdout)
        self.assertEqual("disabled", output["agents"]["codex"]["status"])
        self.assertEqual("failure", output["model"]["status"])
        self.assertEqual("disabled", output["agents"]["hermes"]["status"])

        human = self.run_cli(
            "init",
            "--vault",
            str(self.root / "human-vault"),
            "--no-hermes",
            "--no-antigravity",
        )
        self.assertEqual(2, human.returncode)
        self.assertIn("model: failure", human.stdout)

    def test_cli_antigravity_stop_invalid_payload_returns_required_decision(self):
        result = subprocess.run(
            [PYTHON, "-m", "memleaf.cli", "host-event", "antigravity", "Stop", "--vault", str(self.vault)],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            input="{not-json",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual('{"decision":"stop"}', result.stdout.strip())
        self.assertEqual("", result.stderr)


if __name__ == "__main__":
    unittest.main()
