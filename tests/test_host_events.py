from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from memleaf.adapters.base import host_event_command, merge_hook_config
from memleaf.budget import MAX_CONTEXT_CHARS, MAX_SCOPE_CATALOG_CHARS, MAX_SCOPE_CATALOG_ITEMS
from memleaf.host_events import (
    _antigravity_event_id,
    _format_context,
    _read_ingest_state,
    _write_ingest_state,
    handle_event,
)
from memleaf.index import event_key
from memleaf.retrieval_gate import find_turn, observe_search
from memleaf.service import Memleaf
from memleaf.vault import Vault


def _row(record_type: str, source: str, step_index: int, content: str, **extra: object) -> dict:
    value = {
        "type": record_type,
        "source": source,
        "status": "DONE",
        "step_index": step_index,
        "content": content,
    }
    value.update(extra)
    return value


class _QueueBackend:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def complete(self, prompt: str, *, system: str = "", purpose: str = "", temperature: float = 0.0) -> str:
        del prompt, system, temperature
        if not self.responses:
            raise RuntimeError("synthetic response queue exhausted")
        return self.responses.pop(0)


class HostEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.vault = Vault(self.root / "vault")
        self.transcript = self.root / "transcript.jsonl"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_rows(self, rows: list[dict]) -> None:
        self.transcript.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
        )

    def append_rows(self, rows: list[dict]) -> None:
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))

    def seed_hook_agent(self, agent: str, status: str, action: str) -> None:
        self.vault.ensure()
        index = json.loads(self.vault.agents_index_path.read_text(encoding="utf-8"))
        index["custom"] = {"preserve": True}
        index["agents"][agent] = {
            "agent": agent,
            "hook_activation_status": status,
            "hook_definition_hash": "definition-hash",
            "user_action_required": True,
            "user_action": action,
            "host_specific": {"preserve": True},
        }
        index["agents"]["other"] = {"sentinel": True}
        self.vault.agents_index_path.write_text(json.dumps(index), encoding="utf-8")

    def hook_agent(self, agent: str) -> dict:
        index = json.loads(self.vault.agents_index_path.read_text(encoding="utf-8"))
        return index["agents"][agent]

    def antigravity_event(self, **extra: object) -> dict:
        value = {
            "conversationId": "conversation-1",
            "transcriptPath": str(self.transcript),
            "workspacePaths": [str(self.root)],
        }
        value.update(extra)
        return value

    def observe_no_match(self, session: str, turn: str) -> None:
        # These tests isolate capture/process behavior after successful gate
        # observation. test_v2_host_flow covers the actual MCP-to-hook path.
        retrieval_id = find_turn(self.vault, "codex", session, turn)
        self.assertIsNotNone(retrieval_id)
        observe_search(self.vault, retrieval_id, "no_match", f"call-{turn}")

    def test_codex_host_event_cli_roundtrips_utf8_scope_map(self) -> None:
        service = Memleaf(self.vault.root)
        service.create_memory(
            memory_id="unicode-scope-memory",
            title="中文项目",
            body="中文正文不应直接注入。",
            scopes=["project:中文项目"],
            tags=["unicode"],
        )
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "unicode-session",
            "turn_id": "unicode-turn",
            "prompt": "继续中文项目。",
            "cwd": str(self.root),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "memleaf",
                "host-event",
                "codex",
                "UserPromptSubmit",
                "--vault",
                str(self.vault.root),
            ],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8", errors="replace"))
        output = json.loads(completed.stdout.decode("utf-8"))
        rendered = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("project:中文项目", rendered)
        self.assertNotIn("中文正文不应直接注入", rendered)

    def test_codex_captures_injects_and_deduplicates(self) -> None:
        catalog = {"scopes": [{"scope": "project:alpha", "parent": None, "aliases": ["Alpha"],
                               "memory_id": "mem-project", "title": "Project memory", "body": "HOOK_BODY_SENTINEL"}],
                   "has_more": False, "next_cursor": None}
        prompt = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "prompt": "Please continue the project.",
            "cwd": str(self.root),
        }
        with patch.object(Memleaf, "scope_catalog", return_value=catalog) as context:
            first = handle_event("codex", prompt, vault=self.vault)
            second = handle_event("codex", prompt, vault=self.vault)
        self.assertNotEqual(first, second)
        self.assertEqual({}, second)
        rendered = first["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("mem-project", rendered)
        self.assertNotIn("Project memory", rendered)
        self.assertIn("project:alpha", rendered)
        self.assertNotIn("HOOK_BODY_SENTINEL", rendered)
        self.assertLessEqual(len(rendered), MAX_SCOPE_CATALOG_CHARS)
        self.assertEqual(1, context.call_count)
        session = self.vault.session_path("codex", "session-1")
        text = session.read_text(encoding="utf-8")
        self.assertEqual(1, text.count("Please continue the project."))
        context.assert_called_once_with(limit=MAX_SCOPE_CATALOG_ITEMS)

        stop = {
            "hook_event_name": "Stop",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "last_assistant_message": "The project is ready.",
        }
        self.observe_no_match("session-1", "turn-1")
        with patch.object(Memleaf, "process", return_value={}) as process:
            handle_event("codex", stop, vault=self.vault)
            handle_event("codex", stop, vault=self.vault)
        self.assertEqual(1, process.call_count)
        self.assertEqual(1, session.read_text(encoding="utf-8").count("The project is ready."))

    def test_codex_hook_becomes_active_only_after_successful_event(self) -> None:
        self.seed_hook_agent("codex", "pending_user_review", "Open /hooks")
        prompt = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-activation",
            "turn_id": "turn-activation",
            "prompt": "Activate this hook.",
        }
        with patch.object(Memleaf, "scope_catalog", return_value={"scopes": [], "has_more": False, "next_cursor": None}):
            self.assertIn("hookSpecificOutput", handle_event("codex", prompt, vault=self.vault))
        agent = self.hook_agent("codex")
        self.assertEqual("active", agent["hook_activation_status"])
        self.assertFalse(agent["user_action_required"])
        self.assertNotIn("user_action", agent)
        self.assertEqual({"preserve": True}, agent["host_specific"])

    def test_codex_hook_failure_does_not_become_active(self) -> None:
        self.seed_hook_agent("codex", "pending_user_review", "Open /hooks")
        prompt = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-failed-activation",
            "turn_id": "turn-failed-activation",
            "prompt": "Retry this hook.",
        }
        with patch.object(Memleaf, "scope_catalog", side_effect=RuntimeError("PRIVATE_EXCEPTION")):
            result = handle_event("codex", prompt, vault=self.vault)
            self.assertIn("systemMessage", result)
            self.assertNotIn("PRIVATE_EXCEPTION", json.dumps(result))
        self.assertEqual("pending_user_review", self.hook_agent("codex")["hook_activation_status"])

    def test_invalid_hook_event_does_not_become_active(self) -> None:
        self.seed_hook_agent("codex", "pending_user_review", "Open /hooks")
        event = {
            "hook_event_name": "Unknown",
            "session_id": "session-invalid",
            "turn_id": "turn-invalid",
            "prompt": "Do not process this.",
        }
        self.assertEqual({}, handle_event("codex", event, vault=self.vault))
        self.assertEqual("pending_user_review", self.hook_agent("codex")["hook_activation_status"])

        self.seed_hook_agent("antigravity", "pending_restart", "Quit and reopen Antigravity")
        self.assertEqual(
            {"decision": "stop"},
            handle_event("antigravity", {}, vault=self.vault, event_name="Stop"),
        )
        self.assertEqual("pending_restart", self.hook_agent("antigravity")["hook_activation_status"])

    def test_codex_failed_process_is_retryable_without_blocking_stop(self) -> None:
        prompt = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-2",
            "turn_id": "turn-2",
            "prompt": "Remember this decision.",
        }
        handle_event("codex", prompt, vault=self.vault)
        self.observe_no_match("session-2", "turn-2")
        stop = {
            "hook_event_name": "Stop",
            "session_id": "session-2",
            "turn_id": "turn-2",
            "last_assistant_message": "Decision recorded.",
        }
        with patch.object(Memleaf, "process", side_effect=RuntimeError("model unavailable")):
            failed = handle_event("codex", stop, vault=self.vault)
            self.assertNotIn("decision", failed)
            self.assertIn("failed", failed["systemMessage"].lower())
            self.assertNotIn("model unavailable", str(failed))
        state = json.loads(self.vault.host_ingest_path.read_text(encoding="utf-8"))
        self.assertTrue(state["codex"]["session-2"]["process_pending"])
        with patch.object(Memleaf, "process", return_value={}) as process:
            self.assertEqual({}, handle_event("codex", stop, vault=self.vault))
        self.assertEqual(1, process.call_count)
        state = json.loads(self.vault.host_ingest_path.read_text(encoding="utf-8"))
        self.assertFalse(state["codex"]["session-2"]["process_pending"])

    def test_codex_empty_scope_map_still_requires_search_and_is_consumed_once(self) -> None:
        prompt = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-empty",
            "turn_id": "turn-empty",
            "prompt": "No related memory",
        }
        with patch.object(Memleaf, "scope_catalog", return_value={"scopes": [], "has_more": False, "next_cursor": None}) as context:
            first = handle_event("codex", prompt, vault=self.vault)
            self.assertIn("search", first["hookSpecificOutput"]["additionalContext"])
            self.assertEqual({}, handle_event("codex", prompt, vault=self.vault))
        self.assertEqual(1, context.call_count)
        state = json.loads(self.vault.host_ingest_path.read_text(encoding="utf-8"))
        self.assertIn("turn-empty", state["codex"]["session-empty"]["injected_turn_ids"])

    def test_codex_scope_map_rendering_is_bounded_and_replay_safe(self) -> None:
        prompt = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-bounded",
            "turn_id": "turn-bounded",
            "prompt": "Find the relevant decision.",
        }
        catalog = {
            "scopes": [{"scope": f"project:bounded-{index}", "parent": None, "aliases": []}
                       for index in range(MAX_SCOPE_CATALOG_ITEMS)],
            "has_more": True, "next_cursor": "catalog-next-page",
        }
        with patch.object(Memleaf, "scope_catalog", return_value=catalog) as context:
            first = handle_event("codex", prompt, vault=self.vault)
            second = handle_event("codex", prompt, vault=self.vault)

        rendered = first["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(MAX_SCOPE_CATALOG_ITEMS, rendered.count("\n- "))
        self.assertLessEqual(len(rendered), MAX_SCOPE_CATALOG_CHARS)
        self.assertNotIn("HOOK_BODY_SENTINEL", rendered)
        self.assertIn("catalog-next-page", rendered)
        self.assertIn("project:bounded-19", rendered)
        self.assertEqual({}, second)
        context.assert_called_once_with(limit=MAX_SCOPE_CATALOG_ITEMS)

    def test_context_renderer_skips_oversized_items_without_truncating_later_items(self) -> None:
        memories = [
            SimpleNamespace(
                memory_id="mem-large",
                title="Too large",
                scopes=["project:large"],
                body="HOOK_BODY_SENTINEL_LARGE",
            ),
            SimpleNamespace(
                memory_id="mem-small",
                title="中文记忆",
                scopes=["domain:memory"],
                body="HOOK_BODY_SENTINEL_SMALL",
            ),
        ]

        rendered = _format_context(memories)

        self.assertIn("mem-large", rendered)
        self.assertIn("Too large", rendered)
        self.assertIn("mem-small", rendered)
        self.assertIn("中文记忆", rendered)
        self.assertIn("domain:memory", rendered)
        self.assertIn("best project/identifier match", rendered)
        self.assertIn("read more only if needed", rendered)
        self.assertIn("do not read all entries to filter unrelated items", rendered)
        self.assertNotIn("HOOK_BODY_SENTINEL", rendered)
        self.assertLessEqual(len(rendered), MAX_CONTEXT_CHARS)

    def test_codex_stop_processes_memory_but_next_scope_map_does_not_inject_it(self) -> None:
        user_event = "codex/session-3/turn-3/user"
        user_key = event_key(user_event)
        backend = _QueueBackend(
            [
                json.dumps(
                    {
                        "candidates": [
                            {
                                "candidate_id": "candidate-1",
                                "memory": "The project uses a local memory vault.",
                                "evidence_event_ids": [user_key],
                                "duplicate": False,
                                "worth": True,
                                "type": "fact",
                                "scopes": ["global"],
                                "scope_source": "model",
                            }
                        ]
                    }
                ),
                json.dumps(
                    {
                        "title": "Memory design",
                        "body": "The project uses a local memory vault.",
                        "tags": ["memory"],
                        "type": "fact",
                        "scopes": ["global"],
                        "scope_source": "model",
                        "sources": [
                            {
                                "event_key": user_key,
                                "session_id": "session-3",
                                "turn_id": "turn-3",
                                "conversation_title": "codex/session-3",
                            }
                        ],
                    }
                ),
            ]
        )

        class RoutedMemleaf(Memleaf):
            def __init__(self, vault: Vault) -> None:
                super().__init__(vault, model=backend)

        prompt = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-3",
            "turn_id": "turn-3",
            "prompt": "Use a local memory vault.",
        }
        stop = {
            "hook_event_name": "Stop",
            "session_id": "session-3",
            "turn_id": "turn-3",
            "last_assistant_message": "I will use the local memory vault.",
        }
        with patch("memleaf.host_events.Memleaf", RoutedMemleaf):
            self.assertIn("hookSpecificOutput", handle_event("codex", prompt, vault=self.vault))
            self.observe_no_match("session-3", "turn-3")
            self.assertEqual({}, handle_event("codex", stop, vault=self.vault))
            injected = handle_event(
                "codex",
                {**prompt, "turn_id": "turn-4", "prompt": "memory vault"},
                vault=self.vault,
            )
        self.assertNotIn("Memory design", injected["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("The project uses", injected["hookSpecificOutput"]["additionalContext"])
        self.assertEqual("Memory design", Memleaf(self.vault).search_candidates("memory vault")["results"][0]["title"])
        self.assertEqual(1, len(list(self.vault.knowledge_path.glob("*.md"))))

    def test_antigravity_stop_whitelists_visible_records(self) -> None:
        self.write_rows(
            [
                _row("SYSTEM_MESSAGE", "SYSTEM", 0, "do not store"),
                _row("USER_INPUT", "USER_EXPLICIT", 1, "Visible question"),
                _row(
                    "PLANNER_RESPONSE",
                    "MODEL",
                    2,
                    "Hidden tool thought",
                    tool_calls=[{"name": "private_tool", "arguments": {"secret": "x"}}],
                ),
                _row("CONVERSATION_HISTORY", "SYSTEM", 3, "old context"),
                _row("PLANNER_RESPONSE", "MODEL", 4, "Visible answer"),
            ]
        )
        with patch.object(Memleaf, "process", return_value={}) as process:
            self.assertEqual(
                {"decision": "stop"},
                handle_event(
                    "antigravity",
                    self.antigravity_event(terminationReason="model_stop", fullyIdle=True),
                    vault=self.vault,
                    event_name="Stop",
                ),
            )
        self.assertEqual(1, process.call_count)
        text = self.vault.session_path("antigravity", "conversation-1").read_text(encoding="utf-8")
        self.assertIn("Visible question", text)
        self.assertIn("Visible answer", text)
        self.assertNotIn("Hidden tool thought", text)
        self.assertNotIn("private_tool", text)
        self.assertNotIn("old context", text)

    def test_antigravity_hook_becomes_active_after_successful_pre_invocation(self) -> None:
        self.seed_hook_agent("antigravity", "pending_restart", "Quit and reopen Antigravity")
        self.write_rows([_row("USER_INPUT", "USER_EXPLICIT", 1, "Visible question")])
        with patch.object(Memleaf, "context", return_value=[]):
            self.assertEqual(
                {},
                handle_event(
                    "antigravity",
                    self.antigravity_event(invocationNum=1),
                    vault=self.vault,
                    event_name="PreInvocation",
                ),
            )
        agent = self.hook_agent("antigravity")
        self.assertEqual("active", agent["hook_activation_status"])
        self.assertFalse(agent["user_action_required"])
        self.assertNotIn("user_action", agent)
        self.assertEqual({"sentinel": True}, self.hook_agent("other"))

    def test_antigravity_hook_failure_does_not_become_active(self) -> None:
        self.seed_hook_agent("antigravity", "pending_restart", "Quit and reopen Antigravity")
        self.write_rows([_row("USER_INPUT", "USER_EXPLICIT", 1, "Retry context")])
        with patch.object(Memleaf, "context", side_effect=RuntimeError("temporary")):
            self.assertEqual(
                {},
                handle_event(
                    "antigravity",
                    self.antigravity_event(invocationNum=1),
                    vault=self.vault,
                    event_name="PreInvocation",
                ),
            )
        self.assertEqual("pending_restart", self.hook_agent("antigravity")["hook_activation_status"])

    def test_antigravity_keeps_content_when_thinking_is_separate(self) -> None:
        self.write_rows(
            [
                _row("USER_INPUT", "USER_EXPLICIT", 1, "Visible question"),
                _row(
                    "PLANNER_RESPONSE",
                    "MODEL",
                    2,
                    "Visible answer",
                    thinking="private reasoning",
                ),
            ]
        )
        with patch.object(Memleaf, "process", return_value={}) as process:
            handle_event(
                "antigravity",
                self.antigravity_event(terminationReason="model_stop", fullyIdle=True),
                vault=self.vault,
                event_name="Stop",
            )
        self.assertEqual(1, process.call_count)
        text = self.vault.session_path("antigravity", "conversation-1").read_text(encoding="utf-8")
        self.assertIn("Visible answer", text)
        self.assertNotIn("private reasoning", text)

    def test_antigravity_consecutive_users_share_one_turn_and_process(self) -> None:
        self.write_rows(
            [
                _row("USER_INPUT", "USER_EXPLICIT", 1, "First part"),
                _row("USER_INPUT", "USER_EXPLICIT", 2, "Second part"),
            ]
        )
        with patch.object(
            Memleaf,
            "context",
            return_value=[SimpleNamespace(memory_id="mem-t", title="T", scopes=["project:test"], body="B")],
        ):
            injected = handle_event(
                "antigravity",
                self.antigravity_event(invocationNum=1),
                vault=self.vault,
                event_name="PreInvocation",
            )
        self.assertIn("mem-t", injected["injectSteps"][0]["ephemeralMessage"])
        self.assertNotIn("B", injected["injectSteps"][0]["ephemeralMessage"])
        self.append_rows([_row("PLANNER_RESPONSE", "MODEL", 3, "One final answer")])
        with patch.object(Memleaf, "process", return_value={}) as process:
            handle_event(
                "antigravity",
                self.antigravity_event(terminationReason="model_stop", fullyIdle=True),
                vault=self.vault,
                event_name="Stop",
            )
        self.assertEqual(1, process.call_count)
        text = self.vault.session_path("antigravity", "conversation-1").read_text(encoding="utf-8")
        self.assertEqual(2, text.count('"role":"user"'))
        self.assertEqual(1, text.count('"role":"assistant"'))
        from memleaf.inbox import parse_inbox

        turns = parse_inbox(self.vault)
        self.assertEqual(1, len(turns))
        self.assertTrue(turns[0].complete)
        self.assertEqual(["First part", "Second part", "One final answer"], [event.content for event in turns[0].events])

    def test_antigravity_grouped_turn_reaches_core_process(self) -> None:
        self.write_rows(
            [
                _row("USER_INPUT", "USER_EXPLICIT", 1, "First part"),
                _row("USER_INPUT", "USER_EXPLICIT", 2, "Second part"),
                _row("PLANNER_RESPONSE", "MODEL", 3, "One final answer"),
            ]
        )
        user_key = event_key(_antigravity_event_id("conversation-1", 1, "user"))
        backend = _QueueBackend(
            [
                json.dumps(
                    {
                        "candidates": [
                            {
                                "candidate_id": "candidate-1",
                                "memory": "The two visible requests belong to one turn.",
                                "evidence_event_ids": [user_key],
                                "duplicate": False,
                                "worth": True,
                                "type": "fact",
                                "scopes": ["global"],
                                "scope_source": "model",
                            }
                        ]
                    }
                ),
                json.dumps(
                    {
                        "title": "Grouped turn",
                        "body": "The two visible requests belong to one turn.",
                        "tags": ["turn"],
                        "type": "fact",
                        "scopes": ["global"],
                        "scope_source": "model",
                        "sources": [{"event_key": user_key}],
                    }
                ),
            ]
        )

        class RoutedMemleaf(Memleaf):
            def __init__(self, vault: Vault) -> None:
                super().__init__(vault, model=backend)

        with patch("memleaf.host_events.Memleaf", RoutedMemleaf):
            handle_event(
                "antigravity",
                self.antigravity_event(terminationReason="model_stop", fullyIdle=True),
                vault=self.vault,
                event_name="Stop",
            )
        self.assertEqual(1, len(list(self.vault.knowledge_path.glob("*.md"))))

    def test_antigravity_pre_and_stop_support_continuous_users(self) -> None:
        self.write_rows([_row("USER_INPUT", "USER_EXPLICIT", 1, "First question")])
        memory = SimpleNamespace(
            memory_id="mem-earlier",
            title="Earlier decision",
            scopes=["project:memory"],
            body="Use the local vault",
        )
        with patch.object(Memleaf, "context", return_value=[memory]) as context:
            first = handle_event(
                "antigravity",
                self.antigravity_event(invocationNum=1),
                vault=self.vault,
                event_name="PreInvocation",
            )
            duplicate = handle_event(
                "antigravity",
                self.antigravity_event(invocationNum=1),
                vault=self.vault,
                event_name="PreInvocation",
            )
        self.assertIn("mem-earlier", first["injectSteps"][0]["ephemeralMessage"])
        self.assertNotIn("Use the local vault", first["injectSteps"][0]["ephemeralMessage"])
        self.assertEqual({}, duplicate)
        self.assertEqual(1, context.call_count)

        self.append_rows(
            [
                _row("PLANNER_RESPONSE", "MODEL", 2, "First answer"),
                _row("USER_INPUT", "USER_EXPLICIT", 3, "Second question"),
            ]
        )
        with patch.object(Memleaf, "context", return_value=[memory]):
            second = handle_event(
                "antigravity",
                self.antigravity_event(invocationNum=2),
                vault=self.vault,
                event_name="PreInvocation",
            )
        self.assertIn("Earlier decision", second["injectSteps"][0]["ephemeralMessage"])
        self.append_rows([_row("PLANNER_RESPONSE", "MODEL", 4, "Second answer")])

        with patch.object(Memleaf, "process", return_value={}) as process:
            handle_event(
                "antigravity",
                self.antigravity_event(terminationReason="model_stop", fullyIdle=True),
                vault=self.vault,
                event_name="Stop",
            )
            handle_event(
                "antigravity",
                self.antigravity_event(terminationReason="model_stop", fullyIdle=True),
                vault=self.vault,
                event_name="Stop",
            )
        self.assertEqual(1, process.call_count)
        text = self.vault.session_path("antigravity", "conversation-1").read_text(encoding="utf-8")
        self.assertEqual(2, text.count('"role":"user"'))
        self.assertEqual(2, text.count('"role":"assistant"'))
        self.assertEqual(1, text.count("First question"))
        self.assertEqual(1, text.count("Second question"))

    def test_antigravity_context_failure_does_not_consume_injection(self) -> None:
        self.write_rows([_row("USER_INPUT", "USER_EXPLICIT", 1, "Retry context")])
        event = self.antigravity_event(invocationNum=1)
        with patch.object(
            Memleaf,
            "context",
            side_effect=[
                RuntimeError("temporary"),
                [SimpleNamespace(memory_id="mem-t", title="T", scopes=["project:test"], body="B")],
            ],
        ) as context:
            self.assertEqual({}, handle_event("antigravity", event, vault=self.vault, event_name="PreInvocation"))
            result = handle_event("antigravity", event, vault=self.vault, event_name="PreInvocation")
        self.assertIn("mem-t", result["injectSteps"][0]["ephemeralMessage"])
        self.assertNotIn("B", result["injectSteps"][0]["ephemeralMessage"])
        self.assertEqual(2, context.call_count)

    def test_antigravity_abnormal_stop_does_not_process_and_schema_drift_does_not_advance(self) -> None:
        self.write_rows([_row("USER_INPUT", "USER_EXPLICIT", 1, "Keep this input")])
        handle_event(
            "antigravity",
            self.antigravity_event(invocationNum=1),
            vault=self.vault,
            event_name="PreInvocation",
        )
        with patch.object(Memleaf, "process", return_value={}) as process:
            handle_event(
                "antigravity",
                self.antigravity_event(terminationReason="user_cancelled", fullyIdle=True),
                vault=self.vault,
                event_name="Stop",
            )
        self.assertEqual(0, process.call_count)
        self.assertIn("Keep this input", self.vault.session_path("antigravity", "conversation-1").read_text(encoding="utf-8"))

        drifted = self.root / "drifted.jsonl"
        drifted.write_text(
            json.dumps({"type": "USER_INPUT", "source": "USER_EXPLICIT", "status": "DONE"}) + "\n",
            encoding="utf-8",
        )
        event = self.antigravity_event(transcriptPath=str(drifted), conversationId="drifted")
        self.assertEqual({}, handle_event("antigravity", event, vault=self.vault, event_name="PreInvocation"))
        state = json.loads(self.vault.host_ingest_path.read_text(encoding="utf-8"))
        self.assertNotIn("drifted", state["transcripts"])

    def test_antigravity_stop_always_returns_required_decision(self) -> None:
        cases = [
            (self.antigravity_event(terminationReason="user_cancelled", fullyIdle=True), "Stop"),
            (self.antigravity_event(terminationReason="model_stop"), "Stop"),
            ({}, "Stop"),
            (self.antigravity_event(transcriptPath=str(self.root / "missing.jsonl")), "Stop"),
        ]
        for event, event_name in cases:
            with self.subTest(event=event, event_name=event_name):
                self.assertEqual(
                    {"decision": "stop"},
                    handle_event("antigravity", event, vault=self.vault, event_name=event_name),
                )

    def test_antigravity_stop_process_failure_still_allows_host_to_stop(self) -> None:
        self.write_rows(
            [
                _row("USER_INPUT", "USER_EXPLICIT", 1, "Visible question"),
                _row("PLANNER_RESPONSE", "MODEL", 2, "Visible answer"),
            ]
        )
        with patch.object(Memleaf, "process", side_effect=RuntimeError("model unavailable")):
            result = handle_event(
                "antigravity",
                self.antigravity_event(terminationReason="model_stop", fullyIdle=True),
                vault=self.vault,
                event_name="Stop",
            )
        self.assertEqual({"decision": "stop"}, result)

    def test_antigravity_stop_top_level_failure_returns_safe_response(self) -> None:
        with patch("memleaf.host_events._coerce_vault", side_effect=RuntimeError("unexpected")):
            result = handle_event(
                "antigravity",
                self.antigravity_event(terminationReason="model_stop", fullyIdle=True),
                vault=self.vault,
                event_name="Stop",
            )
        self.assertEqual({"decision": "stop"}, result)

    def test_hook_config_merge_is_idempotent_and_preserves_existing_document(self) -> None:
        path = self.root / "hooks.json"
        path.write_text(json.dumps({"custom": True}), encoding="utf-8")
        additions = {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "memleaf host-event codex --vault /tmp/vault",
                            "timeout": 600,
                        }
                    ]
                }
            ]
        }
        first = merge_hook_config(path, additions, container_key="hooks")
        second = merge_hook_config(path, additions, container_key="hooks")
        self.assertEqual("configured", first.status)
        self.assertEqual("already_configured", second.status)
        self.assertTrue(first.changed)
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(document["custom"])
        self.assertEqual(1, len(document["hooks"]["Stop"]))
        self.assertIsNotNone(first.backup_path)

    def test_disabled_named_hook_is_not_silently_reactivated(self) -> None:
        path = self.root / "antigravity-hooks.json"
        path.write_text(json.dumps({"memleaf": {"enabled": False}}), encoding="utf-8")
        result = merge_hook_config(
            path,
            {"Stop": [{"type": "command", "command": "memleaf host-event antigravity Stop --vault /tmp/vault"}]},
            container_key="memleaf",
        )
        self.assertEqual("diagnostic", result.status)
        self.assertEqual({"memleaf": {"enabled": False}}, json.loads(path.read_text(encoding="utf-8")))

    def test_hook_parent_symlink_is_not_followed(self) -> None:
        external = self.root / "external"
        external.mkdir()
        parent = self.root / "codex"
        parent.symlink_to(external, target_is_directory=True)
        result = merge_hook_config(
            parent / "hooks.json",
            {"Stop": [{"hooks": [{"type": "command", "command": "memleaf host-event codex Stop"}]}]},
            container_key="hooks",
        )
        self.assertEqual("diagnostic", result.status)
        self.assertFalse((external / "hooks.json").exists())

    def test_host_event_command_quotes_interpreter_and_vault(self) -> None:
        interpreter = self.root / "venv with space" / "bin" / "python"
        vault = self.root / "vault with space"
        command = host_event_command("codex", "Stop", vault, interpreter=interpreter)
        self.assertEqual(str(interpreter), shlex.split(command)[0])
        self.assertIn("-m memleaf.cli host-event codex Stop", command)
        self.assertIn("vault with space", shlex.split(command)[-1])

    def test_host_ingest_state_write_merges_interleaved_sessions(self) -> None:
        states = [
            {
                "version": 1,
                "codex": {"session-a": {"process_pending": True}},
                "transcripts": {"transcript-a": {"capture_offset": 1}},
            },
            {
                "version": 1,
                "codex": {"session-b": {"process_pending": False}},
                "transcripts": {"transcript-b": {"capture_offset": 2}},
            },
        ]
        barrier = threading.Barrier(2)

        def writer(value: dict, session: str, transcript: str) -> None:
            barrier.wait()
            for _ in range(8):
                _write_ingest_state(self.vault, value, codex_session=session)
                _write_ingest_state(self.vault, value, transcript_key=transcript)

        threads = [
            threading.Thread(target=writer, args=(states[0], "session-a", "transcript-a")),
            threading.Thread(target=writer, args=(states[1], "session-b", "transcript-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        state = _read_ingest_state(self.vault)
        self.assertIn("session-a", state["codex"])
        self.assertIn("session-b", state["codex"])
        self.assertIn("transcript-a", state["transcripts"])
        self.assertIn("transcript-b", state["transcripts"])


if __name__ == "__main__":
    unittest.main()
