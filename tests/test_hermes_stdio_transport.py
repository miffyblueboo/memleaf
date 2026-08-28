from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from memleaf import Memleaf


ROOT = Path(__file__).resolve().parents[1]
PROVIDER_PATH = ROOT / "integrations" / "hermes" / "memleaf" / "__init__.py"


def load_provider_module():
    memory_provider = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    class RecallStatus:
        def __init__(self, provider_label, count, glyph="memory"):
            self.provider_label = provider_label
            self.count = count
            self.glyph = glyph

    memory_provider.MemoryProvider = MemoryProvider
    memory_provider.RecallStatus = RecallStatus
    agent = types.ModuleType("agent")
    agent.memory_provider = memory_provider
    with patch.dict(sys.modules, {"agent": agent, "agent.memory_provider": memory_provider}):
        spec = importlib.util.spec_from_file_location(
            "test_memleaf_hermes_stdio_transport",
            PROVIDER_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
    return module


provider_module = load_provider_module()


class HermesStdioTransportTests(unittest.TestCase):
    def test_real_stdio_client_roundtrip_captures_visible_turn(self) -> None:
        command = shutil.which("memleaf-mcp")
        self.assertIsNotNone(command, "memleaf-mcp console entry point must be installed")

        with tempfile.TemporaryDirectory(prefix="memleaf-hermes-stdio-") as temporary:
            vault = Path(temporary) / "vault"
            Memleaf.initialize(vault)
            client = provider_module._MCPClient(
                str(command),
                str(vault),
                timeout=5.0,
                process_timeout=10.0,
            )
            try:
                stats = client.call_tool("stats", {})
                catalog = client.call_tool("scope_catalog", {"limit": 20})
                user = client.call_tool(
                    "capture",
                    {
                        "source": "hermes",
                        "session_id": "stdio-session",
                        "turn_id": "turn-000001",
                        "role": "user",
                        "content": "Windows provider transport user message",
                        "record": True,
                        "visible": True,
                    },
                )
                assistant = client.call_tool(
                    "capture",
                    {
                        "source": "hermes",
                        "session_id": "stdio-session",
                        "turn_id": "turn-000001",
                        "role": "assistant",
                        "content": "Windows provider transport assistant message",
                        "record": True,
                        "visible": True,
                    },
                )
            finally:
                client.close()

            self.assertIsInstance(stats, dict)
            self.assertIn("scopes", catalog)
            self.assertTrue(user.get("stored") or user.get("duplicate"))
            self.assertTrue(assistant.get("stored") or assistant.get("duplicate"))

            inbox = vault / "inbox" / "hermes" / "stdio-session.md"
            self.assertTrue(inbox.is_file())
            body = inbox.read_text(encoding="utf-8")
            self.assertIn("Windows provider transport user message", body)
            self.assertIn("Windows provider transport assistant message", body)


if __name__ == "__main__":
    unittest.main()
