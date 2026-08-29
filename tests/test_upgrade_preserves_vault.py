from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf.installer import _select_vault_path


class UpgradeVaultSelectionTests(unittest.TestCase):
    def test_existing_custom_vault_wins_over_environment_and_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-upgrade-") as temporary:
            root = Path(temporary)
            home = root / "home"
            hermes_home = root / "hermes"
            custom = root / "old-custom-vault"
            env_vault = root / "env-vault"
            hermes_home.mkdir(parents=True)
            (hermes_home / "memleaf.json").write_text(
                json.dumps({"vault": str(custom), "command": "old-memleaf-mcp"}),
                encoding="utf-8",
            )

            selected, source = _select_vault_path(
                home=home,
                hermes_home=hermes_home,
                env={"MEMLEAF_VAULT": str(env_vault)},
            )

            self.assertEqual(selected, custom.resolve())
            self.assertEqual(source, "hermes_config")

    def test_explicit_vault_overrides_existing_installation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-upgrade-") as temporary:
            root = Path(temporary)
            home = root / "home"
            hermes_home = root / "hermes"
            old = root / "old-vault"
            explicit = root / "explicit-vault"
            hermes_home.mkdir(parents=True)
            (hermes_home / "memleaf.json").write_text(
                json.dumps({"vault": str(old)}),
                encoding="utf-8",
            )

            selected, source = _select_vault_path(
                home=home,
                hermes_home=hermes_home,
                vault_path=explicit,
                env={"MEMLEAF_VAULT": str(root / "env-vault")},
            )

            self.assertEqual(selected, explicit.resolve())
            self.assertEqual(source, "explicit")

    def test_environment_vault_is_used_when_no_existing_config_exists(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-upgrade-") as temporary:
            root = Path(temporary)
            home = root / "home"
            env_vault = root / "env-vault"

            selected, source = _select_vault_path(
                home=home,
                hermes_home=root / "hermes",
                env={"MEMLEAF_VAULT": str(env_vault)},
            )

            self.assertEqual(selected, env_vault.resolve())
            self.assertEqual(source, "environment")

    def test_legacy_tilde_vault_is_resolved_against_memleaf_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-upgrade-") as temporary:
            root = Path(temporary)
            home = root / "profile"
            hermes_home = root / "hermes"
            hermes_home.mkdir(parents=True)
            (hermes_home / "memleaf.json").write_text(
                json.dumps({"vault": "~/.memleaf"}),
                encoding="utf-8",
            )

            selected, source = _select_vault_path(
                home=home,
                hermes_home=hermes_home,
                env={},
            )

            self.assertEqual(selected, (home / ".memleaf").resolve())
            self.assertEqual(source, "hermes_config")

    def test_malformed_existing_config_fails_instead_of_switching_vault(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-upgrade-") as temporary:
            root = Path(temporary)
            hermes_home = root / "hermes"
            hermes_home.mkdir(parents=True)
            (hermes_home / "memleaf.json").write_text("{broken", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "invalid existing Hermes memleaf config"):
                _select_vault_path(
                    home=root / "home",
                    hermes_home=hermes_home,
                    env={"MEMLEAF_VAULT": str(root / "env-vault")},
                )


if __name__ == "__main__":
    unittest.main()
