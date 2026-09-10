from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memleaf.config import default_config, save_config
from benchmarks.p1.inspect_route import endpoint_class, inspect_config


class P1RouteInspectTests(unittest.TestCase):
    def test_endpoint_class_identifies_supported_public_route_types(self):
        self.assertEqual(endpoint_class("https://api.deepseek.com"), "deepseek-direct")
        self.assertEqual(endpoint_class("https://api.deepseek.com/v1"), "deepseek-direct")
        self.assertEqual(endpoint_class("https://openrouter.ai/api/v1"), "openrouter")
        self.assertEqual(endpoint_class("https://api.openai.com/v1"), "openai-direct")
        self.assertEqual(endpoint_class("https://api.anthropic.com"), "anthropic-direct")
        self.assertEqual(
            endpoint_class("https://generativelanguage.googleapis.com/v1beta"),
            "gemini-direct",
        )
        self.assertEqual(endpoint_class("https://inference-api.nousresearch.com/v1"), "nous")
        self.assertEqual(endpoint_class("https://private.example.invalid/v1"), "custom-or-other")

    def test_inspect_config_never_serializes_secret_or_full_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = default_config(root / "vault")
            config["llm"].update({
                "mode": "api",
                "provider": "deepseek",
                "protocol": "openai",
                "base_url": "https://api.deepseek.com",
                "api_key": "synthetic-secret-never-print",
                "api_key_env": "",
                "model": "deepseek-v4-flash",
            })
            path = root / "config.yaml"
            save_config(path, config)
            value = inspect_config(path)
            encoded = json.dumps(value)
            self.assertEqual(value["status"], "ready")
            self.assertEqual(value["endpoint_class"], "deepseek-direct")
            self.assertEqual(value["provider"], "deepseek")
            self.assertEqual(value["model"], "deepseek-v4-flash")
            self.assertEqual(value["thinking"], "low")
            self.assertEqual(value["model_calls"], 0)
            self.assertNotIn("synthetic-secret-never-print", encoded)
            self.assertNotIn("api.deepseek.com", encoded)
            self.assertNotIn("base_url", value)

    def test_inspect_config_marks_custom_endpoint_without_disclosing_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = default_config(root / "vault")
            config["llm"].update({
                "mode": "api",
                "provider": "custom",
                "protocol": "openai",
                "base_url": "https://private.example.invalid/v1",
                "api_key": "synthetic-secret",
                "api_key_env": "",
                "model": "private-model",
            })
            path = root / "config.yaml"
            save_config(path, config)
            value = inspect_config(path)
            encoded = json.dumps(value)
            self.assertEqual(value["endpoint_class"], "custom-or-other")
            self.assertNotIn("private.example.invalid", encoded)
            self.assertNotIn("synthetic-secret", encoded)


if __name__ == "__main__":
    unittest.main()
