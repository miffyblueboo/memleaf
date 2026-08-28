from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from memleaf import cli
from memleaf.adapters.hermes import HermesAdapter
from memleaf.installer import _copy_provider, _hermes_home, _write_provider_config


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
            self.assertIn("version: 0.1.5", (target / "plugin.yaml").read_text(encoding="utf-8"))

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

    def test_cli_install_is_a_first_class_command(self) -> None:
        result = {
            "status": "configured",
            "reason": "ok",
            "vault": "/tmp/memleaf-vault",
            "model": {"status": "configured"},
        }
        with mock.patch("memleaf.installer.install_hermes", return_value=result):
            self.assertEqual(cli.main(["install", "--json"]), 0)


if __name__ == "__main__":
    unittest.main()
