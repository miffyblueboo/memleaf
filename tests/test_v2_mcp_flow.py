"""Real stdio boundary checks against an isolated Vault, not host acceptance."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.mcp_server import _invoke_tool
from memleaf.retrieval_gate import (
    RetrievalGateError,
    begin_turn,
    observe_search,
    validate_turn,
)
from tests.test_stage_c1_mcp import MCPProcess


class V2MCPFlowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.service = Memleaf(Path(temporary.name) / "vault")
        self.process = MCPProcess(self.service.vault.root)
        self.addCleanup(self.process.close)
        self.request_id = 0

    def call(self, name, **arguments):
        self.request_id += 1
        response = self.process.send({
            "jsonrpc": "2.0", "id": self.request_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        return response["result"]

    def success(self, name, **arguments):
        result = self.call(name, **arguments)
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        return result["structuredContent"]

    def test_candidate_pages_are_lightweight_and_not_top_three(self):
        for index in range(25):
            self.service.create_memory(
                memory_id=f"paged-{index:02}", title=f"Project topic {index:02}",
                body=f"PRIVATE_BODY_{index}", tags=["shared"],
                sources=[{"detail": "PRIVATE_SOURCE"}],
            )
        compatibility_token = begin_turn(
            self.service.vault, "codex", "compatibility-session", "directory-page"
        )
        first = self.success("search", query="shared", retrieval_id=compatibility_token)
        self.assertEqual(first["status"], "found")
        self.assertGreater(len(first["results"]), 3)
        self.assertLessEqual(len(first["results"]), 20)
        self.assertTrue(first["has_more"])
        seen = set()
        page = first
        while True:
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, separators=(",", ":"))), 4000)
            self.assertNotIn("PRIVATE", json.dumps(page))
            for entry in page["results"]:
                self.assertEqual(set(entry), {"memory_id", "title"})
                self.assertNotIn(entry["memory_id"], seen)
                seen.add(entry["memory_id"])
            if not page["has_more"]:
                break
            page = self.success(
                "search",
                query="shared",
                cursor=page["next_cursor"],
                retrieval_id=compatibility_token,
            )
        self.assertEqual(len(seen), 25)
        self.assertTrue(all(self.service.read(memory_id).hit_count == 0 for memory_id in seen))
        empty = self.success(
            "search", query="NO_SUCH_DISTINCT_TOKEN", retrieval_id=compatibility_token
        )
        self.assertEqual(empty, {"status": "no_match", "results": [], "has_more": False, "next_cursor": None})

    def test_scope_catalog_never_exposes_concrete_memory(self):
        self.service.create_memory(memory_id="HIDDEN_MEMORY_ID", title="HIDDEN_TITLE", body="HIDDEN_BODY")
        catalog = self.success("scope_catalog")
        self.assertEqual(set(catalog), {"scopes", "has_more", "next_cursor"})
        self.assertNotIn("HIDDEN", json.dumps(catalog))
        self.assertLessEqual(len(json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))), 2000)
        for scope in catalog["scopes"]:
            self.assertEqual(set(scope), {"scope", "parent", "aliases"})

    def test_managed_read_budget_is_shared_across_stdio_reconnects(self):
        for index in range(4):
            self.service.create_memory(memory_id=f"budget-{index}", title=f"Item {index}", body="字" * 2200)
        retrieval_id = begin_turn(self.service.vault, "codex", "session", "turn")
        observe_search(self.service.vault, retrieval_id, "found", "budget-search")
        for index in range(3):
            page = self.success("read", memory_id=f"budget-{index}", retrieval_id=retrieval_id, max_chars=99999)
            self.assertEqual(len(page["body"]), 2000)
        self.process.close()
        self.process = MCPProcess(self.service.vault.root)
        self.addCleanup(self.process.close)
        denied = self.call("read", memory_id="budget-3", retrieval_id=retrieval_id)
        self.assertTrue(denied["isError"])
        self.assertEqual(self.service.read("budget-3").hit_count, 0)
        self.assertNotIn("body", denied["structuredContent"])
        bypass = self.call("search", query="Item", view="full", retrieval_id=retrieval_id)
        self.assertTrue(bypass["isError"])
        self.assertEqual(bypass["structuredContent"]["error"]["code"], "retrieval_full_view_forbidden")
        another = begin_turn(self.service.vault, "codex", "session", "next-turn")
        observe_search(self.service.vault, another, "found", "another-read-search")
        self.assertEqual(len(self.success("read", memory_id="budget-3", retrieval_id=another)["body"]), 2000)

    def test_missing_or_stale_retrieval_id_is_not_new_budget(self):
        self.service.create_memory(memory_id="private", title="Private", body="PRIVATE_BODY")
        missing = self.call("read", memory_id="private")
        self.assertTrue(missing["isError"])
        self.assertEqual(missing["structuredContent"]["error"]["code"], "retrieval_id_required")
        self.assertNotIn("PRIVATE_BODY", json.dumps(missing))
        for tool, arguments in (("read", {"memory_id": "private"}), ("search", {"query": "Private"})):
            result = self.call(tool, **arguments, retrieval_id="invented-by-model")
            self.assertTrue(result["isError"])
            self.assertNotIn("PRIVATE_BODY", json.dumps(result))
            if tool == "search":
                self.assertEqual(
                    result["structuredContent"]["error"]["code"],
                    "retrieval_id_invalid",
                )

        with patch.object(self.service, "search_candidates") as backend:
            stale = _invoke_tool(
                self.service,
                "search",
                {
                    "query": "Private",
                    "retrieval_id": "rtv-stale-token-that-does-not-exist",
                },
            )
        backend.assert_not_called()
        self.assertTrue(stale["isError"])
        self.assertEqual(stale["structuredContent"]["error"]["code"], "retrieval_id_invalid")

    def test_search_schema_and_runtime_require_retrieval_id(self):
        listed = self.process.send(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        search = next(tool for tool in listed["result"]["tools"] if tool["name"] == "search")
        self.assertIn("retrieval_id", search["inputSchema"]["required"])

        with patch.object(self.service, "search_candidates") as backend:
            direct = _invoke_tool(self.service, "search", {"query": "private"})
        backend.assert_not_called()
        self.assertTrue(direct["isError"])
        self.assertEqual(direct["structuredContent"]["error"]["code"], "retrieval_id_required")

        # Keep one real stdio assertion for the protocol boundary as well.
        result = self.call("search", query="private")
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "retrieval_id_required")

    def test_hermes_obtains_stable_budget_token_over_stdio_without_core_import(self):
        first = self.success("scope_catalog", source="hermes", session_id="s", turn_id="turn-1")
        again = self.success("scope_catalog", source="hermes", session_id="s", turn_id="turn-1")
        self.assertEqual(first["retrieval_id"], again["retrieval_id"])
        next_turn = self.success("scope_catalog", source="hermes", session_id="s", turn_id="turn-2")
        self.assertNotEqual(first["retrieval_id"], next_turn["retrieval_id"])
        result = self.call("search", query="missing", retrieval_id=first["retrieval_id"])
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "retrieval_turn_mismatch")
        result = self.success("search", query="missing", retrieval_id=next_turn["retrieval_id"])
        self.assertEqual(result["status"], "no_match")
        partial = self.call("scope_catalog", session_id="s")
        self.assertTrue(partial["isError"])

    def test_hermes_stdio_search_observes_real_results_and_deduplicates_rpc_id(self):
        self.service.create_memory(
            memory_id="hermes-search-memory",
            title="Hermes search result",
            body="PRIVATE_HERMES_BODY",
            tags=["hermes-search-token"],
        )

        def token(turn_id):
            return self.success(
                "scope_catalog",
                source="hermes",
                session_id="hermes-session",
                turn_id=turn_id,
            )["retrieval_id"]

        def rpc_search(request_id, retrieval_id, **arguments):
            response = self.process.send({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {
                    "name": "search",
                    "arguments": {**arguments, "retrieval_id": retrieval_id},
                },
            })
            return response["result"]

        def seen_calls(retrieval_id):
            ledger = json.loads(
                (self.service.vault.index_path / "retrieval_gate.json").read_text(encoding="utf-8")
            )
            return ledger["entries"][retrieval_id]["seen_call_hashes"]

        found_id = token("found")
        found = rpc_search(101, found_id, query="hermes-search-token")
        self.assertFalse(found["isError"], found)
        found_state = validate_turn(self.service.vault, found_id)
        self.assertEqual(found_state["status"], "FOUND")
        self.assertEqual(found_state["search_attempts"], 1)
        self.assertEqual(len(seen_calls(found_id)), 1)

        read_response = self.process.send({
            "jsonrpc": "2.0", "id": 102, "method": "tools/call",
            "params": {
                "name": "read",
                "arguments": {"memory_id": "hermes-search-memory", "retrieval_id": found_id},
            },
        })["result"]
        self.assertFalse(read_response["isError"], read_response)
        self.assertEqual(read_response["structuredContent"]["body"], "PRIVATE_HERMES_BODY")
        found_state = validate_turn(self.service.vault, found_id)
        self.assertEqual(found_state["read_count"], 1)
        self.assertGreater(found_state["read_chars"], 0)

        # A replay with the same JSON-RPC id is the same server-scoped call.
        replay = rpc_search(101, found_id, query="hermes-search-token")
        self.assertFalse(replay["isError"], replay)
        self.assertEqual(validate_turn(self.service.vault, found_id)["search_attempts"], 1)

        # A newly spawned stdio server has a new namespace, so the same RPC id
        # is a distinct observation rather than a collision with the old one.
        self.process.close()
        self.process = MCPProcess(self.service.vault.root)
        self.addCleanup(self.process.close)
        after_restart = rpc_search(101, found_id, query="hermes-search-token")
        self.assertFalse(after_restart["isError"], after_restart)
        read_after_restart = self.process.send({
            "jsonrpc": "2.0", "id": 103, "method": "tools/call",
            "params": {
                "name": "read",
                "arguments": {"memory_id": "hermes-search-memory", "retrieval_id": found_id},
            },
        })["result"]
        self.assertFalse(read_after_restart["isError"], read_after_restart)
        restarted_state = validate_turn(self.service.vault, found_id)
        self.assertEqual(restarted_state["search_attempts"], 2)
        self.assertEqual(len(seen_calls(found_id)), 2)
        self.assertEqual(restarted_state["read_count"], 1)
        self.assertGreater(restarted_state["read_chars"], 0)

        no_match_id = token("no-match")
        no_match = rpc_search(201, no_match_id, query="mars-warehouse-xyz-unique")
        self.assertFalse(no_match["isError"], no_match)
        no_match_state = validate_turn(self.service.vault, no_match_id)
        self.assertEqual(no_match_state["status"], "NO_MATCH")
        self.assertEqual(no_match_state["search_attempts"], 1)
        self.assertEqual(len(seen_calls(no_match_id)), 1)

        error_id = token("error")
        error = rpc_search(301, error_id, query="hermes-search-token", cursor="invalid")
        self.assertTrue(error["isError"], error)
        error_state = validate_turn(self.service.vault, error_id)
        self.assertEqual(error_state["status"], "ERROR")
        self.assertEqual(error_state["search_attempts"], 1)
        self.assertEqual(len(seen_calls(error_id)), 1)

    def test_codex_stdio_search_does_not_double_count_host_observation(self):
        retrieval_id = begin_turn(self.service.vault, "codex", "codex-session", "turn-1")
        result = self.call(
            "search",
            query="no-such-codex-memory",
            retrieval_id=retrieval_id,
        )
        self.assertFalse(result["isError"], result)
        state = validate_turn(self.service.vault, retrieval_id)
        self.assertEqual(state["status"], "NOT_SEARCHED")
        self.assertEqual(state["search_attempts"], 0)
        ledger = json.loads(
            (self.service.vault.index_path / "retrieval_gate.json").read_text(encoding="utf-8")
        )
        self.assertEqual(ledger["entries"][retrieval_id]["seen_call_hashes"], [])

    def test_old_codex_token_is_rejected_for_search_and_read(self):
        memory = self.service.create_memory(
            memory_id="old-codex-memory",
            title="Old Codex",
            body="OLD_CODEX_BODY",
        )
        old_id = begin_turn(self.service.vault, "codex", "same-codex-session", "turn-1")
        observe_search(self.service.vault, old_id, "found", "old-codex-search")
        current_id = begin_turn(
            self.service.vault, "codex", "same-codex-session", "turn-2"
        )
        self.assertNotEqual(old_id, current_id)

        stale_search = self.call(
            "search",
            query="Old Codex",
            retrieval_id=old_id,
        )
        self.assertTrue(stale_search["isError"], stale_search)
        self.assertEqual(
            stale_search["structuredContent"]["error"]["code"],
            "retrieval_turn_mismatch",
        )

        stale_read = self.call(
            "read",
            memory_id=memory.memory_id,
            retrieval_id=old_id,
        )
        self.assertTrue(stale_read["isError"], stale_read)
        self.assertEqual(
            stale_read["structuredContent"]["error"]["code"],
            "retrieval_turn_mismatch",
        )
        self.assertEqual(validate_turn(self.service.vault, old_id)["read_count"], 0)
        self.assertEqual(self.service.read(memory.memory_id).hit_count, 0)

    def test_hermes_search_observation_failure_cannot_return_success(self):
        retrieval_id = begin_turn(self.service.vault, "hermes", "session", "turn")
        with patch(
            "memleaf.mcp_server.observe_search",
            side_effect=RetrievalGateError("retrieval_ledger_unavailable"),
        ):
            result = _invoke_tool(
                self.service,
                "search",
                {"query": "not-found", "retrieval_id": retrieval_id},
                request_id=99,
            )
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["status"], "error")
        self.assertEqual(
            result["structuredContent"]["error"]["code"],
            "retrieval_ledger_unavailable",
        )

    def test_hermes_search_writeback_rejects_turn_started_during_search(self):
        retrieval_id = self.success(
            "scope_catalog", source="hermes", session_id="race-session", turn_id="turn-1"
        )["retrieval_id"]

        def search_and_start_next_turn(**_arguments):
            self.success(
                "scope_catalog",
                source="hermes",
                session_id="race-session",
                turn_id="turn-2",
            )
            return {"status": "found", "results": [], "has_more": False, "next_cursor": None}

        with patch.object(self.service, "search_candidates", side_effect=search_and_start_next_turn):
            result = _invoke_tool(
                self.service,
                "search",
                {"query": "race", "retrieval_id": retrieval_id},
                request_id=91,
            )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error"]["code"],
            "retrieval_turn_mismatch",
        )
        self.assertEqual("NOT_SEARCHED", validate_turn(self.service.vault, retrieval_id)["status"])
        self.assertEqual(0, validate_turn(self.service.vault, retrieval_id)["search_attempts"])

    def test_real_errors_and_malformed_core_results_never_become_no_match(self):
        malformed_entries = [
            {"memory_id": "a", "title": 4},
            {"memory_id": "", "title": "A"},
            {"memory_id": 7, "title": "A"},
        ]
        values = [[], None, {"status": "found", "results": []}, {"status": "error", "results": []}]
        values.extend({"status": "found", "results": [entry], "has_more": False, "next_cursor": None}
                      for entry in malformed_entries)
        compatibility_token = begin_turn(
            self.service.vault, "codex", "malformed-search-session", "malformed-search"
        )
        for value in values:
            with self.subTest(value=value), patch.object(self.service, "search_candidates", return_value=value):
                result = _invoke_tool(
                    self.service,
                    "search",
                    {"query": "test", "retrieval_id": compatibility_token},
                )
                self.assertTrue(result["isError"])
                self.assertNotEqual(result["structuredContent"].get("status"), "no_match")
        with patch.object(self.service, "search_candidates", side_effect=RuntimeError("PRIVATE_EXCEPTION")):
            result = _invoke_tool(
                self.service,
                "search",
                {"query": "test", "retrieval_id": compatibility_token},
            )
            self.assertTrue(result["isError"])
            self.assertNotIn("PRIVATE_EXCEPTION", json.dumps(result))
        self.assertEqual(self.success("stats")["knowledge"], 0)

    def test_malformed_read_page_is_not_a_successful_tool_result(self):
        valid = {"memory_id": "a", "title": "A", "scopes": ["global"], "body": "ok",
                 "offset": 0, "next_offset": None, "has_more": False, "total_chars": 2, "version": "v1"}
        retrieval_id = begin_turn(self.service.vault, "codex", "session", "read-validation")
        observe_search(self.service.vault, retrieval_id, "found", "read-validation-search")
        values = [{}, {"body": "ok"}, {**valid, "body": ["ok"]}, {**valid, "scopes": "global"},
                  {**valid, "offset": True}, {**valid, "has_more": True}, {**valid, "total_chars": 1}]
        for value in values:
            with self.subTest(value=value), patch.object(self.service, "read_page", return_value=value):
                result = _invoke_tool(
                    self.service,
                    "read",
                    {"memory_id": "a", "retrieval_id": retrieval_id},
                )
                self.assertTrue(result["isError"])

    def test_read_requires_token_and_successful_search_for_hermes(self):
        memory = self.service.create_memory(
            memory_id="hermes-read-memory", title="Hermes read", body="PRIVATE_READ_BODY"
        )
        with patch.object(self.service, "read_page") as read_page:
            missing = self.call("read", memory_id=memory.memory_id)
        read_page.assert_not_called()
        self.assertTrue(missing["isError"])
        self.assertEqual(missing["structuredContent"]["error"]["code"], "retrieval_id_required")
        self.assertEqual(self.service.read(memory.memory_id).hit_count, 0)

        no_match_id = self.success(
            "scope_catalog", source="hermes", session_id="read-session", turn_id="no-match"
        )["retrieval_id"]
        self.success("search", query="does-not-exist", retrieval_id=no_match_id)
        denied = self.call("read", memory_id=memory.memory_id, retrieval_id=no_match_id)
        self.assertTrue(denied["isError"])
        self.assertEqual(denied["structuredContent"]["error"]["code"], "retrieval_search_required")
        self.assertEqual(self.service.read(memory.memory_id).hit_count, 0)

        repeated_id = self.success(
            "scope_catalog", source="hermes", session_id="read-session", turn_id="repeated"
        )["retrieval_id"]
        self.success("search", query="hermes-read-memory", retrieval_id=repeated_id)
        self.success("search", query="does-not-exist", retrieval_id=repeated_id)
        repeated_state = validate_turn(self.service.vault, repeated_id)
        self.assertEqual(repeated_state["status"], "NO_MATCH")
        self.assertEqual(repeated_state["search_attempts"], 2)
        denied = self.call("read", memory_id=memory.memory_id, retrieval_id=repeated_id)
        self.assertTrue(denied["isError"])
        self.assertEqual(denied["structuredContent"]["error"]["code"], "retrieval_search_required")
        self.assertEqual(self.service.read(memory.memory_id).hit_count, 0)

        found_id = self.success(
            "scope_catalog", source="hermes", session_id="read-session", turn_id="found"
        )["retrieval_id"]
        self.success("search", query="hermes-read-memory", retrieval_id=found_id)
        page = self.success("read", memory_id=memory.memory_id, retrieval_id=found_id)
        self.assertEqual(page["body"], "PRIVATE_READ_BODY")
        state = validate_turn(self.service.vault, found_id)
        self.assertEqual(state["read_count"], 1)
        self.assertGreater(state["read_chars"], 0)

    def test_old_hermes_token_is_rejected_after_new_turn_without_read_accounting(self):
        memory = self.service.create_memory(
            memory_id="old-turn-memory", title="Old turn", body="OLD_TURN_BODY"
        )
        old_id = self.success(
            "scope_catalog", source="hermes", session_id="same-session", turn_id="turn-1"
        )["retrieval_id"]
        self.success("search", query="old-turn-memory", retrieval_id=old_id)
        current_id = self.success(
            "scope_catalog", source="hermes", session_id="same-session", turn_id="turn-2"
        )["retrieval_id"]
        self.assertNotEqual(old_id, current_id)
        denied = self.call("read", memory_id=memory.memory_id, retrieval_id=old_id)
        self.assertTrue(denied["isError"])
        self.assertEqual(denied["structuredContent"]["error"]["code"], "retrieval_turn_mismatch")
        self.assertEqual(validate_turn(self.service.vault, old_id)["read_count"], 0)
        self.assertEqual(self.service.read(memory.memory_id).hit_count, 0)


if __name__ == "__main__":
    unittest.main()
