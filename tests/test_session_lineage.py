"""Compression session lineage keeps processing scope without merging sessions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.inbox import parse_inbox
from memleaf.index import event_key
from tests.test_stage_c1_mcp import MCPProcess
from tests.test_hermes_provider import provider_module


class QueueBackend:
    provider = "fake"
    model = "session-lineage-test"

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        del system, temperature
        self.calls.append({"prompt": prompt, "purpose": purpose})
        if not self.responses:
            raise AssertionError("test model response queue exhausted")
        return self.responses.pop(0)


def gate(candidates):
    return json.dumps({"candidates": candidates}, ensure_ascii=False)


def candidate(candidate_id, evidence, memory, *, scopes, update_memory_id=None):
    value = {
        "candidate_id": candidate_id,
        "memory": memory,
        "evidence_event_ids": list(evidence),
        "duplicate": False,
        "worth": True,
        "type": "project",
        "scopes": list(scopes),
        "scope_source": "model",
    }
    if update_memory_id is not None:
        value["update_memory_id"] = update_memory_id
    return value


def summary(event, body, *, title, scopes, update_memory_id=None):
    value = {
        "title": title,
        "body": body,
        "tags": ["session-lineage"],
        "type": "project",
        "scopes": list(scopes),
        "scope_source": "model",
        "sources": [{"event_key": event}],
    }
    if update_memory_id is not None:
        value["update_memory_id"] = update_memory_id
    return json.dumps(value, ensure_ascii=False)


class SessionLineageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="memleaf-session-lineage-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service = Memleaf(Path(temporary.name) / "vault")

    def capture(self, session, turn, user, assistant):
        user_event = f"{session}/{turn}/user"
        assistant_event = f"{session}/{turn}/assistant"
        self.service.capture("hermes", session, turn, "user", user, event_id=user_event)
        self.service.capture("hermes", session, turn, "assistant", assistant, event_id=assistant_event)
        return event_key(user_event), event_key(assistant_event)

    def processed(self):
        return json.loads(self.service.vault.processed_index_path.read_text(encoding="utf-8"))

    def _real_hermes_provider(self):
        hermes_home = self.root / "hermes"
        hermes_home.mkdir()
        if os.name == "nt":
            installed = shutil.which("memleaf-mcp")
            if not installed:
                raise AssertionError("installed memleaf-mcp entry point is required on Windows")
            command = Path(installed)
        else:
            command = self.root / "memleaf-mcp-wrapper"
            command.write_text(
                f"#!{sys.executable}\n"
                "import sys\n"
                f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r})\n"
                "from memleaf.mcp_server import main\n"
                "raise SystemExit(main())\n",
                encoding="utf-8",
            )
            command.chmod(0o755)
        (hermes_home / "memleaf.json").write_text(
            json.dumps(
                {
                    "command": str(command),
                    "vault": str(self.service.vault.root),
                    "timeout": 5,
                    "process_timeout": 5,
                    "auto_process": False,
                }
            ),
            encoding="utf-8",
        )
        provider = provider_module.MemleafMemoryProvider()
        self.addCleanup(provider.shutdown)
        provider.initialize(
            "parent-session",
            hermes_home=str(hermes_home),
            platform="cli",
            agent_context="primary",
        )
        self.assertTrue(provider._gate_enabled)
        self.assertEqual(provider._client.__class__.__name__, "_MCPClient")
        return provider

    def _provider_capture(self, provider, session_id, turn_number, user, assistant):
        provider.on_turn_start(turn_number, {"role": "user", "content": user})
        provider.sync_turn(user, assistant, session_id=session_id, messages=[])
        turn = next(
            turn
            for turn in parse_inbox(self.service.vault)
            if turn.source == "hermes" and turn.session_id == session_id
        )
        self.assertTrue(turn.complete)
        return turn.event_keys

    def test_compression_child_inherits_parent_scope_and_updates_same_memory(self):
        parent_user, parent_assistant = self.capture(
            "parent-session",
            "turn-1",
            "alpha 项目采用达梦数据库，负责人是甲。",
            "已确认 alpha 项目的技术路线和负责人。",
        )
        backend = QueueBackend(
            [
                gate(
                    [
                        candidate(
                            "alpha-owner",
                            [parent_user, parent_assistant],
                            "alpha 项目的负责人是甲。",
                            scopes=["project:alpha"],
                        )
                    ]
                ),
                summary(
                    parent_user,
                    "alpha 项目采用达梦数据库，负责人是甲。",
                    title="alpha 项目负责人",
                    scopes=["project:alpha"],
                ),
            ]
        )
        self.service.process(source="hermes", session_id="parent-session", model=backend)
        old = self.service._read_memories_unlocked("knowledge")[0].memory
        self.assertEqual(
            self.processed()["sessions"]["hermes/parent-session"]["scopes"],
            ["project:alpha"],
        )

        linked = self.service.session_lineage(
            "hermes", "child-session", parent_session_id="parent-session"
        )
        self.assertEqual(linked["parent_session_id"], "parent-session")
        child_state = self.processed()["sessions"]["hermes/child-session"]
        self.assertEqual(child_state["lineage_parent_session_id"], "parent-session")
        self.assertNotIn("scopes", child_state)

        child_user, child_assistant = self.capture(
            "child-session",
            "turn-1",
            "这个项目的负责人更新为乙。",
            "已确认负责人变更为乙。",
        )
        backend.responses.extend(
            [
                gate(
                    [
                        candidate(
                            "alpha-owner-update",
                            [child_user, child_assistant],
                            "alpha 项目的负责人更新为乙。",
                            scopes=["project:alpha"],
                            update_memory_id=old.memory_id,
                        )
                    ]
                ),
                summary(
                    child_user,
                    "alpha 项目采用达梦数据库，负责人已更新为乙。",
                    title="alpha 项目负责人",
                    scopes=["project:alpha"],
                    update_memory_id=old.memory_id,
                ),
            ]
        )
        result = self.service.process(source="hermes", session_id="child-session", model=backend)

        self.assertEqual(result["memory_ids"], [old.memory_id])
        active = self.service._read_memories_unlocked("knowledge")
        self.assertEqual([record.memory.memory_id for record in active], [old.memory_id])
        self.assertIn("负责人已更新为乙", active[0].memory.body)
        self.assertNotIn("负责人是甲", active[0].memory.body)
        history = self.service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.body, old.body)

        gate_prompt = next(call["prompt"] for call in backend.calls if call["purpose"] == "gate" and "乙" in call["prompt"])
        self.assertIn("project:alpha", gate_prompt)
        self.assertIn(old.body, gate_prompt)

    def test_reset_clears_lineage_and_does_not_inherit_parent_scope(self):
        parent_user, parent_assistant = self.capture(
            "parent-session",
            "turn-1",
            "alpha 项目负责人是甲。",
            "已确认 alpha 项目的负责人。",
        )
        self.service.process(
            source="hermes",
            session_id="parent-session",
            model=QueueBackend(
                [
                    gate(
                        [
                            candidate(
                                "alpha-owner",
                                [parent_user, parent_assistant],
                                "alpha 项目的负责人是甲。",
                                scopes=["project:alpha"],
                            )
                        ]
                    ),
                    summary(
                        parent_user,
                        "alpha 项目的负责人是甲。",
                        title="alpha 项目负责人",
                        scopes=["project:alpha"],
                    ),
                ]
            ),
        )

        self.service.session_lineage(
            "hermes", "child-session", parent_session_id="parent-session"
        )
        cleared = self.service.session_lineage("hermes", "child-session", reset=True)
        self.assertTrue(cleared["cleared"])
        child_state = self.processed()["sessions"]["hermes/child-session"]
        self.assertNotIn("lineage_parent_session_id", child_state)

        self.capture(
            "child-session",
            "turn-1",
            "这个项目现在有什么变化？",
            "请先确认具体项目。",
        )
        backend = QueueBackend([gate([])])
        result = self.service.process(source="hermes", session_id="child-session", model=backend)
        self.assertEqual(result["memories_written"], 0)
        gate_prompt = backend.calls[0]["prompt"]
        self.assertIn("Session scope background:\n[]", gate_prompt)

    def test_real_hermes_provider_stdio_lineage_reaches_child_processing(self):
        provider = self._real_hermes_provider()
        parent_keys = self._provider_capture(
            provider,
            "parent-session",
            1,
            "alpha 项目采用达梦数据库，负责人是甲。",
            "已确认 alpha 项目的技术路线和负责人。",
        )
        parent_backend = QueueBackend(
            [
                gate(
                    [
                        candidate(
                            "alpha-owner",
                            parent_keys,
                            "alpha 项目的负责人是甲。",
                            scopes=["project:alpha"],
                        )
                    ]
                ),
                summary(
                    parent_keys[0],
                    "alpha 项目采用达梦数据库，负责人是甲。",
                    title="alpha 项目负责人",
                    scopes=["project:alpha"],
                ),
            ]
        )
        self.service.process(source="hermes", session_id="parent-session", model=parent_backend)
        old = self.service._read_memories_unlocked("knowledge")[0].memory
        self.assertEqual(
            self.processed()["sessions"]["hermes/parent-session"]["scopes"],
            ["project:alpha"],
        )

        provider.on_session_switch(
            "child-session",
            parent_session_id="parent-session",
            reset=False,
            reason="compression",
        )
        linked_state = self.processed()["sessions"]["hermes/child-session"]
        self.assertEqual(linked_state["lineage_parent_session_id"], "parent-session")
        self.assertNotIn("scopes", linked_state)

        child_keys = self._provider_capture(
            provider,
            "child-session",
            1,
            "这个项目负责人更新为乙。",
            "已确认负责人变更为乙。",
        )
        child_backend = QueueBackend(
            [
                gate(
                    [
                        candidate(
                            "alpha-owner-update",
                            child_keys,
                            "alpha 项目负责人更新为乙。",
                            scopes=["project:alpha"],
                            update_memory_id=old.memory_id,
                        )
                    ]
                ),
                summary(
                    child_keys[0],
                    "alpha 项目采用达梦数据库，负责人已更新为乙。",
                    title="alpha 项目负责人",
                    scopes=["project:alpha"],
                    update_memory_id=old.memory_id,
                ),
            ]
        )
        result = self.service.process(source="hermes", session_id="child-session", model=child_backend)

        self.assertEqual(result["memory_ids"], [old.memory_id])
        active = self.service._read_memories_unlocked("knowledge")
        self.assertEqual([record.memory.memory_id for record in active], [old.memory_id])
        self.assertIn("负责人已更新为乙", active[0].memory.body)
        history = self.service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.body, old.body)

    def test_session_lineage_is_available_as_a_control_only_mcp_tool(self):
        process = MCPProcess(self.service.vault.root)
        self.addCleanup(process.close)

        listed = process.send(
            {"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}}
        )
        self.assertNotIn(
            "session_lineage",
            {tool["name"] for tool in listed["result"]["tools"]},
        )

        response = process.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "session_lineage",
                    "arguments": {
                        "source": "hermes",
                        "session_id": "child-session",
                        "parent_session_id": "parent-session",
                    },
                },
            }
        )
        result = response["result"]
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["linked"], True)
        state = self.processed()["sessions"]["hermes/child-session"]
        self.assertEqual(state["lineage_parent_session_id"], "parent-session")

        response = process.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "session_lineage",
                    "arguments": {"source": "hermes", "session_id": "child-session", "reset": True},
                },
            }
        )
        result = response["result"]
        self.assertFalse(result["isError"], result)
        self.assertNotIn("lineage_parent_session_id", self.processed()["sessions"]["hermes/child-session"])


if __name__ == "__main__":
    unittest.main()
