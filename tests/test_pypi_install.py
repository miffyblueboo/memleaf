from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from memleaf import cli
from memleaf.installer import _copy_provider


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
            self.assertIn("version: 0.1.1", (target / "plugin.yaml").read_text(encoding="utf-8"))

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
