from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf.adapters.base import CommandResult
from memleaf.credentials import credential_text
from memleaf.model_discovery import discover_hermes


class CredentialSafetyTests(unittest.TestCase):
    def test_hermes_masked_key_falls_through_to_dotenv(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-credential-") as temporary:
            home = Path(temporary)
            hermes_home = home / "hermes"
            hermes_home.mkdir()
            (hermes_home / ".env").write_text(
                "DEEPSEEK_API_KEY=real-env-secret\n",
                encoding="utf-8",
            )
            payloads = {
                "model": {},
                "custom_providers": [
                    {
                        "name": "deepseek",
                        "base_url": "https://api.deepseek.com/v1",
                        "api_key": "***",
                        "api_mode": "chat_completions",
                        "model": "deepseek-chat",
                    }
                ],
            }

            def runner(argv, env=None):
                if argv[1:3] == ["config", "env-path"]:
                    return CommandResult(1, "", "not configured")
                if argv[1:3] == ["config", "get"]:
                    return CommandResult(0, json.dumps(payloads[argv[3]]), "")
                return CommandResult(1, "", "not configured")

            candidates, diagnostics = discover_hermes(
                home=home,
                env={"PATH": "", "HERMES_HOME": str(hermes_home)},
                executable="hermes",
                runner=runner,
            )

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].api_key, "real-env-secret")
            self.assertNotIn("real-env-secret", " ".join(diagnostics))

    def test_hermes_masked_key_without_real_credential_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-credential-") as temporary:
            home = Path(temporary)
            hermes_home = home / "hermes"
            hermes_home.mkdir()
            payloads = {
                "model": {},
                "custom_providers": [
                    {
                        "name": "deepseek",
                        "base_url": "https://api.deepseek.com/v1",
                        "api_key": "sk-p...7890",
                        "api_mode": "chat_completions",
                        "model": "deepseek-chat",
                    }
                ],
            }

            def runner(argv, env=None):
                if argv[1:3] == ["config", "env-path"]:
                    return CommandResult(1, "", "not configured")
                if argv[1:3] == ["config", "get"]:
                    return CommandResult(0, json.dumps(payloads[argv[3]]), "")
                return CommandResult(1, "", "not configured")

            candidates, diagnostics = discover_hermes(
                home=home,
                env={"PATH": "", "HERMES_HOME": str(hermes_home)},
                executable="hermes",
                runner=runner,
            )

            self.assertEqual(candidates, [])
            self.assertTrue(any("redacted" in item for item in diagnostics))
            self.assertNotIn("sk-p...7890", " ".join(diagnostics))

    def test_display_masks_are_rejected_without_length_heuristic(self) -> None:
        for value in ("***", "********", "sk-p...7890", "<redacted>", "[masked]"):
            with self.subTest(value=value):
                self.assertIsNone(credential_text(value))
        self.assertEqual("short-real-key", credential_text("short-real-key"))


if __name__ == "__main__":
    unittest.main()
