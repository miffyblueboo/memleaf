from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from memleaf import __version__, cli
from memleaf.adapters.base import CommandResult, ConfigureResult, Detection
from memleaf.hermes_runtime import HermesMcpInspection, inspect_hermes_mcp
from memleaf.installer import (
    _choose_hermes_mcp_command,
    _configure_hermes_mcp_entry,
    _memleaf_mcp_command,
    install_hermes,
)


class HermesRuntimeInspectionTests(unittest.TestCase):
    def test_block_yaml_vault_args_are_recognized(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-runtime-yaml-") as temporary:
            root = Path(temporary)
            config = root / "config.yaml"
            vault = root / "vault"
            command = root / "venv" / "bin" / "memleaf-mcp"
            vault.mkdir()
            command.parent.mkdir(parents=True)
            command.write_text("", encoding="utf-8")
            config.write_text(
                "mcp_servers:\n"
                "  memleaf:\n"
                f"    command: {json.dumps(str(command))}\n"
                "    args:\n"
                "      - --vault\n"
                f"      - {json.dumps(str(vault))}\n",
                encoding="utf-8",
            )

            inspection = inspect_hermes_mcp(config, vault, command)

            self.assertEqual("correct", inspection.status)
            self.assertEqual(str(command), inspection.configured_command)
            self.assertEqual(str(vault), inspection.configured_vault)

    def test_valid_json_without_mcp_section_is_absent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-runtime-absent-") as temporary:
            root = Path(temporary)
            config = root / "config.yaml"
            config.write_text(json.dumps({"model": {"default": "example"}}), encoding="utf-8")

            inspection = inspect_hermes_mcp(
                config,
                root / "vault",
                root / "venv" / "bin" / "memleaf-mcp",
            )

            self.assertEqual("absent", inspection.status)

    def test_vault_equals_form_is_recognized(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-runtime-equals-") as temporary:
            root = Path(temporary)
            config = root / "config.yaml"
            vault = root / "vault"
            command = root / "venv" / "bin" / "memleaf-mcp"
            vault.mkdir()
            command.parent.mkdir(parents=True)
            command.write_text("", encoding="utf-8")
            config.write_text(
                json.dumps(
                    {
                        "mcp_servers": {
                            "memleaf": {
                                "command": str(command),
                                "args": [f"--vault={vault}"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                "correct",
                inspect_hermes_mcp(config, vault, command).status,
            )

    def test_different_absolute_runtime_is_classified_separately(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-runtime-conflict-") as temporary:
            root = Path(temporary)
            config = root / "config.yaml"
            vault = root / "vault"
            current = root / "managed" / "bin" / "memleaf-mcp"
            existing = root / "source" / ".venv" / "bin" / "memleaf-mcp"
            vault.mkdir()
            current.parent.mkdir(parents=True)
            existing.parent.mkdir(parents=True)
            current.write_text("", encoding="utf-8")
            existing.write_text("", encoding="utf-8")
            config.write_text(
                json.dumps(
                    {
                        "mcp_servers": {
                            "memleaf": {
                                "command": str(existing),
                                "args": ["--vault", str(vault)],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            inspection = inspect_hermes_mcp(config, vault, current)

            self.assertEqual("runtime_conflict", inspection.status)
            self.assertIn("different memleaf runtime", inspection.reason)

    def test_windows_case_and_slash_variants_are_equivalent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-runtime-windows-") as temporary:
            config = Path(temporary) / "config.yaml"
            config.write_text(
                json.dumps(
                    {
                        "mcp_servers": {
                            "memleaf": {
                                "command": r"F:\Source\.venv\Scripts\memleaf-mcp.exe",
                                "args": ["--vault", r"F:\Memleaf\Vault"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            inspection = inspect_hermes_mcp(
                config,
                "f:/memleaf/vault",
                "f:/source/.venv/scripts/MEMLEAF-MCP.EXE",
                platform="nt",
            )

            self.assertEqual("correct", inspection.status)

    def test_other_vault_remains_a_hard_conflict(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-runtime-vault-") as temporary:
            root = Path(temporary)
            config = root / "config.yaml"
            current = root / "bin" / "memleaf-mcp"
            current.parent.mkdir()
            current.write_text("", encoding="utf-8")
            config.write_text(
                json.dumps(
                    {
                        "mcp_servers": {
                            "memleaf": {
                                "command": str(current),
                                "args": ["--vault", str(root / "other")],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            inspection = inspect_hermes_mcp(config, root / "wanted", current)

            self.assertEqual("conflict", inspection.status)
            self.assertIn("different Vault", inspection.reason)


class _ConfigSetRunner:
    def __init__(self, config_path: Path, *, persist: bool = True) -> None:
        self.config_path = config_path
        self.persist = persist
        self.calls: list[list[str]] = []

    def __call__(self, argv, env=None, input_text=None):
        command = list(argv)
        self.calls.append(command)
        if self.persist and command[1:3] == ["config", "set"]:
            key = command[3]
            raw = command[4]
            if self.config_path.exists():
                value = json.loads(self.config_path.read_text(encoding="utf-8"))
            else:
                value = {}
            if raw == "true":
                parsed = True
            elif raw == "false":
                parsed = False
            elif raw.startswith("[") or raw.startswith("{"):
                parsed = json.loads(raw)
            else:
                parsed = raw
            cursor = value
            parts = key.split(".")
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[parts[-1]] = parsed
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(value), encoding="utf-8")
        return CommandResult(0)


class HermesPersistentConfigurationTests(unittest.TestCase):
    def _fixture(self):
        temporary = tempfile.TemporaryDirectory(prefix="memleaf-persist-")
        root = Path(temporary.name)
        config = root / "hermes" / "config.yaml"
        vault = root / "vault"
        command = root / "venv" / "bin" / "memleaf-mcp"
        vault.mkdir()
        command.parent.mkdir(parents=True)
        command.write_text("", encoding="utf-8")
        detection = Detection(
            agent="hermes",
            detected=True,
            confidence="high",
            executable="hermes",
            config_path=str(config),
            status="detected",
        )
        return temporary, root, config, vault, command, detection

    def test_persists_with_config_set_and_reads_back(self) -> None:
        temporary, _, config, vault, command, detection = self._fixture()
        with temporary:
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"other": "keep"}), encoding="utf-8")
            runner = _ConfigSetRunner(config)
            adapter = SimpleNamespace(
                config_path=config,
                platform=os.name,
                runner=runner,
                env={},
            )

            result = _configure_hermes_mcp_entry(
                adapter,
                detection,
                vault,
                str(command),
                allow_runtime_migration=False,
            )

            self.assertEqual("configured", result.status)
            self.assertEqual(4, len(runner.calls))
            self.assertTrue(all(call[1:3] == ["config", "set"] for call in runner.calls))
            self.assertFalse(any("add" in call for call in runner.calls))
            value = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual("keep", value["other"])
            self.assertEqual(str(command), value["mcp_servers"]["memleaf"]["command"])
            self.assertEqual(
                ["--vault", str(vault.resolve())],
                value["mcp_servers"]["memleaf"]["args"],
            )

    def test_zero_exit_without_persistence_is_failure(self) -> None:
        temporary, _, config, vault, command, detection = self._fixture()
        with temporary:
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"other": "keep"}), encoding="utf-8")
            runner = _ConfigSetRunner(config, persist=False)
            adapter = SimpleNamespace(
                config_path=config,
                platform=os.name,
                runner=runner,
                env={},
            )

            result = _configure_hermes_mcp_entry(
                adapter,
                detection,
                vault,
                str(command),
                allow_runtime_migration=False,
            )

            self.assertEqual("failure", result.status)
            self.assertIn("not confirmed", result.reason)
            self.assertNotIn("mcp_servers", json.loads(config.read_text(encoding="utf-8")))

    def test_runtime_migration_requires_explicit_permission(self) -> None:
        temporary, root, config, vault, command, detection = self._fixture()
        with temporary:
            old = root / "source" / ".venv" / "bin" / "memleaf-mcp"
            old.parent.mkdir(parents=True)
            old.write_text("", encoding="utf-8")
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "mcp_servers": {
                            "memleaf": {
                                "command": str(old),
                                "args": ["--vault", str(vault)],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            runner = _ConfigSetRunner(config)
            adapter = SimpleNamespace(
                config_path=config,
                platform=os.name,
                runner=runner,
                env={},
            )

            refused = _configure_hermes_mcp_entry(
                adapter,
                detection,
                vault,
                str(command),
                allow_runtime_migration=False,
            )
            accepted = _configure_hermes_mcp_entry(
                adapter,
                detection,
                vault,
                str(command),
                allow_runtime_migration=True,
            )

            self.assertEqual("diagnostic", refused.status)
            self.assertEqual("configured", accepted.status)


class HermesRuntimeDiscoveryTests(unittest.TestCase):
    def test_current_python_scripts_win_over_another_runtime_on_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-runtime-discovery-") as temporary:
            root = Path(temporary)
            scripts = root / "current" / "bin"
            path_runtime = root / "source" / ".venv" / "bin"
            scripts.mkdir(parents=True)
            path_runtime.mkdir(parents=True)
            name = "memleaf-mcp.exe" if os.name == "nt" else "memleaf-mcp"
            current = scripts / name
            other = path_runtime / name
            current.write_text("", encoding="utf-8")
            other.write_text("", encoding="utf-8")

            with mock.patch(
                "memleaf.installer.sysconfig.get_path",
                return_value=str(scripts),
            ), mock.patch(
                "memleaf.installer.site.USER_BASE",
                str(root / "user"),
            ), mock.patch(
                "memleaf.installer.shutil.which",
                return_value=str(other),
            ):
                selected = _memleaf_mcp_command()

            self.assertEqual(current.resolve(), selected)


class HermesRuntimePolicyTests(unittest.TestCase):
    def test_auto_stops_on_second_runtime_without_executing_it(self) -> None:
        current = Path("/managed/bin/memleaf-mcp")
        inspection = HermesMcpInspection(
            status="runtime_conflict",
            reason="different runtime",
            config_path="/home/.hermes/config.yaml",
            expected_command=str(current),
            configured_command="/source/.venv/bin/memleaf-mcp",
            configured_vault="/vault",
        )
        with mock.patch("memleaf.installer._probe_memleaf_runtime_version") as probe:
            selected, details = _choose_hermes_mcp_command(
                inspection,
                current,
                policy="auto",
                core_version=__version__,
            )
        self.assertIsNone(selected)
        self.assertEqual("requires_explicit_choice", details["selection_status"])
        probe.assert_not_called()

    def test_existing_policy_requires_matching_version(self) -> None:
        current = Path("/managed/bin/memleaf-mcp")
        existing = "/source/.venv/bin/memleaf-mcp"
        inspection = HermesMcpInspection(
            status="runtime_conflict",
            reason="different runtime",
            config_path="/home/.hermes/config.yaml",
            expected_command=str(current),
            configured_command=existing,
            configured_vault="/vault",
        )
        with mock.patch(
            "memleaf.installer._probe_memleaf_runtime_version",
            return_value=("0.0.1", None),
        ):
            selected, details = _choose_hermes_mcp_command(
                inspection,
                current,
                policy="existing",
                core_version=__version__,
            )
        self.assertIsNone(selected)
        self.assertEqual("version_mismatch", details["selection_status"])

        with mock.patch(
            "memleaf.installer._probe_memleaf_runtime_version",
            return_value=(__version__, None),
        ):
            selected, details = _choose_hermes_mcp_command(
                inspection,
                current,
                policy="existing",
                core_version=__version__,
            )
        self.assertEqual(existing, selected)
        self.assertEqual("selected", details["selection_status"])


class HermesInstallerTransactionTests(unittest.TestCase):
    def test_runtime_conflict_stops_before_vault_or_provider_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-preflight-stop-") as temporary:
            root = Path(temporary)
            home = root / "home"
            hermes_home = root / "hermes"
            config = hermes_home / "config.yaml"
            vault = root / "vault"
            current = root / "managed" / "bin" / "memleaf-mcp"
            existing = root / "source" / ".venv" / "bin" / "memleaf-mcp"
            config.parent.mkdir(parents=True)
            vault.mkdir()
            current.parent.mkdir(parents=True)
            existing.parent.mkdir(parents=True)
            current.write_text("", encoding="utf-8")
            existing.write_text("", encoding="utf-8")
            config.write_text(
                json.dumps(
                    {
                        "mcp_servers": {
                            "memleaf": {
                                "command": str(existing),
                                "args": ["--vault", str(vault)],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            detection = Detection(
                agent="hermes",
                detected=True,
                confidence="high",
                executable="hermes",
                config_path=str(config),
                status="detected",
            )
            adapter = mock.Mock()
            adapter.detect.return_value = detection
            adapter.config_path = config
            adapter.platform = os.name

            with mock.patch("memleaf.installer._home_from_environment", return_value=home), \
                 mock.patch("memleaf.installer._hermes_home", return_value=hermes_home), \
                 mock.patch("memleaf.installer._select_vault_path", return_value=(vault, "explicit")), \
                 mock.patch("memleaf.installer.HermesAdapter", return_value=adapter), \
                 mock.patch("memleaf.installer._memleaf_mcp_command", return_value=current), \
                 mock.patch("memleaf.installer.Vault.initialize") as initialize, \
                 mock.patch("memleaf.installer._prepare_model_route") as model, \
                 mock.patch("memleaf.installer._copy_provider") as copy_provider:
                result = install_hermes()

            self.assertEqual("failure", result["status"])
            self.assertEqual("mcp_preflight", result["stage"])
            self.assertTrue(result["user_action_required"])
            initialize.assert_not_called()
            model.assert_not_called()
            copy_provider.assert_not_called()

    def test_late_mcp_failure_restores_original_hermes_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-transaction-rollback-") as temporary:
            root = Path(temporary)
            home = root / "home"
            hermes_home = root / "hermes"
            config = hermes_home / "config.yaml"
            vault = root / "vault"
            current = root / "managed" / "bin" / "memleaf-mcp"
            existing = root / "source" / ".venv" / "bin" / "memleaf-mcp"
            config.parent.mkdir(parents=True)
            vault.mkdir()
            current.parent.mkdir(parents=True)
            existing.parent.mkdir(parents=True)
            current.write_text("", encoding="utf-8")
            existing.write_text("", encoding="utf-8")
            original = json.dumps(
                {
                    "other": "keep",
                    "mcp_servers": {
                        "memleaf": {
                            "command": str(existing),
                            "args": ["--vault", str(vault)],
                        }
                    },
                }
            )
            config.write_text(original, encoding="utf-8")
            detection = Detection(
                agent="hermes",
                detected=True,
                confidence="high",
                executable="hermes",
                config_path=str(config),
                status="detected",
            )
            adapter = mock.Mock()
            adapter.detect.return_value = detection
            adapter.config_path = config
            adapter.platform = os.name
            adapter.configure_mcp_lifecycle.return_value = False
            initialized = SimpleNamespace(
                root=vault,
                agents_index_path=vault / "_index" / "agents.json",
            )
            configured = ConfigureResult(
                agent="hermes",
                detected=True,
                confidence="high",
                executable="hermes",
                config_path=str(config),
                status="configured",
                changed=True,
                reason="configured",
            )

            def change_config(*args, **kwargs):
                config.write_text("changed", encoding="utf-8")
                return configured

            with mock.patch("memleaf.installer._home_from_environment", return_value=home), \
                 mock.patch("memleaf.installer._hermes_home", return_value=hermes_home), \
                 mock.patch("memleaf.installer._select_vault_path", return_value=(vault, "explicit")), \
                 mock.patch("memleaf.installer.HermesAdapter", return_value=adapter), \
                 mock.patch("memleaf.installer._memleaf_mcp_command", return_value=current), \
                 mock.patch("memleaf.installer.Vault.initialize", return_value=initialized), \
                 mock.patch("memleaf.installer._prepare_model_route", return_value={"status": "configured"}), \
                 mock.patch("memleaf.installer._configure_hermes_mcp_entry", side_effect=change_config), \
                 mock.patch("memleaf.installer._copy_provider") as copy_provider:
                result = install_hermes(mcp_runtime="current")

            self.assertEqual("failure", result["status"])
            self.assertEqual("mcp_lifecycle", result["stage"])
            self.assertEqual("completed", result["rollback_status"])
            self.assertEqual(original, config.read_text(encoding="utf-8"))
            copy_provider.assert_not_called()


class HermesInstallCliDiagnosticsTests(unittest.TestCase):
    def test_failure_output_includes_stage_detail_action_and_commands(self) -> None:
        failure = {
            "status": "failure",
            "stage": "mcp_persist",
            "reason": "Hermes MCP entry could not be configured",
            "core_version": __version__,
            "provider_version": None,
            "provider_updated": False,
            "vault": "/vault",
            "mcp": {
                "reason": "config writer returned success but entry was not confirmed",
                "config_path": "/home/.hermes/config.yaml",
                "backup_path": "/home/.hermes/config.yaml.bak",
            },
            "mcp_runtime": {
                "config_path": "/home/.hermes/config.yaml",
                "configured_command": "/old/memleaf-mcp",
                "current_command": "/new/memleaf-mcp",
            },
            "user_action_required": True,
            "user_action": "verify the persisted entry",
            "recovery_commands": [["hermes", "mcp", "test", "memleaf"]],
            "rollback_status": "completed",
        }
        stdout = StringIO()
        stderr = StringIO()
        with mock.patch("memleaf.installer.install_hermes", return_value=failure), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(["install"])

        self.assertEqual(2, result)
        text = stderr.getvalue()
        self.assertIn("failed stage: mcp_persist", text)
        self.assertIn("MCP detail:", text)
        self.assertIn("configured MCP runtime: /old/memleaf-mcp", text)
        self.assertIn("action required:", text)
        self.assertIn("hermes mcp test memleaf", text)
        self.assertIn("restored to its pre-install state", text)

    def test_explicit_runtime_policy_is_forwarded_to_installer(self) -> None:
        success = {
            "status": "configured",
            "reason": "ok",
            "vault": "/vault",
            "model": {"status": "configured"},
        }
        with mock.patch("memleaf.installer.install_hermes", return_value=success) as install, \
             redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = cli.main(["install", "--mcp-runtime", "current", "--json"])

        self.assertEqual(0, result)
        install.assert_called_once_with(vault_path=None, mcp_runtime="current")


if __name__ == "__main__":
    unittest.main()
