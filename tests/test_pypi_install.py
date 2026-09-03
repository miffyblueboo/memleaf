from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import unittest
from unittest import mock

from memleaf import __version__, cli
from memleaf.adapters.hermes import HermesAdapter
from memleaf.installer import (
    _copy_provider,
    _hermes_home,
    _provider_manifest_version,
    _run,
    _write_provider_config,
    install_codex,
    install_hermes,
)


class PyPIInstallTests(unittest.TestCase):
    def test_packaged_provider_can_be_copied_without_importing_hermes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-pypi-provider-") as temporary:
            hermes_home = Path(temporary) / ".hermes"
            target = _copy_provider(hermes_home)
            self.assertEqual(target, hermes_home / "plugins" / "memleaf")
            self.assertTrue((target / "__init__.py").is_file())
            self.assertTrue((target / "plugin.yaml").is_file())
            self.assertTrue((target / "README.md").is_file())
            self.assertIn("name: memleaf", (target / "plugin.yaml").read_text(encoding="utf-8"))
            self.assertEqual(__version__, _provider_manifest_version(target))

    def test_installer_rejects_provider_version_mismatch_after_copy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-pypi-version-mismatch-") as temporary:
            root = Path(temporary)
            home = root / "home"
            vault_path = root / "vault"
            provider_path = root / ".hermes" / "plugins" / "memleaf"
            provider_path.mkdir(parents=True)
            (provider_path / "plugin.yaml").write_text(
                "name: memleaf\nversion: 0.2.9\n",
                encoding="utf-8",
            )
            detection = SimpleNamespace(detected=True, confidence="high", executable="hermes")
            initialized = SimpleNamespace(root=vault_path)
            model = {"status": "configured"}
            adapter = mock.Mock()
            adapter.detect.return_value = detection

            with mock.patch("memleaf.installer._home_from_environment", return_value=home), \
                 mock.patch("memleaf.installer._select_vault_path", return_value=(vault_path, "default")), \
                 mock.patch("memleaf.installer.Vault.initialize", return_value=initialized), \
                 mock.patch("memleaf.installer._prepare_model_route", return_value=model), \
                 mock.patch("memleaf.installer.HermesAdapter", return_value=adapter), \
                 mock.patch("memleaf.installer._memleaf_mcp_command", return_value=root / "memleaf-mcp"), \
                 mock.patch("memleaf.installer._copy_provider", return_value=provider_path), \
                 mock.patch("memleaf.installer._write_provider_config") as write_config:
                result = install_hermes()

            self.assertEqual("failure", result["status"])
            self.assertEqual(__version__, result["core_version"])
            self.assertEqual("0.2.9", result["provider_version"])
            self.assertIn("version mismatch", result["reason"])
            write_config.assert_not_called()

    @unittest.skipIf(os.name == "nt", "symlink creation is not guaranteed on Windows")
    def test_copy_provider_replaces_verified_legacy_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-pypi-legacy-") as temporary:
            root = Path(temporary)
            hermes_home = root / ".hermes"
            plugins = hermes_home / "plugins"
            plugins.mkdir(parents=True)
            old = root / "old-provider"
            old.mkdir()
            (old / "plugin.yaml").write_text("name: memleaf\nversion: 0.1.0\n", encoding="utf-8")
            (plugins / "memleaf").symlink_to(old, target_is_directory=True)

            target = _copy_provider(hermes_home)

            self.assertTrue(target.is_dir())
            self.assertFalse(target.is_symlink())
            self.assertIn("version: 0.2.19", (target / "plugin.yaml").read_text(encoding="utf-8"))

    def test_windows_hermes_paths_follow_official_native_layout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-win-paths-") as temporary:
            root = Path(temporary)
            home = root / "profile"
            local = root / "LocalAppData"
            expected = (local / "hermes").resolve()

            self.assertEqual(
                _hermes_home(
                    home,
                    env={"LOCALAPPDATA": str(local)},
                    platform="nt",
                ),
                expected,
            )
            adapter = HermesAdapter(
                home=home,
                env={"LOCALAPPDATA": str(local), "PATH": ""},
                platform="nt",
            )
            self.assertEqual(adapter.hermes_home, expected)
            self.assertEqual(
                adapter.known_executables,
                (
                    expected / "bin" / "hermes.exe",
                    expected / "hermes-agent" / "venv" / "Scripts" / "hermes.exe",
                    expected / "bin" / "hermes.cmd",
                ),
            )

            launcher = expected / "bin" / "hermes.exe"
            launcher.parent.mkdir(parents=True)
            launcher.write_bytes(b"fake Windows launcher")
            try:
                launcher.chmod(0o755)
            except OSError:
                pass
            detection = adapter.detect()
            self.assertTrue(detection.detected)
            self.assertEqual("high", detection.confidence)
            self.assertEqual(str(launcher.resolve()), detection.executable)

    def test_installer_host_cli_output_is_decoded_as_utf8(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="中文", stderr="")
        with mock.patch("memleaf.installer.subprocess.run", return_value=completed) as run:
            result = _run(["hermes", "memory", "status"])
        self.assertIs(result, completed)
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "strict")
        self.assertTrue(run.call_args.kwargs["text"])

    def test_provider_config_uses_absolute_vault_on_all_platforms(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-provider-config-") as temporary:
            root = Path(temporary)
            path = root / "hermes" / "memleaf.json"
            command = root / "Scripts" / ("memleaf-mcp.exe" if os.name == "nt" else "memleaf-mcp")
            command.parent.mkdir(parents=True)
            command.write_text("", encoding="utf-8")
            vault = root / "vault"
            vault.mkdir()
            _write_provider_config(path, command, vault)
            import json
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["vault"], str(vault.resolve()))
            self.assertEqual(value["command"], str(command))

    def test_codex_install_reports_independent_model_route_requirement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-codex-model-route-") as temporary:
            root = Path(temporary)
            home = root / "home"
            vault_path = root / "vault"
            detection = SimpleNamespace(
                detected=True,
                confidence="high",
                executable="codex",
            )
            preflight = SimpleNamespace(status="would_configure", reason="ok")
            configured = SimpleNamespace(
                status="configured",
                reason="configured",
                user_action_required=False,
                user_action=None,
                to_dict=lambda: {"status": "configured"},
            )
            adapter = mock.Mock()
            adapter.detect.return_value = detection
            adapter.configure.side_effect = [preflight, configured]
            initialized = SimpleNamespace(
                root=vault_path,
                agents_index_path=vault_path / "index" / "agents.json",
            )
            model = {
                "status": "not_configured",
                "reason": "model discovery disabled and no complete memleaf route exists",
                "selected": None,
            }

            with mock.patch("memleaf.installer._home_from_environment", return_value=home), \
                 mock.patch("memleaf.installer.CodexAdapter", return_value=adapter), \
                 mock.patch("memleaf.installer._select_codex_vault_path", return_value=(vault_path, "default")), \
                 mock.patch("memleaf.installer.Vault.initialize", return_value=initialized), \
                 mock.patch("memleaf.installer._prepare_model_route", return_value=model), \
                 mock.patch("memleaf.installer.update_agents_index"):
                result = install_codex()

            self.assertEqual("configured", result["status"])
            self.assertEqual("model_route_required", result["processing_status"])
            self.assertTrue(result["user_action_required"])
            self.assertIn("independent memleaf Model Route", result["user_action"])
            self.assertIn("not used or modified", result["user_action"])

    def test_cli_install_is_a_first_class_command(self) -> None:
        result = {
            "status": "configured",
            "reason": "ok",
            "vault": "/tmp/memleaf-vault",
            "model": {"status": "configured"},
        }
        with mock.patch("memleaf.installer.install_hermes", return_value=result) as hermes:
            with mock.patch("memleaf.installer.install_codex") as codex:
                self.assertEqual(cli.main(["install", "--json"]), 0)
        hermes.assert_called_once_with(vault_path=None)
        codex.assert_not_called()

    def test_cli_hermes_install_prints_core_and_provider_versions(self) -> None:
        result = {
            "status": "configured",
            "reason": "ok",
            "vault": "/tmp/memleaf-vault",
            "core_version": "0.2.19",
            "provider_version": "0.2.19",
            "model": {"status": "configured"},
        }
        with mock.patch("memleaf.installer.install_hermes", return_value=result), \
             mock.patch("sys.stdout") as stdout:
            self.assertEqual(cli.main(["install"]), 0)
        output = "\n".join(str(call.args[0]) for call in stdout.write.call_args_list)
        self.assertIn("core=0.2.19", output)
        self.assertIn("Hermes provider=0.2.19", output)

    def test_readmes_document_one_line_core_and_provider_upgrade(self) -> None:
        command = "python -m pip install -U memleaf && python -m memleaf install"
        for filename, heading in (("README.md", "### 更新 memleaf"), ("README.en.md", "### Updating memleaf")):
            with self.subTest(filename=filename):
                text = (Path(__file__).resolve().parents[1] / filename).read_text(encoding="utf-8")
                section = text.split(heading, 1)[1].split("\n### ", 1)[0]
                self.assertIn(command, section)
                self.assertTrue("core" in section.casefold() or "核心" in section)
                self.assertIn("provider", section.casefold())

    def test_cli_codex_install_requires_explicit_host(self) -> None:
        result = {
            "status": "already_configured",
            "reason": "ok",
            "vault": "/tmp/memleaf-vault",
            "model": {"status": "already_configured"},
        }
        with mock.patch("memleaf.installer.install_codex", return_value=result) as codex:
            with mock.patch("memleaf.installer.install_hermes") as hermes:
                self.assertEqual(cli.main(["install", "--host", "codex", "--json"]), 0)
        codex.assert_called_once_with(vault_path=None)
        hermes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
