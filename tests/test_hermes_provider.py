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
    return module, MemoryProvider


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


class HermesProviderTests(unittest.TestCase):
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

    def test_native_provider_registration_and_auto_process_config_boolean(self) -> None:
        context = types.SimpleNamespace(register_memory_provider=lambda provider: setattr(context, "provider", provider))
        provider_module.register(context)
        self.assertIsInstance(context.provider, HermesMemoryProvider)
        self.assertEqual(context.provider.name, "memleaf")

        config_path = self.hermes_home / "memleaf.json"
        config_path.write_text(json.dumps({"auto_process": "false"}), encoding="utf-8")
        loaded = provider_module._load_config(self.hermes_home)
        self.assertIs(loaded["auto_process"], False)
        self.assertIs(provider_module._as_bool("false", True), False)
        self.assertIs(provider_module._as_bool("true", False), True)

        provider_module.MemleafMemoryProvider().save_config({"auto_process": "false"}, str(self.hermes_home))
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertIs(saved["auto_process"], False)

    def test_non_primary_hermes_sessions_skip_recall_capture_and_process(self) -> None:
        lifecycle_values = [
            {"platform": "cron", "agent_context": "primary"},
            {"platform": "cli", "agent_context": "flush"},
            {"platform": "cli", "agent_context": "subagent"},
            {"platform": "cli", "agent_context": "cron"},
        ]

        for index, lifecycle in enumerate(lifecycle_values):
            with self.subTest(lifecycle=lifecycle):
                provider = self.provider(responses=[])
                client = provider._client
                with patch.object(provider_module, "_resolve_command") as resolve_command, patch.object(
                    provider_module, "_MCPClient"
                ) as mcp_client:
                    provider.initialize(
                        f"excluded-session-{index}",
                        hermes_home=str(self.hermes_home),
                        **lifecycle,
                    )

                self.assertFalse(provider._write_enabled)
                self.assertIsNone(provider._client)
                resolve_command.assert_not_called()
                mcp_client.assert_not_called()
                provider.on_turn_start(1, {"role": "user", "content": "excluded user"})
                self.assertEqual(provider.prefetch("excluded query"), "")
                provider.sync_turn("excluded user", "excluded assistant")
                self.assertEqual(client.calls, [])
                self.assertEqual(provider.system_prompt_block(), "")
                self.assertIsNone(provider.recall_status())

    def test_interactive_primary_session_keeps_recall_capture_and_process(self) -> None:
        client = FakeClient(
            responses=[
                {"stats": True},
                {
                    "scopes": [
                        {
                            "scope": "project:phoenix",
                            "parent": "project",
                            "aliases": ["phoenix"],
                        }
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
                {"stored": True},
                {"stored": True},
                {"processed_turns": 1},
            ]
        )
        provider = provider_module.MemleafMemoryProvider()
        with patch.object(provider_module, "_resolve_command", return_value="memleaf-mcp"), patch.object(
            provider_module, "_MCPClient", return_value=client
        ):
            provider.initialize(
                "interactive-session",
                hermes_home=str(self.hermes_home),
                platform="cli",
                agent_context="primary",
            )

        context = provider.prefetch("what do we already know?")
        provider.sync_turn("visible user", "visible assistant", session_id="interactive-session")

        self.assertIn("project:phoenix", context)
        self.assertNotIn("memory_id", context)
        self.assertNotIn("PROVIDER_BODY_SENTINEL", context)
        self.assertIsNone(provider.recall_status())
        self.assertEqual(
            [name for name, _ in client.calls],
            ["stats", "scope_catalog", "capture", "capture", "process"],
        )
        self.assertEqual(
            client.calls[1][1],
            {
                "limit": provider_module._MAX_SCOPE_ITEMS,
            },
        )

    def test_prefetch_bodyless_directory_is_bounded_and_keeps_read_ids(self) -> None:
        responses = [
            {
                "scope": f"project:p{index}",
                "parent": "project",
                "aliases": [f"p{index}"],
            }
            for index in range(8)
        ]
        provider = self.provider(
            responses=[{"scopes": responses, "has_more": False, "next_cursor": None}]
        )

        rendered = provider.prefetch("directory query", session_id="directory-session")

        self.assertLessEqual(len(rendered), provider_module._MAX_SCOPE_CHARS)
        self.assertEqual(8, rendered.count("\n- "))
        self.assertIn("call memleaf MCP search", rendered)
        self.assertIn("read only the selected memory", rendered)
        for index in range(8):
            self.assertIn(f"project:p{index}", rendered)
        self.assertNotIn("memory_id", rendered)
        self.assertNotIn("title", rendered)
        self.assertNotIn("body", rendered.casefold())
        self.assertEqual(
            provider._client.calls[0],
            (
                "scope_catalog",
                {
                    "limit": provider_module._MAX_SCOPE_ITEMS,
                },
            ),
        )

    def test_prefetch_connection_failure_reports_unavailable_without_fallback_or_writes(self) -> None:
        provider = self.provider(responses=[RuntimeError("MCP_CONNECTION_SECRET")])

        rendered = provider.prefetch("query", session_id="failed-context-session")

        self.assertIn("scope map was unavailable", rendered.casefold())
        self.assertIn("retrieval was not verified", rendered.casefold())
        self.assertEqual([name for name, _ in provider._client.calls], ["scope_catalog"])
        self.assertFalse(list(self.root.rglob("*.md")))
        self.assertNotIn("MCP_CONNECTION_SECRET", rendered)

    def test_scope_context_keeps_long_ids_and_marks_omitted_metadata(self) -> None:
        long_scope = "project:" + "s" * 320
        long_parent = "parent:" + "p" * 180
        long_alias = "alias:" + "a" * 180
        rendered, _ = provider_module._scope_context(
            {
                "scopes": [
                    {
                        "scope": long_scope,
                        "parent": long_parent,
                        "aliases": [long_alias],
                    }
                ],
                "has_more": True,
                "next_cursor": "scope-next-page",
            },
            retrieval_id="rtv-long-metadata",
        )

        self.assertLessEqual(len(rendered), provider_module._MAX_SCOPE_CHARS)
        self.assertIn(long_scope, rendered)
        self.assertNotIn(long_parent, rendered)
        self.assertNotIn(long_alias, rendered)
        self.assertIn("scope-next-page", rendered)
        self.assertIn("preview incomplete", rendered.casefold())

    def test_prefetch_malformed_scope_catalog_reports_safe_diagnostic(self) -> None:
        provider = self.provider(
            responses=[
                {
                    "scopes": [{"scope": "project:broken", "parent": None, "aliases": "not-a-list"}],
                    "has_more": False,
                    "next_cursor": None,
                }
            ]
        )

        rendered = provider.prefetch("query", session_id="malformed-context-session")

        self.assertIn("scope map was unavailable", rendered.casefold())
        self.assertIn("retrieval was not verified", rendered.casefold())
        self.assertEqual([name for name, _ in provider._client.calls], ["scope_catalog"])
        self.assertNotIn("not-a-list", rendered)

    def test_prefetch_requests_mcp_token_for_current_turn_and_injects_only_scope_map(self) -> None:
        client = FakeClient(
            responses=[
                {
                    "scopes": [
                        {"scope": "project:phoenix", "parent": "project", "aliases": ["phoenix"]}
                    ],
                    "has_more": False,
                    "next_cursor": None,
                    "retrieval_id": "rtv-current-turn",
                }
            ]
        )
        provider = self.lineage_provider(client)
        provider.on_session_switch("token-session")
        provider.on_turn_start(7, {"role": "user", "content": "What changed in Phoenix?"})

        rendered = provider.prefetch("What changed in Phoenix?", session_id="token-session")

        self.assertIn("project:phoenix", rendered)
        self.assertIn("rtv-current-turn", rendered)
        self.assertNotIn("memory_id", rendered)
        self.assertNotIn("title", rendered)
        self.assertNotIn("body", rendered.casefold())
        self.assertEqual(
            client.calls[0],
            (
                "scope_catalog",
                {
                    "limit": provider_module._MAX_SCOPE_ITEMS,
                    "source": "hermes",
                    "session_id": "token-session",
                    "turn_id": "turn-000007-"
                    + provider_module._visible_fingerprint("What changed in Phoenix?"),
                },
            ),
        )

    def test_prefetch_hints_unique_project_scope_without_injecting_memory_data(self) -> None:
        provider = self.provider(
            responses=[
                {
                    "scopes": [
                        {"scope": "project:alpha", "parent": "project", "aliases": ["Alpha"]},
                        {"scope": "project:beta", "parent": "project", "aliases": ["Beta"]},
                    ],
                    "has_more": False,
                    "next_cursor": None,
                }
            ]
        )

        rendered = provider.prefetch("请查询 Alpha 项目部署进展", session_id="scope-hint-session")

        self.assertIn("scope=project:alpha", rendered)
        self.assertIn("unique project scope", rendered)
        self.assertIn("business subject words", rendered)
        self.assertNotIn("memory_id", rendered)
        self.assertNotIn("title", rendered)
        self.assertNotIn("body", rendered.casefold())

    def test_prefetch_does_not_guess_ambiguous_project_scope(self) -> None:
        provider = self.provider(
            responses=[
                {
                    "scopes": [
                        {"scope": "project:alpha", "parent": "project", "aliases": ["shared"]},
                        {"scope": "project:beta", "parent": "project", "aliases": ["shared"]},
                    ],
                    "has_more": False,
                    "next_cursor": None,
                }
            ]
        )

        rendered = provider.prefetch("查询 shared 项目", session_id="ambiguous-scope-session")

        self.assertNotIn("scope=project:alpha", rendered)
        self.assertNotIn("scope=project:beta", rendered)

    def test_compression_rotation_migrates_pending_turn_and_retrieval_token(self) -> None:
        token = "rtv-compression-continuity"
        client = FakeClient(
            responses=[
                {
                    "scopes": [
                        {"scope": "project:alpha", "parent": "project", "aliases": ["alpha"]}
                    ],
                    "has_more": False,
                    "next_cursor": None,
                    "retrieval_id": token,
                },
                {
                    "linked": True,
                    "source": "hermes",
                    "session_id": "new-compression-session",
                    "parent_session_id": "old-compression-session",
                },
                {"stored": True},
                {"stored": True},
                {"processed_turns": 1},
            ]
        )
        provider = self.lineage_provider(client)
        provider.on_session_switch("old-compression-session")
        provider.on_turn_start(4, {"role": "user", "content": "What changed in alpha?"})
        provider.prefetch("What changed in alpha?", session_id="old-compression-session")

        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "search-after-compression",
                        "function": {
                            "name": "mcp__memleaf__search",
                            "arguments": json.dumps(
                                {"query": "alpha changes", "scope": "project:alpha", "retrieval_id": token}
                            ),
                        },
                    },
                    {
                        "id": "read-after-compression",
                        "function": {
                            "name": "mcp__memleaf__read",
                            "arguments": json.dumps(
                                {"memory_id": "mem-alpha", "retrieval_id": token}
                            ),
                        },
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "search-after-compression",
                "content": json.dumps(
                    {
                        "status": "found",
                        "results": [
                            {"memory_id": "mem-alpha", "title": "Alpha"}
                        ],
                    }
                ),
            },
            {
                "role": "tool",
                "tool_call_id": "read-after-compression",
                "content": json.dumps({"memory_id": "mem-alpha", "body": "Alpha details"}),
            },
        ]

        provider.on_session_switch(
            "new-compression-session",
            parent_session_id="old-compression-session",
            reset=False,
            reason="compression",
        )
        with patch.object(provider_module.logger, "info") as info:
            provider.sync_turn(
                "What changed in alpha?",
                "The latest Alpha update is recorded.",
                session_id="new-compression-session",
                messages=messages,
            )

        self.assertEqual(
            provider._canonical_session_id("old-compression-session"),
            "new-compression-session",
        )
        self.assertEqual(provider._last_retrieval_observation, "found")
        capture_calls = [
            arguments for name, arguments in client.calls if name == "capture"
        ]
        self.assertEqual(len(capture_calls), 2)
        self.assertTrue(all(item["session_id"] == "new-compression-session" for item in capture_calls))
        self.assertTrue(all(item["turn_id"].startswith("turn-000004-") for item in capture_calls))
        lineage_calls = [
            arguments for name, arguments in client.calls if name == "session_lineage"
        ]
        self.assertEqual(
            lineage_calls,
            [{
                "source": "hermes",
                "session_id": "new-compression-session",
                "parent_session_id": "old-compression-session",
            }],
        )
        process_calls = [
            arguments for name, arguments in client.calls if name == "process"
        ]
        self.assertEqual(process_calls, [{"source": "hermes", "session_id": "new-compression-session"}])
        self.assertEqual(provider._active_retrieval_ids["new-compression-session"], token)
        self.assertNotIn(("old-compression-session", 4), provider._retrieval_ids_by_turn)
        self.assertEqual(provider._retrieval_ids_by_turn[("new-compression-session", 4)], token)
        output = "\n".join(call.args[0] % call.args[1:] for call in info.call_args_list)
        self.assertIn(
            "retrieval_present=True retrieval_match=True result=ok",
            output,
        )

    def test_failed_compression_lineage_defers_process_until_bounded_retry_succeeds(self) -> None:
        linked = {
            "linked": True,
            "source": "hermes",
            "session_id": "child-session",
            "parent_session_id": "parent-session",
        }
        client = FakeClient(
            responses=[
                RuntimeError("initial lineage failure"),
                RuntimeError("retry lineage failure"),
                {"stored": True},
                {"stored": True},
                linked,
                {"stored": True},
                {"stored": True},
                {"processed_turns": 1},
            ]
        )
        provider = self.lineage_provider(client)
        provider.on_session_switch("parent-session")
        provider.on_session_switch(
            "child-session",
            parent_session_id="parent-session",
            reset=False,
            reason="compression",
        )

        self.assertEqual(self.pending_lineage_head(provider)["attempts"], 1)
        provider.sync_turn("child fact one", "child answer one", session_id="child-session")

        self.assertEqual(self.pending_lineage_head(provider)["attempts"], 2)
        self.assertFalse(any(name == "process" for name, _ in client.calls))
        self.assertEqual(
            [name for name, _ in client.calls if name == "capture"],
            ["capture", "capture"],
        )

        provider.sync_turn("child fact two", "child answer two", session_id="child-session")

        self.assertFalse(provider._pending_lineage)
        self.assertEqual(
            [name for name, _ in client.calls if name == "process"],
            ["process"],
        )
        self.assertEqual(
            [arguments for name, arguments in client.calls if name == "session_lineage"],
            [
                {
                    "source": "hermes",
                    "session_id": "child-session",
                    "parent_session_id": "parent-session",
                }
            ]
            * 3,
        )

    def test_failed_compression_lineage_stops_after_two_retries(self) -> None:
        client = FakeClient(
            responses=[
                RuntimeError("initial lineage failure"),
                RuntimeError("retry one failure"),
                {"stored": True},
                {"stored": True},
                RuntimeError("retry two failure"),
                {"stored": True},
                {"stored": True},
                {"stored": True},
                {"stored": True},
            ]
        )
        provider = self.lineage_provider(client)
        provider.on_session_switch("parent-session")
        provider.on_session_switch(
            "child-session",
            parent_session_id="parent-session",
            reset=False,
            reason="compression",
        )

        for index in range(3):
            provider.sync_turn(
                f"child fact {index}",
                f"child answer {index}",
                session_id="child-session",
            )

        self.assertEqual(
            len([name for name, _ in client.calls if name == "session_lineage"]),
            3,
        )
        self.assertFalse(any(name == "process" for name, _ in client.calls))
        self.assertEqual(self.pending_lineage_head(provider)["attempts"], 3)

    def test_lineage_queue_capacity_preserves_old_links_and_fails_closed(self) -> None:
        provider = self.lineage_provider(auto_process=False)
        for index in range(provider_module._MAX_SESSION_ALIASES):
            self.assertTrue(
                provider._remember_pending_lineage(
                    {
                        "source": "hermes",
                        "session_id": f"child-{index}",
                        "parent_session_id": f"parent-{index}",
                    },
                    1,
                )
            )
        oldest = dict(provider._pending_lineage[0])

        self.assertFalse(
            provider._remember_pending_lineage(
                {
                    "source": "hermes",
                    "session_id": "child-overflow",
                    "parent_session_id": "parent-overflow",
                },
                1,
            )
        )
        self.assertEqual(len(provider._pending_lineage), provider_module._MAX_SESSION_ALIASES)
        self.assertEqual(dict(provider._pending_lineage[0]), oldest)
        self.assertFalse(provider._retry_pending_lineage("child-overflow"))
        self.assertEqual(provider._client.calls, [])

    def test_compression_lineage_chain_retries_parent_before_child_and_processes_after_all_links(self) -> None:
        child_link = self.lineage_link("child-session", "parent-session")
        grandchild_link = self.lineage_link("grandchild-session", "child-session")
        client = FakeClient(
            responses=[
                RuntimeError("initial child lineage failure"),
                child_link,
                RuntimeError("initial grandchild lineage failure"),
                {"stored": True},
                {"stored": True},
                grandchild_link,
                {"stored": True},
                {"stored": True},
                {"processed_turns": 1},
            ]
        )
        provider = self.lineage_provider(client)
        provider.on_session_switch("parent-session")
        provider.on_session_switch(
            "child-session",
            parent_session_id="parent-session",
            reset=False,
            reason="compression",
        )
        provider.on_session_switch(
            "grandchild-session",
            parent_session_id="child-session",
            reset=False,
            reason="compression",
        )

        self.assertEqual(len(provider._pending_lineage), 2)
        self.assertEqual(provider._pending_lineage[0]["session_id"], "child-session")
        self.assertEqual(provider._pending_lineage[1]["session_id"], "grandchild-session")
        self.assertEqual(
            [arguments for name, arguments in client.calls if name == "session_lineage"],
            [
                {
                    "source": "hermes",
                    "session_id": "child-session",
                    "parent_session_id": "parent-session",
                }
            ],
        )

        provider.sync_turn(
            "grandchild fact one",
            "grandchild answer one",
            session_id="grandchild-session",
        )
        self.assertEqual(
            [arguments for name, arguments in client.calls if name == "session_lineage"],
            [
                {
                    "source": "hermes",
                    "session_id": "child-session",
                    "parent_session_id": "parent-session",
                },
                {
                    "source": "hermes",
                    "session_id": "child-session",
                    "parent_session_id": "parent-session",
                },
                {
                    "source": "hermes",
                    "session_id": "grandchild-session",
                    "parent_session_id": "child-session",
                },
            ],
        )
        self.assertEqual(
            [name for name, _ in client.calls if name == "process"],
            [],
        )
        self.assertEqual(provider._pending_lineage[0]["session_id"], "grandchild-session")

        provider.sync_turn(
            "grandchild fact two",
            "grandchild answer two",
            session_id="grandchild-session",
        )
        self.assertFalse(provider._pending_lineage)
        self.assertEqual(
            [arguments for name, arguments in client.calls if name == "session_lineage"],
            [
                {
                    "source": "hermes",
                    "session_id": "child-session",
                    "parent_session_id": "parent-session",
                },
                {
                    "source": "hermes",
                    "session_id": "child-session",
                    "parent_session_id": "parent-session",
                },
                {
                    "source": "hermes",
                    "session_id": "grandchild-session",
                    "parent_session_id": "child-session",
                },
                {
                    "source": "hermes",
                    "session_id": "grandchild-session",
                    "parent_session_id": "child-session",
                },
            ],
        )
        self.assertEqual(
            [name for name, _ in client.calls if name == "process"],
            ["process"],
        )

    def test_incomplete_parent_lineage_never_allows_grandchild_link_or_process(self) -> None:
        client = FakeClient(
            responses=[
                RuntimeError("initial child lineage failure"),
                RuntimeError("child retry failure"),
                {"stored": True},
                {"stored": True},
                RuntimeError("child retry failure after capture"),
                {"stored": True},
                {"stored": True},
            ]
        )
        provider = self.lineage_provider(client)
        provider.on_session_switch("parent-session")
        provider.on_session_switch(
            "child-session",
            parent_session_id="parent-session",
            reset=False,
            reason="compression",
        )
        provider.on_session_switch(
            "grandchild-session",
            parent_session_id="child-session",
            reset=False,
            reason="compression",
        )

        provider.sync_turn(
            "grandchild fact one",
            "grandchild answer one",
            session_id="grandchild-session",
        )
        provider.sync_turn(
            "grandchild fact two",
            "grandchild answer two",
            session_id="grandchild-session",
        )

        lineage_calls = [
            arguments for name, arguments in client.calls if name == "session_lineage"
        ]
        self.assertEqual(
            lineage_calls,
            [
                {
                    "source": "hermes",
                    "session_id": "child-session",
                    "parent_session_id": "parent-session",
                }
            ]
            * 3,
        )
        self.assertEqual(provider._pending_lineage[0]["session_id"], "child-session")
        self.assertEqual(provider._pending_lineage[1]["session_id"], "grandchild-session")
        self.assertFalse(any(name == "process" for name, _ in client.calls))

    def test_lineage_pending_capture_replays_physical_sessions_in_order(self) -> None:
        child_link = self.lineage_link("child-session", "parent-session")
        grandchild_link = self.lineage_link("grandchild-session", "child-session")
        client = FakeClient(
            responses=[
                RuntimeError("initial child lineage failure"),
                RuntimeError("child retry failure"),
                {"stored": True},
                {"stored": True},
                child_link,
                grandchild_link,
                {"stored": True},
                {"stored": True},
                {"processed_turns": 1},
                {"processed_turns": 1},
            ]
        )
        provider = self.lineage_provider(client)
        provider.on_session_switch("parent-session")
        provider.on_session_switch(
            "child-session",
            parent_session_id="parent-session",
            reset=False,
            reason="compression",
        )

        provider.sync_turn(
            "child fact",
            "child answer",
            session_id="child-session",
        )
        self.assertEqual(
            list(provider._deferred_process_sessions),
            ["child-session"],
        )
        self.assertFalse(any(name == "process" for name, _ in client.calls))

        provider.on_session_switch(
            "grandchild-session",
            parent_session_id="child-session",
            reset=False,
            reason="compression",
        )
        provider.sync_turn(
            "grandchild fact",
            "grandchild answer",
            session_id="grandchild-session",
        )

        capture_sessions = [
            arguments["session_id"]
            for name, arguments in client.calls
            if name == "capture"
        ]
        self.assertEqual(
            capture_sessions,
            ["child-session", "child-session", "grandchild-session", "grandchild-session"],
        )
        self.assertEqual(
            [arguments for name, arguments in client.calls if name == "process"],
            [
                {"source": "hermes", "session_id": "child-session"},
                {"source": "hermes", "session_id": "grandchild-session"},
            ],
        )
        self.assertEqual(provider._deferred_process_sessions, {})

    def test_failed_deferred_process_retains_physical_queue_for_retry(self) -> None:
        child_link = self.lineage_link("child-session", "parent-session")
        grandchild_link = self.lineage_link("grandchild-session", "child-session")
        client = FakeClient(
            responses=[
                RuntimeError("initial child lineage failure"),
                RuntimeError("child retry failure"),
                {"stored": True},
                {"stored": True},
                child_link,
                grandchild_link,
                {"stored": True},
                {"stored": True},
                RuntimeError("child process failure"),
                {"stored": True},
                {"stored": True},
                {"processed_turns": 1},
                {"processed_turns": 1},
            ]
        )
        provider = self.lineage_provider(client)
        provider.on_session_switch("parent-session")
        provider.on_session_switch(
            "child-session",
            parent_session_id="parent-session",
            reset=False,
            reason="compression",
        )
        provider.sync_turn("child fact", "child answer", session_id="child-session")
        provider.on_session_switch(
            "grandchild-session",
            parent_session_id="child-session",
            reset=False,
            reason="compression",
        )
        provider.sync_turn(
            "grandchild fact",
            "grandchild answer",
            session_id="grandchild-session",
        )

        self.assertEqual(
            list(provider._deferred_process_sessions),
            ["child-session"],
        )
        self.assertEqual(
            [arguments for name, arguments in client.calls if name == "process"],
            [{"source": "hermes", "session_id": "child-session"}],
        )
        self.assertEqual(
            provider._last_auto_process_failure["session_id"],
            "child-session",
        )

        provider.sync_turn(
            "grandchild retry fact",
            "grandchild retry answer",
            session_id="grandchild-session",
        )
        self.assertEqual(
            [arguments for name, arguments in client.calls if name == "process"],
            [
                {"source": "hermes", "session_id": "child-session"},
                {"source": "hermes", "session_id": "child-session"},
                {"source": "hermes", "session_id": "grandchild-session"},
            ],
        )
        self.assertEqual(provider._deferred_process_sessions, {})

    def test_multi_level_reset_or_rewind_clears_the_entire_alias_component(self) -> None:
        for switch_kwargs in ({"reset": True}, {"rewound": True}):
            with self.subTest(switch_kwargs=switch_kwargs):
                client = FakeClient(
                    responses=[
                        {
                            "linked": True,
                            "source": "hermes",
                            "session_id": "child-session",
                            "parent_session_id": "parent-session",
                        },
                        {
                            "linked": True,
                            "source": "hermes",
                            "session_id": "grandchild-session",
                            "parent_session_id": "child-session",
                        },
                        {
                            "session_id": "grandchild-session",
                            "parent_session_id": None,
                            "cleared": True,
                        },
                    ]
                )
                provider = self.lineage_provider(client, auto_process=False)
                provider.on_session_switch("parent-session")
                provider.on_session_switch(
                    "child-session",
                    parent_session_id="parent-session",
                    reset=False,
                    reason="compression",
                )
                provider.on_session_switch(
                    "grandchild-session",
                    parent_session_id="child-session",
                    reset=False,
                    reason="compression",
                )
                self.assertEqual(
                    provider._canonical_session_id("parent-session"),
                    "grandchild-session",
                )
                self.assertEqual(
                    provider._canonical_session_id("child-session"),
                    "grandchild-session",
                )
                provider._defer_process_session("parent-session")
                provider._defer_process_session("parent-session")
                provider._defer_process_session("child-session")

                provider.on_session_switch("grandchild-session", **switch_kwargs)

                self.assertEqual(provider._session_aliases, {})
                self.assertEqual(provider._deferred_process_sessions, {})
                for session_id in (
                    "parent-session",
                    "child-session",
                    "grandchild-session",
                ):
                    self.assertEqual(provider._canonical_session_id(session_id), session_id)

    def test_pending_compression_lineage_is_cleared_by_new_independent_session(self) -> None:
        for switch_kwargs in (
            {"reset": True},
            {"rewound": True},
            {"reset": False, "rewound": False},
        ):
            with self.subTest(switch_kwargs=switch_kwargs):
                provider = self.lineage_provider(
                    auto_process=False,
                    responses=[RuntimeError("lineage failure")],
                )
                provider.on_session_switch("parent-session")
                provider.on_session_switch(
                    "child-session",
                    parent_session_id="parent-session",
                    reset=False,
                    reason="compression",
                )
                self.assertTrue(provider._pending_lineage)

                provider.on_session_switch("fresh-session", **switch_kwargs)

                if switch_kwargs.get("reset") or switch_kwargs.get("rewound"):
                    pending = self.pending_lineage_head(provider)
                    self.assertEqual(pending["session_id"], "fresh-session")
                    self.assertTrue(pending["reset"])
                else:
                    self.assertFalse(provider._pending_lineage)

    def test_reset_after_compression_drops_lineage_and_retrieval_state(self) -> None:
        provider = self.lineage_provider(
            auto_process=False,
            responses=[
                RuntimeError("compression lineage failure"),
                {"session_id": "fresh-session", "parent_session_id": None, "cleared": False},
            ],
        )
        provider.on_session_switch("old-compression-session")
        provider.on_turn_start(4, {"role": "user", "content": "continuing"})
        provider._retrieval_ids_by_turn[("old-compression-session", 4)] = "rtv-old"
        provider._active_retrieval_ids["old-compression-session"] = "rtv-old"

        provider.on_session_switch(
            "new-compression-session",
            parent_session_id="old-compression-session",
            reset=False,
            reason="compression",
        )
        self.assertEqual(
            provider._canonical_session_id("old-compression-session"),
            "new-compression-session",
        )
        self.assertEqual(len(provider._pending_lineage), 1)

        provider.on_session_switch("fresh-session", reset=True)

        self.assertFalse(provider._pending_lineage)
        self.assertEqual(
            provider._canonical_session_id("old-compression-session"),
            "old-compression-session",
        )
        self.assertEqual(provider._pending_turn_count, 0)
        self.assertNotIn("new-compression-session", provider._active_turn_numbers)
        self.assertNotIn("new-compression-session", provider._active_retrieval_ids)
        self.assertNotIn(("new-compression-session", 4), provider._retrieval_ids_by_turn)
        self.assertNotIn(("new-compression-session", 4), provider._gate_turn_ids)
        self.assertNotIn("old-compression-session", provider._session_aliases)

    def test_failed_reset_lineage_defers_process_until_reset_retry_succeeds(self) -> None:
        client = FakeClient(
            responses=[
                RuntimeError("compression lineage failure"),
                RuntimeError("reset lineage failure"),
                {"session_id": "fresh-session", "parent_session_id": None, "cleared": True},
                {"stored": True},
                {"stored": True},
                {"processed_turns": 1},
            ]
        )
        provider = self.lineage_provider(client)
        provider.on_session_switch("parent-session")
        provider.on_session_switch(
            "child-session",
            parent_session_id="parent-session",
            reset=False,
            reason="compression",
        )
        provider.on_session_switch("fresh-session", reset=True)

        pending = self.pending_lineage_head(provider)
        self.assertEqual(pending["session_id"], "fresh-session")
        self.assertTrue(pending["reset"])

        provider.sync_turn("fresh fact", "fresh answer", session_id="fresh-session")

        self.assertFalse(provider._pending_lineage)
        self.assertEqual(
            [arguments for name, arguments in client.calls if name == "process"],
            [{"source": "hermes", "session_id": "fresh-session"}],
        )

    def test_soft_observer_ignores_previous_turn_and_requires_current_token(self) -> None:
        old_call = {
            "id": "call-old",
            "function": {
                "name": "mcp__memleaf__search",
                "arguments": json.dumps({"query": "old", "retrieval_id": "rtv-old"}),
            },
        }
        current_call = {
            "id": "call-current",
            "function": {
                "name": "mcp__memleaf__search",
                "arguments": json.dumps({"query": "current", "retrieval_id": "rtv-current"}),
            },
        }
        messages = [
            {"role": "assistant", "tool_calls": [old_call]},
            {
                "role": "tool",
                "tool_call_id": "call-old",
                "content": json.dumps({"status": "found", "results": [{"memory_id": "m", "title": "M", "scopes": ["global"]}]}),
            },
            {"role": "assistant", "tool_calls": [current_call]},
            {
                "role": "tool",
                "tool_call_id": "call-current",
                "content": json.dumps({"status": "no_match", "results": [], "has_more": False, "next_cursor": None}),
            },
        ]

        self.assertEqual(
            "no_match",
            provider_module.MemleafMemoryProvider._observe_search_messages(messages, "rtv-current"),
        )
        self.assertEqual(
            "unknown",
            provider_module.MemleafMemoryProvider._observe_search_messages(messages[:2], "rtv-current"),
        )

    def test_search_status_rejects_malformed_v2_envelopes(self) -> None:
        valid = {"memory_id": "mem-1", "title": "Memory"}
        cases = [
            ({"status": "found", "results": []}, "error"),
            ({"status": "no_match", "results": [valid]}, "error"),
            (
                {
                    "status": "found",
                    "results": [dict(valid, scopes=["global"])],
                },
                "error",
            ),
            ({"status": "found", "results": [valid], "error": {"code": "failed"}}, "error"),
            ({"status": "found", "results": [valid]}, "found"),
            ({"status": "no_match", "results": []}, "no_match"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(expected, provider_module._hermes_search_status(value))

    def test_read_diagnostics_cover_tokens_errors_and_responses_wrapper(self) -> None:
        current_token = "rtv-current-read"
        calls = [
            {
                "id": "call-good",
                "function": {
                    "name": "mcp__memleaf__read",
                    "arguments": json.dumps(
                        {"memory_id": "MEMORY_GOOD", "retrieval_id": current_token}
                    ),
                },
            },
            {
                "id": "call-missing-token",
                "function": {
                    "name": "mcp__memleaf__read",
                    "arguments": json.dumps({"memory_id": "MEMORY_MISSING_TOKEN"}),
                },
            },
            {
                "id": "call-wrong-token",
                "function": {
                    "name": "mcp__memleaf__read",
                    "arguments": json.dumps(
                        {"memory_id": "MEMORY_WRONG", "retrieval_id": "rtv-old-read"}
                    ),
                },
            },
            {
                "id": "call-no-result",
                "function": {
                    "name": "mcp__memleaf__read",
                    "arguments": json.dumps(
                        {"memory_id": "MEMORY_NO_RESULT", "retrieval_id": current_token}
                    ),
                },
            },
        ]
        messages = [
            {"role": "assistant", "tool_calls": calls},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "function_call",
                        "call_id": "call-wrapper",
                        "name": "tool_call",
                        "arguments": json.dumps(
                            {
                                "name": "mcp__memleaf__read",
                                "arguments": {
                                    "memory_id": "MEMORY_WRAPPED",
                                    "retrieval_id": current_token,
                                },
                            }
                        ),
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-good",
                "content": json.dumps({"memory_id": "MEMORY_GOOD", "body": "BODY_SECRET"}),
            },
            {
                "role": "tool",
                "tool_call_id": "call-missing-token",
                "content": json.dumps({"memory_id": "MEMORY_MISSING_TOKEN", "body": "BODY_SECRET"}),
            },
            {
                "role": "tool",
                "tool_call_id": "call-wrong-token",
                "content": json.dumps({"error": {"code": "retrieval_id_invalid"}}),
            },
            {
                "type": "function_call_output",
                "call_id": "call-wrapper",
                "output": json.dumps(
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {"memory_id": "MEMORY_WRAPPED", "body": "BODY_SECRET"}
                                ),
                            }
                        ],
                        "isError": False,
                    }
                ),
            },
        ]

        with patch.object(provider_module.logger, "info") as info:
            status = provider_module.MemleafMemoryProvider._observe_search_messages(
                messages,
                current_token,
                session_id="session/diagnostic-secret",
                turn_id="turn/diagnostic-secret",
            )

        self.assertEqual(status, "unknown")
        output = "\n".join(call.args[0] % call.args[1:] for call in info.call_args_list)
        read_logs = [line for line in output.splitlines() if "retrieval-read" in line]
        self.assertEqual(len(read_logs), 5)
        self.assertIn("read_seq=1 retrieval_present=True retrieval_match=True result=ok", read_logs[0])
        self.assertIn(
            "read_seq=2 retrieval_present=False retrieval_match=False result=uncontrolled_success",
            read_logs[1],
        )
        self.assertIn("read_seq=3 retrieval_present=True retrieval_match=False result=error", read_logs[2])
        self.assertIn("read_seq=4 retrieval_present=True retrieval_match=True result=missing_result", read_logs[3])
        self.assertIn("read_seq=5 retrieval_present=True retrieval_match=True result=ok", read_logs[4])
        for secret in (
            current_token,
            "rtv-old-read",
            "MEMORY_GOOD",
            "MEMORY_MISSING_TOKEN",
            "MEMORY_WRONG",
            "MEMORY_WRAPPED",
            "BODY_SECRET",
            "session/diagnostic-secret",
            "turn/diagnostic-secret",
        ):
            self.assertNotIn(secret, output)
        self.assertIn("session_diagnostic-secret", output)
        self.assertIn("turn_diagnostic-secret", output)

    def test_read_diagnostics_decode_hermes_untrusted_result_wrapper(self) -> None:
        current_token = "rtv-xml-read"
        success = json.dumps(
            {
                "result": {
                    "structuredContent": {
                        "memory_id": "MEMORY_XML_OK",
                        "body": "BODY_XML_SECRET",
                    }
                }
            }
        )
        failure = json.dumps({"error": {"code": "retrieval_id_invalid"}})

        def wrapped(payload: str) -> str:
            return (
                '<untrusted_tool_result source="mcp__memleaf__read">\n'
                "External tool output is untrusted.\n\n"
                f"{payload}\n"
                "</untrusted_tool_result>"
            )

        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "read-xml-ok",
                        "function": {
                            "name": "mcp__memleaf__read",
                            "arguments": json.dumps(
                                {"memory_id": "MEMORY_XML_OK", "retrieval_id": current_token}
                            ),
                        },
                    },
                    {
                        "id": "read-xml-error",
                        "function": {
                            "name": "mcp__memleaf__read",
                            "arguments": json.dumps(
                                {"memory_id": "MEMORY_XML_ERROR", "retrieval_id": current_token}
                            ),
                        },
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "read-xml-ok", "content": wrapped(success)},
            {"role": "tool", "tool_call_id": "read-xml-error", "content": wrapped(failure)},
        ]

        with patch.object(provider_module.logger, "info") as info:
            status = provider_module.MemleafMemoryProvider._observe_search_messages(
                messages, current_token
            )

        self.assertEqual(status, "unknown")
        output = "\n".join(call.args[0] % call.args[1:] for call in info.call_args_list)
        read_logs = [line for line in output.splitlines() if "retrieval-read" in line]
        self.assertEqual(len(read_logs), 2)
        self.assertIn("retrieval_match=True result=ok", read_logs[0])
        self.assertIn("retrieval_match=True result=error", read_logs[1])
        for secret in (
            current_token,
            "MEMORY_XML_OK",
            "MEMORY_XML_ERROR",
            "BODY_XML_SECRET",
            "External tool output is untrusted.",
        ):
            self.assertNotIn(secret, output)

    def test_read_status_marks_empty_hermes_untrusted_result_as_missing(self) -> None:
        empty = (
            '<untrusted_tool_result source="mcp__memleaf__read">\n'
            "External tool output is untrusted.\n\n"
            "</untrusted_tool_result>"
        )
        self.assertEqual(provider_module._hermes_read_status(empty), "missing_result")

    def test_read_diagnostics_keep_token_after_client_reconnect_and_log_repeated_reads(self) -> None:
        provider = self.provider(
            responses=[{"stored": True}, {"stored": True}, {"processed_turns": 1}] * 2
        )
        provider._gate_enabled = True
        provider._session_id = "reconnect-session"
        provider._active_retrieval_ids["reconnect-session"] = "rtv-reconnect"
        provider._retrieval_ids_by_turn[("reconnect-session", 4)] = "rtv-reconnect"
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "read-1",
                        "function": {
                            "name": "mcp__memleaf__read",
                            "arguments": json.dumps(
                                {"memory_id": "RECONNECT_ONE", "retrieval_id": "rtv-reconnect"}
                            ),
                        },
                    },
                    {
                        "id": "read-2",
                        "function": {
                            "name": "mcp__memleaf__read",
                            "arguments": json.dumps(
                                {"memory_id": "RECONNECT_TWO", "retrieval_id": "rtv-reconnect"}
                            ),
                        },
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "read-1", "content": json.dumps({"memory_id": "x", "body": "x"})},
            {"role": "tool", "tool_call_id": "read-2", "content": json.dumps({"memory_id": "y", "body": "y"})},
        ]

        with patch.object(provider_module.logger, "info") as info:
            provider.sync_turn(
                "first",
                "answer",
                session_id="reconnect-session",
                messages=messages,
                turn_number=4,
            )
            provider._client = FakeClient(
                responses=[{"stored": True}, {"stored": True}, {"processed_turns": 1}]
            )
            provider.sync_turn(
                "second",
                "answer",
                session_id="reconnect-session",
                messages=messages,
                turn_number=4,
            )

        output = "\n".join(call.args[0] % call.args[1:] for call in info.call_args_list)
        read_logs = [line for line in output.splitlines() if "retrieval-read" in line]
        self.assertEqual(len(read_logs), 4)
        self.assertTrue(all("retrieval_present=True" in line for line in read_logs))
        self.assertTrue(all("retrieval_match=True" in line for line in read_logs))
        self.assertNotIn("rtv-reconnect", output)

    def test_file_tools_only_mark_paths_inside_configured_vault_as_bypass(self) -> None:
        vault = self.root / "configured-vault"
        vault.mkdir()
        (self.hermes_home / "memleaf.json").write_text(
            json.dumps({"vault": str(vault)}), encoding="utf-8"
        )
        provider = self.provider(
            responses=[{"stored": True}, {"stored": True}, {"processed_turns": 1}]
        )
        provider._gate_enabled = True
        provider._session_id = "file-session"
        provider._active_retrieval_ids["file-session"] = "rtv-file"
        vault_path = vault / "knowledge" / "memory.md"
        prefix_path = self.root / "configured-vault-other" / "memory.md"
        wiki_path = self.root / "wiki" / "memory.md"
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "file-vault", "function": {"name": "read_file", "arguments": {"path": str(vault_path)}}},
                    {"id": "file-prefix", "function": {"name": "search_files", "arguments": {"path": str(prefix_path)}}},
                    {"id": "file-wiki", "function": {"name": "mcp__filesystem__read_file", "arguments": {"path": str(wiki_path)}}},
                ],
            }
        ]

        with patch.object(provider_module.logger, "info") as info:
            provider.sync_turn("visible", "answer", session_id="file-session", messages=messages)

        output = "\n".join(call.args[0] % call.args[1:] for call in info.call_args_list)
        bypass_logs = [line for line in output.splitlines() if "file-tool" in line]
        self.assertEqual(len(bypass_logs), 3)
        self.assertEqual(sum("bypass=detected" in line for line in bypass_logs), 1)
        self.assertEqual(sum("bypass=not_detected" in line for line in bypass_logs), 2)
        for path in (str(vault_path), str(prefix_path), str(wiki_path)):
            self.assertNotIn(path, output)

    def test_mcp_requests_use_short_and_process_specific_timeouts(self) -> None:
        client = provider_module._MCPClient("memleaf-mcp", str(self.root / "vault"), 5, 300)
        with patch.object(client, "_start_locked"), patch.object(
            client, "_request_locked", return_value={}
        ) as request:
            client.call_tool("capture", {})
            client.call_tool("process", {})

        self.assertEqual(request.call_args_list[0].kwargs["timeout"], 5)
        self.assertEqual(request.call_args_list[1].kwargs["timeout"], 300)

    def test_mcp_timeout_closes_connection_and_next_call_can_rebuild(self) -> None:
        client = provider_module._MCPClient("memleaf-mcp", str(self.root / "vault"), 5, 300)
        with patch.object(client, "_start_locked") as start, patch.object(
            client, "_close_locked"
        ) as close, patch.object(
            client, "_request_locked", side_effect=[TimeoutError("slow"), {}]
        ):
            with self.assertRaises(TimeoutError):
                client.call_tool("process", {})
            self.assertEqual(client.call_tool("capture", {}), None)

        self.assertEqual(start.call_count, 2)
        close.assert_called_once_with()

    def test_sync_turn_captures_only_explicit_visible_arguments_then_processes_session(self) -> None:
        provider = self.provider(responses=[{"stored": True}, {"duplicate": True}, {"processed_turns": 1}])
        provider.sync_turn(
            "visible user",
            "visible assistant",
            session_id="session-1",
            messages=[
                {"role": "system", "content": "SYSTEM_SECRET"},
                {"role": "tool", "content": "TOOL_SECRET"},
                {"role": "assistant", "content": [{"type": "image", "data": "BINARY_SECRET"}]},
            ],
        )

        calls = provider._client.calls
        self.assertEqual([name for name, _ in calls], ["capture", "capture", "process"])
        self.assertEqual(calls[0][1]["source"], "hermes")
        self.assertEqual(calls[0][1]["session_id"], "session-1")
        self.assertEqual(calls[0][1]["role"], "user")
        self.assertEqual(calls[0][1]["content"], "visible user")
        self.assertEqual(calls[1][1]["role"], "assistant")
        self.assertEqual(calls[1][1]["content"], "visible assistant")
        self.assertEqual(calls[2][1], {"source": "hermes", "session_id": "session-1"})
        self.assertNotIn("SYSTEM_SECRET", json.dumps(calls))
        self.assertNotIn("TOOL_SECRET", json.dumps(calls))
        self.assertNotIn("BINARY_SECRET", json.dumps(calls))

    def test_turn_start_number_and_visible_hash_survive_reconnect_and_duplicates(self) -> None:
        provider = self.provider(auto_process=False, responses=[{"stored": True}] * 8)
        provider.on_session_switch("same-session")
        provider.on_turn_start(6, {"role": "user", "content": "first visible user"})
        provider.sync_turn("first visible user", "first visible assistant", session_id="same-session")
        first_turn_id = provider._client.calls[0][1]["turn_id"]
        self.assertTrue(first_turn_id.startswith("turn-000006-"))

        replacement = FakeClient(responses=[{"stored": True}] * 8)
        with patch.object(provider_module, "_resolve_command", return_value="memleaf-mcp"), patch.object(
            provider_module, "_MCPClient", return_value=replacement
        ):
            provider.initialize("same-session", hermes_home=str(self.hermes_home))
        provider._auto_process = False
        replacement.calls.clear()

        provider.on_turn_start(7, {"role": "user", "content": "second visible user"})
        provider.sync_turn("second visible user", "second visible assistant", session_id="same-session")
        second_turn_id = replacement.calls[0][1]["turn_id"]
        self.assertTrue(second_turn_id.startswith("turn-000007-"))
        self.assertNotEqual(first_turn_id, second_turn_id)

        # A repeated completed callback has no new hook requirement and must
        # reuse the exact same visible-pair id.
        provider.sync_turn("second visible user", "second visible assistant", session_id="same-session")
        self.assertEqual(replacement.calls[2][1]["turn_id"], second_turn_id)

    def test_same_visible_text_with_different_turn_numbers_is_not_merged(self) -> None:
        provider = self.provider(auto_process=False, responses=[{"stored": True}] * 8)
        provider.on_session_switch("same-session")
        provider.on_turn_start(20, "repeated visible user")
        provider.sync_turn("repeated visible user", "same visible assistant", session_id="same-session")
        first_turn_id = provider._client.calls[0][1]["turn_id"]
        provider.on_turn_start(21, "repeated visible user")
        provider.sync_turn("repeated visible user", "same visible assistant", session_id="same-session")
        second_turn_id = provider._client.calls[2][1]["turn_id"]
        self.assertTrue(first_turn_id.startswith("turn-000020-"))
        self.assertTrue(second_turn_id.startswith("turn-000021-"))
        self.assertNotEqual(first_turn_id, second_turn_id)

    def test_session_switch_only_clears_old_queue_on_reset_or_rewind(self) -> None:
        provider = self.provider(auto_process=False, responses=[{"stored": True}] * 10)
        provider.on_turn_start(11, {"role": "user", "content": "resume me"})
        provider.on_session_switch("other-session", reset=False)
        provider.on_session_switch("initialized-session", reset=False)
        provider.sync_turn("resume me", "resumed", session_id="initialized-session")
        self.assertTrue(provider._client.calls[0][1]["turn_id"].startswith("turn-000011-"))

        provider.on_turn_start(12, {"role": "user", "content": "discard me"})
        provider.on_session_switch("reset-session", reset=True)
        provider.on_session_switch("initialized-session", reset=False)
        provider.sync_turn("discard me", "new branch", session_id="initialized-session")
        self.assertTrue(provider._client.calls[2][1]["turn_id"].startswith("turn-fallback-"))

    def test_stage_logs_are_structured_and_do_not_contain_visible_text(self) -> None:
        provider = self.provider(responses=[{"stored": True}, {"stored": True}, {"processed_turns": 1}])
        with patch.object(provider_module.logger, "info") as info:
            provider.sync_turn("VISIBLE_USER_SECRET", "VISIBLE_ASSISTANT_SECRET", session_id="log-session")
        output = "\n".join(call.args[0] % call.args[1:] for call in info.call_args_list)
        for stage in ("capture_user", "capture_assistant", "process"):
            self.assertIn(f"stage={stage}", output)
        self.assertIn("duration_ms=", output)
        self.assertIn("status=ok", output)
        self.assertNotIn("VISIBLE_USER_SECRET", output)
        self.assertNotIn("VISIBLE_ASSISTANT_SECRET", output)

    def test_timeout_stage_log_classifies_failure_without_error_text(self) -> None:
        provider = self.provider(
            responses=[{"stored": True}, {"stored": True}, TimeoutError("TEST_SECRET_TIMEOUT")]
        )
        with patch.object(provider_module.logger, "info") as info:
            provider.sync_turn("visible user", "visible assistant", session_id="timeout-session")
        output = "\n".join(call.args[0] % call.args[1:] for call in info.call_args_list)
        self.assertIn("stage=process", output)
        self.assertIn("error_type=TimeoutError", output)
        self.assertNotIn("TEST_SECRET_TIMEOUT", output)

    def test_structured_mcp_model_error_is_logged_safely_with_code_and_stage(self) -> None:
        provider = self.provider(
            responses=[
                {"stored": True},
                {"stored": True},
                {
                    "isError": True,
                    "structuredContent": {
                        "error": {
                            "code": "model_invalid_response",
                            "message": "MODEL_RESPONSE_SECRET",
                            "stage": "gate",
                            "validation_reason": "schema_violation",
                            "validation_detail": "invalid_type",
                            "attempt_count": 3,
                        }
                    },
                },
            ]
        )
        with patch.object(provider_module.logger, "info") as info:
            provider.sync_turn("visible user", "visible assistant", session_id="safe-error-session")
        output = "\n".join(call.args[0] % call.args[1:] for call in info.call_args_list)
        self.assertIn("stage=process", output)
        self.assertIn("error_type=MCPToolError", output)
        self.assertIn("error_code=model_invalid_response", output)
        self.assertIn("error_stage=gate", output)
        self.assertIn("validation_reason=schema_violation", output)
        self.assertIn("validation_detail=invalid_type", output)
        self.assertIn("attempt_count=3", output)
        self.assertNotIn("MODEL_RESPONSE_SECRET", output)

    def test_unknown_validation_detail_degrades_to_safe_generic(self) -> None:
        value = {
            "isError": True,
            "structuredContent": {
                "error": {
                    "code": "model_invalid_response",
                    "stage": "gate",
                    "validation_reason": "schema_violation",
                    "validation_detail": "MODEL_DETAIL_SECRET",
                }
            },
        }
        fields = provider_module._mcp_error_fields(value)
        self.assertIsNotNone(fields)
        self.assertEqual(fields[4], "other_schema_violation")

    def test_known_update_target_validation_detail_is_preserved(self) -> None:
        value = {
            "isError": True,
            "structuredContent": {
                "error": {
                    "code": "model_invalid_response",
                    "stage": "summarize",
                    "validation_reason": "schema_violation",
                    "validation_detail": "invalid_update_target",
                }
            },
        }
        fields = provider_module._mcp_error_fields(value)
        self.assertIsNotNone(fields)
        self.assertEqual(fields[4], "invalid_update_target")

    def test_duplicate_update_target_validation_detail_is_preserved(self) -> None:
        value = {
            "isError": True,
            "structuredContent": {
                "error": {
                    "code": "model_invalid_response",
                    "stage": "gate",
                    "validation_reason": "schema_violation",
                    "validation_detail": "duplicate_update_target",
                    "attempt_count": 1,
                }
            },
        }
        fields = provider_module._mcp_error_fields(value)
        self.assertIsNotNone(fields)
        self.assertEqual(fields[4], "duplicate_update_target")

    def test_mixed_project_scope_validation_detail_is_preserved(self) -> None:
        value = {
            "isError": True,
            "structuredContent": {
                "error": {
                    "code": "model_invalid_response",
                    "stage": "gate",
                    "validation_reason": "schema_violation",
                    "validation_detail": "mixed_project_scopes",
                    "attempt_count": 3,
                }
            },
        }
        fields = provider_module._mcp_error_fields(value)
        self.assertIsNotNone(fields)
        self.assertEqual(fields[4], "mixed_project_scopes")

    def test_update_target_type_validation_detail_is_preserved(self) -> None:
        value = {
            "isError": True,
            "structuredContent": {
                "error": {
                    "code": "model_invalid_response",
                    "stage": "gate",
                    "validation_reason": "schema_violation",
                    "validation_detail": "update_target_type_mismatch",
                    "attempt_count": 3,
                }
            },
        }
        fields = provider_module._mcp_error_fields(value)
        self.assertIsNotNone(fields)
        self.assertEqual(fields[4], "update_target_type_mismatch")

    def test_relative_time_validation_detail_is_preserved_and_logged(self) -> None:
        value = {
            "isError": True,
            "structuredContent": {
                "error": {
                    "code": "model_invalid_response",
                    "stage": "summarize",
                    "validation_reason": "schema_violation",
                    "validation_detail": "relative_time",
                    "attempt_count": 3,
                }
            },
        }
        fields = provider_module._mcp_error_fields(value)
        self.assertIsNotNone(fields)
        self.assertEqual(fields[4], "relative_time")
        error = provider_module._MCPToolError(
            code="model_invalid_response",
            stage="summarize",
            validation_reason="schema_violation",
            validation_detail="relative_time",
            attempt_count=3,
        )
        self.assertEqual(error.validation_detail, "relative_time")

        provider = self.provider(
            responses=[
                {"stored": True},
                {"stored": True},
                value,
            ]
        )
        with patch.object(provider_module.logger, "info") as info:
            provider.sync_turn("visible user", "visible assistant", session_id="relative-time-session")
        output = "\n".join(call.args[0] % call.args[1:] for call in info.call_args_list)
        self.assertIn("validation_detail=relative_time", output)
        self.assertIn("attempt_count=3", output)

    def test_capture_failure_skips_process_and_does_not_escape_provider(self) -> None:
        provider = self.provider(responses=[{"stored": True}, RuntimeError("capture unavailable")])
        with patch.object(provider_module.logger, "warning") as warning:
            provider.sync_turn("visible user", "visible assistant", session_id="retry-session")
        self.assertEqual([name for name, _ in provider._client.calls], ["capture", "capture"])
        warning.assert_called()

    def test_process_failure_is_warning_only_and_auto_process_can_be_disabled(self) -> None:
        provider = self.provider(responses=[{"stored": True}, {"stored": True}, RuntimeError("model unavailable")])
        with patch.object(provider_module.logger, "warning") as warning:
            provider.sync_turn("visible user", "visible assistant", session_id="failed-session")
        self.assertEqual([name for name, _ in provider._client.calls], ["capture", "capture", "process"])
        warning.assert_called()
        notice = provider.prefetch("what should I do next?", session_id="failed-session").casefold()
        self.assertIn("automatic memleaf processing", notice)
        self.assertIn("failed", notice)
        self.assertIn("do not write or rewrite the vault", notice)
        self.assertIn("terminal", notice)
        self.assertEqual(provider.system_prompt_block().count("Do not use terminal"), 1)

        disabled = self.provider(auto_process=False, responses=[{"stored": True}, {"stored": True}])
        disabled.sync_turn("visible user", "visible assistant", session_id="disabled-session")
        self.assertEqual([name for name, _ in disabled._client.calls], ["capture", "capture"])

    def test_provider_does_not_claim_auto_success_when_process_fails(self) -> None:
        provider = self.provider(
            responses=[
                {"stored": True},
                {"stored": True},
                {
                    "isError": True,
                    "structuredContent": {
                        "error": {
                            "code": "model_invalid_response",
                            "stage": "gate",
                            "validation_reason": "schema_violation",
                            "validation_detail": "root_shape",
                            "attempt_count": 3,
                        }
                    },
                },
            ]
        )
        provider.sync_turn(
            "prepare a durable project update",
            "The automatic extraction failed and must be retried.",
            session_id="failed-model-session",
        )

        # The provider made no direct knowledge/history write and exposes the
        # failure as pending work; only a later successful process can clear it.
        self.assertEqual([name for name, _ in provider._client.calls], ["capture", "capture", "process"])
        status = provider.prefetch("continue", session_id="failed-model-session")
        self.assertIn("model_invalid_response", status)
        self.assertIn("automatic memory extraction has not succeeded", status)
        self.assertNotIn("automatic memory extraction has succeeded", status)

    def test_process_deferred_work_is_visible_without_claiming_full_completion(self) -> None:
        provider = self.provider(
            responses=[
                {"stored": True},
                {"stored": True},
                {"processed_turns": 1, "deferred_candidates": 2, "deferred_inbox_turns": 1},
                {"scopes": [], "has_more": False, "next_cursor": None},
            ]
        )
        provider.sync_turn("a scoped project fact", "a visible project answer", session_id="deferred-session")

        notice = provider.prefetch("follow up", session_id="deferred-session")

        self.assertIn("2 deferred candidate(s)", notice)
        self.assertIn("1 pending inbox turn(s)", notice)
        self.assertIn("not fully complete", notice)
        self.assertNotIn("all captured turn(s) were processed", notice)

    def test_system_prompt_reserves_deliberate_memleaf_mcp_for_explicit_requests(self) -> None:
        provider = self.provider(responses=[])
        prompt = provider.system_prompt_block().casefold()
        self.assertIn("each visible user turn", prompt)
        self.assertIn("ordinary greetings", prompt)
        self.assertIn("explicitly asks", prompt)
        self.assertIn("memleaf mcp", prompt)
        self.assertIn("remember", prompt)
        self.assertIn("forget", prompt)
        self.assertIn("automatic", prompt)
        self.assertIn("directory", prompt)
        self.assertIn("read(memory_id, retrieval_id)", prompt)
        self.assertIn("missing or mismatched", prompt)
        self.assertIn("search_files", prompt)
        self.assertIn("memleaf vault", prompt)
        self.assertIn("ordinary project/wiki files", prompt)
        self.assertIn("titles", prompt)
        self.assertIn("best project/identifier match", prompt)
        self.assertIn("read more only if needed", prompt)
        self.assertIn("do not read all entries to filter unrelated items", prompt)

    def test_core_process_schema_retry_is_single_provider_call(self) -> None:
        """Provider delegates bounded schema retries to the public process API."""

        session_id = "schema-retry-session"
        vault = self.root / "schema-retry-vault"
        service = Memleaf(vault)

        class RetryModel:
            provider = "fake"
            model = "schema-retry"

            def __init__(self):
                self.event_keys = []
                self.gate_calls = 0

            def complete(self, prompt, *, system="", purpose="", temperature=0.0):
                del prompt, system, temperature
                if purpose == "gate":
                    self.gate_calls += 1
                    if self.gate_calls == 1:
                        return '{"candidates":"invalid"}'
                    return '{"candidates":[]}'
                raise AssertionError(f"unexpected model purpose: {purpose}")

        model = RetryModel()
        core = CoreClient(service, model)
        provider = self.provider()
        provider._client = core
        provider.sync_turn("a normal project question", "a normal project answer", session_id=session_id)

        # One public process call owns the retry and the final ledger commit;
        # the provider does not issue a second process/write fallback.
        self.assertEqual([name for name, _ in core.calls], ["capture", "capture", "process"])
        self.assertEqual(model.gate_calls, 2)
        processed = json.loads(service.vault.processed_index_path.read_text(encoding="utf-8"))
        state = processed["sessions"][f"hermes/{session_id}"]
        self.assertEqual(state["watermark"], 1)
        self.assertEqual(state["processing"]["status"], "idle")

    def test_incomplete_visible_turn_is_not_processed(self) -> None:
        provider = self.provider(responses=[])
        provider.sync_turn("visible user", "", session_id="incomplete")
        self.assertEqual(provider._client.calls, [])

    def test_sync_turn_reaches_local_core_knowledge_index_and_retry_boundary(self) -> None:
        session_id = "synthetic_hermes_session"
        vault = self.root / "vault"
        service = Memleaf(vault)
        provider = self.provider()
        success_model = E2EBackend()
        core = CoreClient(service, success_model)
        provider._client = core

        provider.sync_turn(
            "Alice is the main contact for the Phoenix project. Phoenix background: local Markdown memory tracks deployment decisions.",
            "Confirmed: keep Alice as the Phoenix contact and retain the Phoenix project background.",
            session_id=session_id,
            messages=[
                {"role": "system", "content": "SYSTEM_ONLY_SECRET_20260825"},
                {"role": "tool", "content": "TOOL_ONLY_SECRET_20260825"},
                {
                    "role": "assistant",
                    "content": [{"type": "image", "data": "ATTACHMENT_ONLY_SECRET_20260825"}],
                },
            ],
        )

        self.assertEqual([name for name, _ in core.calls], ["capture", "capture", "process"])
        self.assertEqual(core.calls[-1][1], {"source": "hermes", "session_id": session_id})
        memories = service._read_memories_unlocked("knowledge")
        self.assertEqual(len(memories), 1)
        memory = memories[0].memory
        self.assertIn("Alice", memory.body)
        self.assertIn("Phoenix", memory.body)
        self.assertTrue(service.search("Alice"))
        self.assertTrue(service.search("Phoenix"))
        tags_index = json.loads(service.vault.tags_index_path.read_text(encoding="utf-8"))
        self.assertIn(memory.memory_id, tags_index["tags"]["contact"])
        inbox_and_knowledge = "\n".join(
            path.read_text(encoding="utf-8")
            for area in ("inbox", "knowledge")
            for path in service.vault.list_markdown(area)
        )
        for secret in (
            "SYSTEM_ONLY_SECRET_20260825",
            "TOOL_ONLY_SECRET_20260825",
            "ATTACHMENT_ONLY_SECRET_20260825",
        ):
            self.assertNotIn(secret, inbox_and_knowledge)

        processed_path = service.vault.processed_index_path
        processed = json.loads(processed_path.read_text(encoding="utf-8"))
        state = processed["sessions"][f"hermes/{session_id}"]
        self.assertEqual(state["watermark"], 1)

        failing_model = E2EBackend(failing=True)
        failing_core = CoreClient(service, failing_model)
        provider._client = failing_core
        # The provider must absorb the core/model failure while leaving this
        # second turn in the inbox for a later retry.
        provider.sync_turn(
            "Bob is the backup contact for the Phoenix project.",
            "The Phoenix project renewal remains pending.",
            session_id=session_id,
        )
        self.assertEqual([name for name, _ in failing_core.calls], ["capture", "capture", "process"])
        processed_after_failure = json.loads(processed_path.read_text(encoding="utf-8"))
        failed_state = processed_after_failure["sessions"][f"hermes/{session_id}"]
        self.assertEqual(failed_state["watermark"], 1)
        inbox_path = service.vault.inbox_path / "hermes" / f"{session_id}.md"
        self.assertIn("Bob is the backup contact", inbox_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
