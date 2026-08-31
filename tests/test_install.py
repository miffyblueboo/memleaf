from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from memleaf.config import load_config


ROOT = Path(__file__).resolve().parents[1]


class InstallScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="memleaf-install space-")
        self.home = Path(self.tempdir.name) / "home"
        self.home.mkdir()
        self.install_root = self.home / "memleaf"
        shutil.copytree(
            ROOT,
            self.install_root,
            ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc", "*.egg-info"),
        )
        self.bin = self.home / "bin"
        self.bin.mkdir()
        (self.bin / "python3.11").symlink_to(Path(sys.executable).resolve())

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def env(self) -> dict[str, str]:
        environment = os.environ.copy()
        for name in ("MEMLEAF_PYTHON", "MEMLEAF_INSTALL_ROOT", "PYTHONPATH", "CODEX_HOME", "MEMLEAF_VAULT"):
            environment.pop(name, None)
        environment.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "HERMES_HOME": str(self.home / ".hermes"),
                "PIP_NO_INDEX": "1",
                "PYTHONNOUSERSITE": "1",
            }
        )
        return environment

    def run_install(
        self,
        *,
        hermes: bool = False,
        codex: bool = False,
        antigravity: bool = False,
        hermes_status: str = "ok",
        hermes_mcp_status: str = "ok",
    ) -> subprocess.CompletedProcess[str]:
        if hermes:
            hermes_home = self.home / ".hermes"
            hermes_home.mkdir(exist_ok=True)
            (hermes_home / ".env").write_text("DEEPSEEK_API_KEY=install-secret\n", encoding="utf-8")
            hermes = self.bin / "hermes"
            hermes.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = config ] && [ \"$2\" = env-path ]; then\n"
                "  printf '%s\\n' \"$HOME/.hermes/.env\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = config ] && [ \"$2\" = get ] && [ \"$3\" = model ]; then\n"
                "  printf '%s\\n' '{\"default\":\"deepseek-flash\",\"provider\":\"deepseek\",\"base_url\":\"https://api.deepseek.com/v1\"}'\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = config ] && [ \"$2\" = get ] && [ \"$3\" = custom_providers ]; then\n"
                "  printf '%s\\n' '{}'\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = mcp ] && [ \"$2\" = list ]; then\n"
                "  if [ -f \"$HOME/hermes-mcp-configured\" ]; then\n"
                "    printf 'memleaf: %s --vault %s\\n' \"$(sed -n '1p' \"$HOME/hermes-mcp-command\")\" \"$HOME/.memleaf\"\n"
                "  fi\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = mcp ] && [ \"$2\" = add ]; then\n"
                "  mkdir -p \"$HOME/.hermes\"\n"
                "  printf '%s\\n' \"$5\" > \"$HOME/hermes-mcp-command\"\n"
                "  touch \"$HOME/hermes-mcp-configured\"\n"
                "  printf 'mcp_servers:\\n  memleaf:\\n    command: \"%s\"\\n    args:\\n      - --vault\\n      - \"%s\"\\n' \"$5\" \"$8\" > \"$HOME/.hermes/config.yaml\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = mcp ] && [ \"$2\" = test ]; then\n"
                "  if [ \"${MEMLEAF_TEST_HERMES_MCP_STATUS:-ok}\" = fail ]; then\n"
                "    printf 'MCP test failed\\n'\n"
                "    exit 1\n"
                "  fi\n"
                "  if [ -f \"$HOME/hermes-mcp-configured\" ]; then\n"
                "    printf 'Connected (12ms)\\nTools discovered: 11\\n'\n"
                "    exit 0\n"
                "  fi\n"
                "  exit 1\n"
                "fi\n"
                "if [ \"$1\" = config ] && [ \"$2\" = set ]; then\n"
                "  printf '%s %s %s %s\\n' \"$1\" \"$2\" \"$3\" \"$4\" >> \"$HOME/hermes.calls\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = memory ] && [ \"$2\" = status ]; then\n"
                "  if [ \"${MEMLEAF_TEST_HERMES_STATUS:-ok}\" = fail ]; then\n"
                "    printf 'Hermes status unavailable\\n'\n"
                "    exit 1\n"
                "  fi\n"
                "  printf 'Memory status\\n  Provider: memleaf\\n  Plugin:    installed ✓\\n  Status:    available ✓\\n  memleaf ← active\\n'\n"
                "  exit 0\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            hermes.chmod(0o700)

        if codex:
            codex_bin = self.bin / "codex"
            codex_bin.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = mcp ] && [ \"$2\" = get ]; then\n"
                "  if [ -f \"$HOME/codex-configured\" ]; then\n"
                "    printf '%s\\n' '{\"name\":\"memleaf\",\"command\":\"memleaf-mcp\",\"args\":[\"--vault\",\"'\"$HOME/.memleaf\"'\"]}'\n"
                "    exit 0\n"
                "  fi\n"
                "  printf 'server not found\\n' >&2\n"
                "  exit 1\n"
                "fi\n"
                "if [ \"$1\" = mcp ] && [ \"$2\" = add ]; then\n"
                "  touch \"$HOME/codex-configured\"\n"
                "  exit 0\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            codex_bin.chmod(0o700)

        if antigravity:
            antigravity_config = self.home / ".gemini" / "config" / "mcp_config.json"
            antigravity_config.parent.mkdir(parents=True, exist_ok=True)
            antigravity_config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

        environment = self.env()
        environment["PATH"] = f"{self.bin}:/usr/bin:/bin"
        environment["MEMLEAF_TEST_HERMES_STATUS"] = hermes_status
        environment["MEMLEAF_TEST_HERMES_MCP_STATUS"] = hermes_mcp_status
        return subprocess.run(
            [str(self.install_root / "install.sh")],
            cwd=self.install_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def configure_existing_model(self) -> None:
        vault = self.home / ".memleaf"
        vault.mkdir()
        config = vault / "config.yaml"
        config.write_text(
            f"vault: {vault}\n"
            "llm:\n"
            "  mode: api\n"
            "  provider: openai\n"
            "  protocol: openai\n"
            "  base_url: https://example.test/v1\n"
            "  api_key: existing-secret\n"
            "  model: example-mini\n",
            encoding="utf-8",
        )
        config.chmod(0o600)

    def test_wrong_physical_root_is_rejected_with_local_root_hint(self) -> None:
        environment = self.env()
        result = subprocess.run(
            [str(ROOT / "install.sh")],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("place the local source", result.stderr)
        self.assertIn("$HOME/memleaf", result.stderr)
        self.assertFalse((self.home / ".hermes").exists())
        self.assertFalse((self.home / ".memleaf").exists())

    def test_install_works_without_pip_or_setuptools(self) -> None:
        self.configure_existing_model()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        probe = subprocess.run(
            [str(self.install_root / ".venv/bin/python"), "-c",
             "import importlib.util, memleaf; "
             "assert importlib.util.find_spec('pip') is None; "
             "assert importlib.util.find_spec('setuptools') is None; "
             "print(memleaf.__file__)"],
            env=self.env(), cwd=self.home, capture_output=True, text=True, check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn(str(self.install_root / "src/memleaf"), probe.stdout)

    def test_installer_accepts_python3_when_versioned_command_is_absent(self) -> None:
        self.configure_existing_model()
        (self.bin / "python3.11").rename(self.bin / "python3")
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_explicit_interpreter_fails_before_vault_changes(self) -> None:
        environment = self.env()
        environment["MEMLEAF_PYTHON"] = str(self.home / "missing-python")
        result = subprocess.run(
            [str(self.install_root / "install.sh")], cwd=self.install_root,
            env=environment, capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("working Python 3.11+", result.stderr)
        self.assertFalse((self.home / ".memleaf").exists())

    def test_symlinked_install_root_is_rejected_without_side_effects(self) -> None:
        shutil.rmtree(self.install_root)
        symlink_target = Path(self.tempdir.name) / "symlink-target"
        symlink_target.mkdir()
        self.install_root.symlink_to(symlink_target, target_is_directory=True)
        result = subprocess.run(
            [str(ROOT / "install.sh")],
            cwd=ROOT,
            env=self.env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinked installation root", result.stderr)
        self.assertFalse((self.home / ".hermes").exists())
        self.assertFalse((self.home / ".memleaf").exists())
        self.assertFalse((self.install_root / ".venv").exists())

    def test_no_hermes_installs_to_home_root_without_provider_side_effects(self) -> None:
        self.configure_existing_model()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.install_root / ".venv" / "bin" / "memleaf").is_file())
        self.assertTrue((self.install_root / ".venv" / "bin" / "memleaf-mcp").is_file())
        self.assertTrue(
            any(
                path.name == "memleaf-local.pth"
                for path in (self.install_root / ".venv").glob("lib/python*/site-packages/*.pth")
            )
        )
        self.assertNotIn("-m pip", (self.install_root / "install.sh").read_text(encoding="utf-8"))
        self.assertTrue((self.home / ".local" / "bin" / "memleaf").is_symlink())
        self.assertTrue((self.home / ".local" / "bin" / "memleaf-mcp").is_symlink())
        self.assertTrue((self.home / ".memleaf").is_dir())
        self.assertFalse((self.home / ".hermes" / "plugins" / "memleaf").exists())
        self.assertFalse((self.home / ".hermes" / "memleaf.json").exists())
        self.assertIn("Hermes executable not found", result.stderr)
        agents = json.loads(
            (self.home / ".memleaf" / "_index" / "agents.json").read_text(encoding="utf-8")
        )
        self.assertNotEqual("active", agents["agents"]["hermes"].get("provider_status"))
        self.assertNotEqual("active", agents["agents"]["hermes"].get("status"))
        self.assertFalse((self.home / ".codex" / "hooks.json").exists())
        self.assertFalse((self.home / ".gemini" / "config" / "hooks.json").exists())

    def test_install_configures_only_hermes_with_other_hosts_present(self) -> None:
        self.configure_existing_model()
        antigravity_config = self.home / ".gemini" / "config" / "mcp_config.json"
        antigravity_config.parent.mkdir(parents=True, exist_ok=True)
        antigravity_config.write_text(
            json.dumps({"mcpServers": {"dbx": {"command": "dbx"}}}),
            encoding="utf-8",
        )
        antigravity_before = antigravity_config.read_bytes()

        result = self.run_install(hermes=True, codex=True)
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertFalse((self.home / ".codex").exists())
        self.assertFalse((self.home / "codex-configured").exists())

        self.assertTrue((self.home / ".hermes" / "plugins" / "memleaf").is_symlink())
        self.assertTrue((self.home / ".hermes" / "memleaf.json").is_file())
        self.assertEqual(antigravity_before, antigravity_config.read_bytes())
        self.assertFalse((self.home / ".gemini" / "config" / "hooks.json").exists())
        self.assertIn("codex: disabled", result.stdout)
        self.assertNotIn("/hooks", result.stdout)
        self.assertIn("antigravity: disabled", result.stdout)
        self.assertNotIn("Fully quit and reopen Antigravity", result.stdout)
        self.assertNotIn("bypass", result.stdout.lower())

    def test_noninteractive_install_without_model_fails_before_provider_registration(self) -> None:
        result = self.run_install()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("model: failure", result.stdout)
        self.assertFalse((self.home / ".hermes" / "plugins" / "memleaf").exists())

    def test_hermes_activation_status_config_and_repeat_are_idempotent(self) -> None:
        self.configure_existing_model()
        index_path = self.home / ".memleaf" / "_index" / "agents.json"
        index_path.parent.mkdir(parents=True)
        index_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "custom": {"preserve": True},
                    "agents": {
                        "codex": {"sentinel": "codex"},
                        "antigravity": {"sentinel": "antigravity"},
                        "hermes": {"legacy": "keep"},
                    },
                }
            ),
            encoding="utf-8",
        )
        first = self.run_install(hermes=True)
        self.assertEqual(first.returncode, 0, first.stderr)

        plugin = self.home / ".hermes" / "plugins" / "memleaf"
        self.assertTrue(plugin.is_symlink())
        self.assertEqual(
            os.path.realpath(plugin),
            str((self.install_root / "src" / "memleaf" / "hermes_provider").resolve()),
        )
        config_path = self.home / ".hermes" / "memleaf.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["command"], str(self.install_root / ".venv" / "bin" / "memleaf-mcp"))
        self.assertEqual(config["vault"], "~/.memleaf")
        self.assertTrue(config["auto_process"])
        self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
        model_config = load_config(self.home / ".memleaf" / "config.yaml")["llm"]
        self.assertEqual(model_config["model"], "deepseek-flash")
        self.assertEqual(model_config["api_key"], "install-secret")
        self.assertEqual(model_config["request_timeout"], 120)
        self.assertEqual(stat.S_IMODE((self.home / ".memleaf" / "config.yaml").stat().st_mode), 0o600)
        self.assertNotIn("install-secret", first.stdout + first.stderr)
        agents_path = self.home / ".memleaf" / "_index" / "agents.json"
        first_agents = json.loads(agents_path.read_text(encoding="utf-8"))
        hermes_agent = first_agents["agents"]["hermes"]
        self.assertEqual("configured", hermes_agent["status"])
        self.assertEqual("active", hermes_agent["provider_status"])
        self.assertEqual("active", hermes_agent["mcp_status"])
        self.assertEqual("available", hermes_agent["mcp_availability"])
        self.assertTrue(hermes_agent["detected"])
        self.assertEqual("high", hermes_agent["confidence"])
        self.assertEqual(str((self.bin / "hermes").resolve()), hermes_agent["executable"])
        self.assertEqual(str(config_path.resolve()), hermes_agent["config_path"])
        self.assertEqual("keep", hermes_agent["legacy"])
        self.assertTrue(first_agents["custom"]["preserve"])
        self.assertEqual("codex", first_agents["agents"]["codex"]["sentinel"])
        self.assertEqual("antigravity", first_agents["agents"]["antigravity"]["sentinel"])
        self.assertEqual("disabled", first_agents["agents"]["antigravity"]["status"])
        self.assertEqual("disabled", first_agents["agents"]["antigravity"]["hook_activation_status"])
        self.assertFalse(first_agents["agents"]["antigravity"]["user_action_required"])
        self.assertNotIn("hook_activation_status", hermes_agent)
        self.assertEqual(
            str(self.install_root / ".venv" / "bin" / "memleaf-mcp"),
            (self.home / "hermes-mcp-command").read_text(encoding="utf-8").strip(),
        )
        hermes_calls = (self.home / "hermes.calls").read_text(encoding="utf-8")
        self.assertIn("config set mcp_servers.memleaf.lazy true", hermes_calls)
        self.assertIn(
            "config set mcp_servers.memleaf.idle_timeout_seconds 60",
            hermes_calls,
        )
        self.assertNotIn("Memory status", first.stdout)
        self.assertNotIn("Tools discovered", first.stdout)

        before = config_path.read_bytes()
        second = self.run_install(hermes=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(config_path.read_bytes(), before)
        self.assertEqual(
            (self.home / "hermes.calls").read_text(encoding="utf-8").count("config set memory.provider memleaf"),
            2,
        )
        hermes_calls = (self.home / "hermes.calls").read_text(encoding="utf-8")
        self.assertEqual(2, hermes_calls.count("config set mcp_servers.memleaf.lazy true"))
        self.assertEqual(
            2,
            hermes_calls.count("config set mcp_servers.memleaf.idle_timeout_seconds 60"),
        )
        second_agents = json.loads(agents_path.read_text(encoding="utf-8"))
        self.assertEqual(first_agents, second_agents)

    def test_hermes_provider_status_failure_does_not_mark_active(self) -> None:
        self.configure_existing_model()
        result = self.run_install(hermes=True, hermes_status="fail")
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("provider_status", result.stdout + result.stderr)
        agents = json.loads(
            (self.home / ".memleaf" / "_index" / "agents.json").read_text(encoding="utf-8")
        )
        hermes_agent = agents["agents"]["hermes"]
        self.assertNotEqual("active", hermes_agent.get("provider_status"))
        self.assertNotEqual("active", hermes_agent.get("status"))

    def test_hermes_mcp_test_failure_does_not_mark_active(self) -> None:
        self.configure_existing_model()
        result = self.run_install(hermes=True, hermes_mcp_status="fail")
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("mcp_status", result.stdout + result.stderr)
        agents = json.loads(
            (self.home / ".memleaf" / "_index" / "agents.json").read_text(encoding="utf-8")
        )
        hermes_agent = agents["agents"]["hermes"]
        self.assertEqual("configured", hermes_agent["status"])
        self.assertEqual("active", hermes_agent["provider_status"])
        self.assertEqual("failed", hermes_agent["mcp_status"])
        self.assertEqual("unavailable", hermes_agent["mcp_availability"])

    def test_wrong_existing_links_are_not_overwritten(self) -> None:
        target = self.home / "elsewhere"
        target.write_text("keep", encoding="utf-8")
        user_bin = self.home / ".local" / "bin"
        user_bin.mkdir(parents=True)
        (user_bin / "memleaf").symlink_to(target)
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(os.readlink(user_bin / "memleaf"), str(target))
        self.assertIn("points elsewhere", result.stderr)


if __name__ == "__main__":
    unittest.main()
