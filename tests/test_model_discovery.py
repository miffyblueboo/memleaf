from __future__ import annotations

import json
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from memleaf.adapters.base import CommandResult
from memleaf import cli
from memleaf.config import load_config
from memleaf.llm import ModelRouter
from memleaf.credentials import credential_text
from memleaf.model_discovery import (
    DiscoveryResult,
    ModelCandidate,
    discover_codex,
    discover_hermes,
    discover_models,
    manual_candidate,
    select_lightest,
    write_model_config,
)


class ModelDiscoveryTests(unittest.TestCase):
    def test_default_discovery_never_scans_codex(self):
        with (
            patch("memleaf.model_discovery.discover_hermes", return_value=([], [])),
            patch("memleaf.model_discovery.discover_codex", side_effect=AssertionError("must not scan Codex")) as codex,
        ):
            result = discover_models()
        codex.assert_not_called()
        self.assertIsNone(result.selected)

    def test_lightness_is_deterministic_and_excludes_non_chat_models(self):
        def candidate(model: str) -> ModelCandidate:
            return ModelCandidate(
                source="manual",
                provider="test",
                protocol="openai",
                base_url="https://example.test/v1",
                model=model,
                api_key="key",
            )

        selected = select_lightest(
            [candidate("provider-pro"), candidate("provider-mini"), candidate("provider-embedding")]
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.model, "provider-mini")
        self.assertIsNone(select_lightest([candidate("provider-embedding")]))

    def test_hermes_cli_json_fixture_discovers_current_and_custom_routes_without_leaking_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            hermes_home = home / "custom-hermes"
            hermes_home.mkdir()
            (hermes_home / ".env").write_text("DEEPSEEK_API_KEY=env-secret\n", encoding="utf-8")
            payloads = {
                "model": {
                    "default": "deepseek-v4-flash-vision-exp",
                    "provider": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                },
                "custom_providers": [{
                    "name": "heavy",
                    "base_url": "https://example.test/v1",
                    "api_key": "custom-secret",
                    "api_mode": "chat_completions",
                    "model": "provider-pro",
                }],
            }

            def runner(argv, env=None):
                if argv[1:3] == ["config", "env-path"]:
                    return CommandResult(1, "", "not configured")
                if argv[1:3] == ["config", "get"]:
                    return CommandResult(0, json.dumps(payloads[argv[3]]), "")
                return CommandResult(1, "", "not configured")

            result = discover_models(
                home=home,
                env={"PATH": "/bin", "HERMES_HOME": str(hermes_home)},
                hermes_executable="/bin/hermes",
                runner=runner,
                include_codex=False,
            )
            self.assertIsNotNone(result.selected)
            self.assertEqual(result.selected.model, "deepseek-v4-flash-vision-exp")
            rendered = json.dumps(result.to_dict(), ensure_ascii=False)
            self.assertNotIn("env-secret", rendered)
            self.assertNotIn("custom-secret", rendered)

    def test_redacted_hermes_key_falls_through_to_dotenv(self):
        with tempfile.TemporaryDirectory() as temporary:
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
                executable="/fake/hermes",
                runner=runner,
            )

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].api_key, "real-env-secret")
            self.assertNotIn("real-env-secret", " ".join(diagnostics))

    def test_redacted_hermes_key_without_real_credential_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
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
                executable="/fake/hermes",
                runner=runner,
            )

            self.assertEqual(candidates, [])
            self.assertTrue(any("redacted" in item for item in diagnostics))
            self.assertNotIn("sk-p...7890", " ".join(diagnostics))

    def test_credential_redaction_recognizes_hermes_display_forms(self):
        for value in ("***", "********", "sk-p...7890", "<redacted>", "[masked]"):
            with self.subTest(value=value):
                self.assertIsNone(credential_text(value))
        self.assertEqual(credential_text("short-real-key"), "short-real-key")

    def test_existing_redacted_memleaf_route_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.yaml"
            path.write_text(
                'llm:\n'
                '  mode: "api"\n'
                '  provider: "deepseek"\n'
                '  protocol: "openai"\n'
                '  base_url: "https://api.deepseek.com/v1"\n'
                '  api_key: "***"\n'
                '  api_key_env: ""\n'
                '  model: "deepseek-chat"\n',
                encoding="utf-8",
            )
            self.assertIsNone(cli._existing_memleaf_route(path))
            router = ModelRouter.from_config(load_config(path, vault=root))
            self.assertIsNone(router.api)

    def test_codex_model_discovery_is_fail_closed_and_does_not_read_config(self):
        with tempfile.TemporaryDirectory(prefix="memleaf-codex-model-disabled-") as temporary:
            home = Path(temporary)
            codex_home = home / ".codex"
            codex_home.mkdir()
            config = codex_home / "config.toml"
            config.write_text(
                'model = "sensitive-model"\nmodel_provider = "private"\n',
                encoding="utf-8",
            )
            with patch.object(Path, "read_text", side_effect=AssertionError("must not read Codex config")):
                candidates, diagnostics = discover_codex(
                    home=home,
                    env={"CODEX_HOME": str(codex_home), "PRIVATE_KEY": "SECRET"},
                )
        self.assertEqual([], candidates)
        self.assertTrue(any("disabled" in item for item in diagnostics))
        self.assertNotIn("SECRET", repr(diagnostics))

    def test_include_codex_flag_is_compatibility_noop(self):
        with patch(
            "memleaf.model_discovery.discover_codex",
            return_value=([], ["codex: model discovery is disabled"]),
        ) as codex:
            result = discover_models(
                home=Path("/tmp/isolated-home"),
                env={},
                include_hermes=False,
                include_codex=True,
            )
        codex.assert_called_once()
        self.assertIsNone(result.selected)
        self.assertTrue(any("disabled" in item for item in result.diagnostics))

    def test_direct_key_is_written_with_restricted_permissions_and_router_uses_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.yaml"
            candidate = manual_candidate(
                provider="openai",
                protocol="openai",
                base_url="https://example.test/v1",
                model="small-model",
                api_key="direct-secret",
            )
            write_model_config(path, candidate, vault=Path(temporary))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            value = load_config(path, vault=Path(temporary))
            self.assertEqual(value["llm"]["api_key"], "direct-secret")
            self.assertEqual(value["llm"]["request_timeout"], 120)
            old = os.environ.pop("MEMLEAF_DISCOVERY_KEY", None)
            try:
                router = ModelRouter.from_config(value)
                self.assertEqual(router.api.api_key, "direct-secret")
            finally:
                if old is not None:
                    os.environ["MEMLEAF_DISCOVERY_KEY"] = old

    def test_interactive_fallback_writes_hidden_direct_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            vault = home / ".memleaf"
            home.mkdir()
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.dict(os.environ, {"HOME": str(home), "PATH": ""}, clear=False),
                patch("memleaf.cli.discover_models", return_value=DiscoveryResult()),
                patch("memleaf.cli.sys.stdin.isatty", return_value=True),
                patch("builtins.input", side_effect=["openai", "openai", "https://example.test/v1", "manual-mini"]),
                patch("memleaf.cli.getpass.getpass", return_value="manual-secret"),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                code = cli.main(
                    [
                        "init",
                        "--vault",
                        str(vault),
                        "--no-codex",
                        "--no-hermes",
                        "--no-antigravity",
                    ]
                )
            self.assertEqual(code, 0)
            config = load_config(vault / "config.yaml", vault=vault)
            self.assertEqual(config["llm"]["model"], "manual-mini")
            self.assertEqual(config["llm"]["api_key"], "manual-secret")
            self.assertEqual(config["llm"]["request_timeout"], 120)
            self.assertEqual(stat.S_IMODE((vault / "config.yaml").stat().st_mode), 0o600)
            self.assertNotIn("manual-secret", stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
