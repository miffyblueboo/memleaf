"""Isolated host-event replay plus a real MCP process; not a live Codex chat."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.host_events import handle_event
from memleaf.inbox import parse_inbox
from memleaf.retrieval_gate import find_turn, validate_turn
from tests.test_stage_c1_mcp import MCPProcess


class EmptyGate:
    def complete(self, prompt, *, system="", purpose="", temperature=0):
        if purpose != "gate":
            raise AssertionError("A pure greeting must not reach summarization")
        return '{"candidates":[]}'


class V2HostFlowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="memleaf-v2-host-flow-")
        self.addCleanup(temporary.cleanup)
        self.service = Memleaf(Path(temporary.name) / "vault", model=EmptyGate())
        factory = patch("memleaf.host_events.Memleaf", return_value=self.service)
        factory.start()
        self.addCleanup(factory.stop)
        self.process = MCPProcess(self.service.vault.root)
        self.addCleanup(self.process.close)

    def event(self, name, turn_id="user-turn", **extra):
        return handle_event("codex", {
            "hook_event_name": name, "session_id": "isolated-session", "turn_id": turn_id,
            **extra,
        }, vault=self.service.vault)

    def test_new_turn_id_continuation_is_one_business_turn(self):
        injected = self.event("UserPromptSubmit", prompt="你好")
        self.assertIn("scope", injected["hookSpecificOutput"]["additionalContext"].lower())
        first_id = find_turn(self.service.vault, "codex", "isolated-session", "user-turn")
        blocked = self.event("Stop", last_assistant_message="SHOULD_NOT_BE_CAPTURED")
        self.assertEqual(blocked["decision"], "block")
        self.assertEqual({}, self.event("UserPromptSubmit", "continuation-turn", prompt=blocked["reason"]))
        self.assertEqual(first_id, find_turn(self.service.vault, "codex", "isolated-session", "continuation-turn"))

        arguments = {"query": "你好"}
        before = self.event("PreToolUse", "continuation-turn", tool_name="mcp__memleaf__search",
                            tool_use_id="call-1", tool_input=arguments)
        arguments = before["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(arguments["retrieval_id"], first_id)
        actual = self.process.send({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search", "arguments": arguments},
        })["result"]
        self.assertFalse(actual["isError"])
        self.assertEqual(actual["structuredContent"]["status"], "no_match")
        self.event("PostToolUse", "continuation-turn", tool_name="mcp__memleaf__search",
                   tool_use_id="call-1", tool_input=arguments, tool_response=actual)
        self.assertEqual(validate_turn(self.service.vault, first_id)["status"], "NO_MATCH")
        self.assertEqual({}, self.event("Stop", "continuation-turn", stop_hook_active=True,
                                        last_assistant_message="你好，有什么需要帮忙的？"))
        turns = parse_inbox(self.service.vault)
        self.assertEqual(len(turns), 1)
        self.assertTrue(turns[0].complete)
        self.assertEqual(len(turns[0].events), 2)
        self.assertNotIn("SHOULD_NOT_BE_CAPTURED", str([event.content for event in turns[0].events]))
        self.assertNotIn("memleaf continuation", str([event.content for event in turns[0].events]))
        ledger = json.loads(self.service.vault.processed_index_path.read_text())
        state = ledger["sessions"]["codex/isolated-session"]
        self.assertEqual(state["processed_watermark"], 1)
        self.assertEqual(state["processing"]["status"], "idle")
        self.assertEqual(self.service.stats()["knowledge"], 0)

    def test_a_real_user_message_is_not_eaten_by_pending_stop(self):
        self.event("UserPromptSubmit", prompt="你好")
        self.event("Stop", last_assistant_message="unverified")
        self.event("UserPromptSubmit", "actual-next-turn", prompt="这是新的真实问题，不是内部续跑。")
        turns = parse_inbox(self.service.vault)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[1].events[0].content, "这是新的真实问题，不是内部续跑。")
        self.assertNotEqual(
            find_turn(self.service.vault, "codex", "isolated-session", "user-turn"),
            find_turn(self.service.vault, "codex", "isolated-session", "actual-next-turn"),
        )

    def test_failed_search_is_bounded_and_degradation_remains_visible(self):
        self.event("UserPromptSubmit", prompt="你好")
        retrieval_id = find_turn(self.service.vault, "codex", "isolated-session", "user-turn")
        for attempt in range(2):
            self.event("PostToolUse", tool_name="mcp__memleaf__search",
                       tool_use_id=f"failed-{attempt}", tool_input={"retrieval_id": retrieval_id},
                       tool_response={"isError": True, "error": "PRIVATE_ERROR"})
            blocked = self.event("Stop", stop_hook_active=bool(attempt), last_assistant_message="你好")
            self.assertEqual(blocked["decision"], "block")
            self.assertNotIn("PRIVATE_ERROR", str(blocked))
        for _ in range(2):
            degraded = self.event("Stop", stop_hook_active=True, last_assistant_message="你好")
            self.assertNotIn("decision", degraded)
            self.assertIn("degraded", degraded["systemMessage"].lower())
        state = validate_turn(self.service.vault, retrieval_id)
        self.assertEqual(state["gate_retries"], 2)
        self.assertEqual(state["status"], "DEGRADED")
        self.assertEqual(len(parse_inbox(self.service.vault)[0].events), 2)

    def test_binding_does_not_grant_permission_and_expired_turn_is_denied(self):
        self.event("UserPromptSubmit", prompt="你好")
        retrieval_id = find_turn(self.service.vault, "codex", "isolated-session", "user-turn")
        for tool in ("search", "read"):
            before = self.event("PreToolUse", tool_name=f"mcp__memleaf__{tool}",
                                tool_input={"retrieval_id": "model-supplied-token"})["hookSpecificOutput"]
            self.assertEqual(before["updatedInput"]["retrieval_id"], retrieval_id)
            self.assertNotIn("permissionDecision", before)
        self.assertEqual({}, self.event("PreToolUse", tool_name="mcp__other__search", tool_input={}))
        ledger_path = self.service.vault.index_path / "retrieval_gate.json"
        ledger = json.loads(ledger_path.read_text())
        ledger["entries"][retrieval_id]["expires_at"] = 0
        ledger_path.write_text(json.dumps(ledger))
        denied = self.event("PreToolUse", tool_name="mcp__memleaf__search", tool_input={})
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("updatedInput", denied["hookSpecificOutput"])

    def test_only_valid_current_turn_search_results_can_satisfy_gate(self):
        self.event("UserPromptSubmit", prompt="你好")
        retrieval_id = find_turn(self.service.vault, "codex", "isolated-session", "user-turn")
        good = {"status": "no_match", "results": [], "has_more": False, "next_cursor": None}
        self.event("PostToolUse", tool_name="mcp__memleaf__search", tool_use_id="old-call",
                   tool_input={"retrieval_id": "old-turn"}, tool_response=good)
        self.assertEqual(validate_turn(self.service.vault, retrieval_id)["status"], "NOT_SEARCHED")
        for index, malformed in enumerate((
            {"status": "found", "results": []},
            {"status": "no_match", "results": [{"memory_id": "a"}]},
            {"isError": True, "structuredContent": good},
        )):
            self.event("PostToolUse", tool_name="mcp__memleaf__search", tool_use_id=f"malformed-{index}",
                       tool_input={"retrieval_id": retrieval_id}, tool_response=malformed)
            self.assertEqual(validate_turn(self.service.vault, retrieval_id)["status"], "ERROR")
        for _ in range(2):
            self.event("PostToolUse", tool_name="mcp__memleaf__search", tool_use_id="good-call",
                       tool_input={"retrieval_id": retrieval_id}, tool_response=good)
        state = validate_turn(self.service.vault, retrieval_id)
        self.assertEqual(state["status"], "NO_MATCH")
        self.assertEqual(state["search_attempts"], 4)

    def test_scope_deferred_processing_is_reported_as_incomplete(self):
        self.event("UserPromptSubmit", prompt="你好")
        retrieval_id = find_turn(self.service.vault, "codex", "isolated-session", "user-turn")
        self.event("PostToolUse", tool_name="mcp__memleaf__search", tool_use_id="no-match",
                   tool_input={"retrieval_id": retrieval_id},
                   tool_response={"status": "no_match", "results": []})
        with patch.object(self.service, "process", return_value={"deferred_candidates": 1, "deferred_inbox_turns": 1}):
            result = self.event("Stop", last_assistant_message="你好")
        self.assertNotIn("decision", result)
        self.assertIn("scope", result["systemMessage"].lower())
        self.assertIn("deferred", result["systemMessage"].lower())


if __name__ == "__main__":
    unittest.main()
