from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.index import event_key
from memleaf.llm import ModelError


ROOT = Path(__file__).resolve().parents[1]
PROVIDER_PATH = ROOT / "src" / "memleaf" / "hermes_provider" / "__init__.py"


def load_provider_module():
    """Load the plugin against a minimal stand-in for Hermes' public ABC."""

    memory_provider = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    class RecallStatus:
        def __init__(self, provider_label, count, glyph="🧠"):
            self.provider_label = provider_label
            self.count = count
            self.glyph = glyph

    memory_provider.MemoryProvider = MemoryProvider
    memory_provider.RecallStatus = RecallStatus
    agent = types.ModuleType("agent")
    agent.memory_provider = memory_provider
    with patch.dict(sys.modules, {"agent": agent, "agent.memory_provider": memory_provider}):
        spec = importlib.util.spec_from_file_location("test_memleaf_hermes_provider", PROVIDER_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
    return module._impl, MemoryProvider


provider_module, HermesMemoryProvider = load_provider_module()


class FakeClient:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if not self.responses:
            return {"stored": True}
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        return None


class CoreClient:
    """Dispatch provider calls to the local core without an MCP subprocess."""

    def __init__(self, service, model):
        self.service = service
        self.model = model
        self.calls: list[tuple[str, dict]] = []
        self.event_keys: list[str] = []

    def call_tool(self, name, arguments):
        payload = dict(arguments)
        self.calls.append((name, payload))
        if name == "capture":
            result = self.service.capture(**payload)
            self.event_keys.append(event_key(result.event_id))
            return {"stored": result.stored, "duplicate": result.duplicate}
        if name == "process":
            self.model.event_keys = list(self.event_keys)
            return self.service.process(
                source=payload["source"],
                session_id=payload["session_id"],
                model=self.model,
            )
        raise AssertionError(f"unexpected core tool: {name}")

    def close(self):
        return None


from tests.semantic_fixtures import semantic_fixture

@semantic_fixture
class E2EBackend:
    provider = "fake"
    model = "hermes-local-e2e"

    def __init__(self, *, failing=False):
        self.event_keys: list[str] = []
        self.failing = failing
        self.calls: list[str] = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        del prompt, system, temperature
        self.calls.append(purpose)
        if self.failing:
            raise ModelError("deterministic local model unavailable")
        evidence = self.event_keys[0]
        if purpose == "gate":
            return json.dumps(
                {
                    "candidates": [
                        {
                            "candidate_id": "contact-project",
                            "memory": "Alice is the contact for the Phoenix project.",
                            "evidence_event_ids": [evidence],
                            "duplicate": False,
                            "worth": True,
                            "type": "fact",
                            "scopes": ["global"],
                            "scope_source": "model",
                        }
                    ]
                }
            )
        if purpose == "summarize":
            return json.dumps(
                {
                    "title": "Alice and Phoenix project",
                    "body": (
                        "Alice is the contact for the Phoenix project, whose background "
                        "is tracked in local Markdown memory."
                    ),
                    "tags": ["contact", "project"],
                    "type": "fact",
                    "scopes": ["global"],
                    "scope_source": "model",
                    "sources": [{"event_key": evidence}],
                }
            )
        raise AssertionError(f"unexpected model purpose: {purpose}")

class HermesProviderTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="memleaf-hermes-provider-")
        self.root = Path(self.tempdir.name)
        self.hermes_home = self.root / "hermes"
        self.hermes_home.mkdir()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def provider(self, *, auto_process: bool = True, responses=None):
        provider = provider_module.MemleafMemoryProvider()
        provider._hermes_home = str(self.hermes_home)
        provider._session_id = "initialized-session"
        provider._write_enabled = True
        provider._auto_process = auto_process
        provider._client = FakeClient(responses)
        return provider

    def lineage_provider(self, client=None, *, auto_process: bool = True, responses=None):
        provider = self.provider(auto_process=auto_process, responses=responses)
        if client is not None:
            provider._client = client
        provider._gate_enabled = True
        return provider

    @staticmethod
    def lineage_link(session_id, parent_session_id):
        return {
            "linked": True,
            "source": "hermes",
            "session_id": session_id,
            "parent_session_id": parent_session_id,
        }

    @staticmethod
    def pending_lineage_head(provider):
        queue = provider._pending_lineage
        return queue[0] if queue else None

__all__ = [name for name in globals() if not name.startswith('__')]
