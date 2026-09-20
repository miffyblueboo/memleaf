"""Bare MCP retrieval fallback keeps search-before-read without a host hook."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.mcp_server import _TOOL_BY_NAME, _invoke_tool
from memleaf.retrieval_gate import begin_turn


def payload(result):
    return result["structuredContent"]


class McpRetrievalFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memleaf-mcp-fallback-")
        self.addCleanup(self.temp.cleanup)
        self.s = Memleaf.initialize(Path(self.temp.name) / "vault")
        self.s.create_memory(
            memory_id="mem-atlas",
            title="Atlas decision",
            body="Atlas uses MySQL.",
            type="fact",
        )
        self.s.create_memory(
            memory_id="mem-todo",
            title="Deliver report",
            body="Deliver the acceptance report.",
            type="todo",
            status="active",
            due_date="2026-10-02",
        )

    def test_schema_allows_first_search_without_token_but_read_stays_gated(self):
        self.assertEqual(_TOOL_BY_NAME["search"]["inputSchema"]["required"], ["query"])
        self.assertEqual(_TOOL_BY_NAME["list_todos"]["inputSchema"]["required"], [])
        self.assertEqual(
            _TOOL_BY_NAME["read"]["inputSchema"]["required"],
            ["memory_id", "retrieval_id"],
        )

    def test_bare_mcp_search_mints_token_and_read_uses_same_chain(self):
        searched = payload(_invoke_tool(
            self.s,
            "search",
            {"query": "Atlas"},
            request_id="search-1",
        ))
        self.assertEqual(searched["status"], "found")
        self.assertEqual(searched["results"][0]["memory_id"], "mem-atlas")
        token = searched["retrieval_id"]
        self.assertTrue(token.startswith("rtv-"))

        read = payload(_invoke_tool(
            self.s,
            "read",
            {"memory_id": "mem-atlas", "retrieval_id": token},
            request_id="read-1",
        ))
        self.assertEqual(read["memory_id"], "mem-atlas")
        self.assertEqual(read["body"], "Atlas uses MySQL.")

    def test_bare_mcp_todo_list_mints_token_and_read_uses_same_chain(self):
        listed = payload(_invoke_tool(
            self.s,
            "list_todos",
            {"status": "active"},
            request_id="todo-1",
        ))
        self.assertEqual(listed["status"], "found")
        self.assertEqual(listed["results"][0]["memory_id"], "mem-todo")
        token = listed["retrieval_id"]

        read = payload(_invoke_tool(
            self.s,
            "read",
            {"memory_id": "mem-todo", "retrieval_id": token},
            request_id="todo-read-1",
        ))
        self.assertEqual(read["memory_id"], "mem-todo")
        self.assertEqual(read["status"], "active")
        self.assertEqual(read["due_date"], "2026-10-02")

    def test_read_without_token_is_still_rejected(self):
        result = payload(_invoke_tool(
            self.s,
            "read",
            {"memory_id": "mem-atlas"},
            request_id="read-missing",
        ))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "retrieval_id_required")

    def test_pagination_without_returned_token_is_rejected(self):
        for name, args in (
            ("search", {"query": "Atlas", "cursor": "opaque"}),
            ("list_todos", {"status": "active", "cursor": "opaque"}),
        ):
            with self.subTest(name=name):
                result = payload(_invoke_tool(self.s, name, args, request_id=f"{name}-page"))
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], "retrieval_id_required")

    def test_new_bare_first_page_supersedes_old_connection_chain(self):
        first = payload(_invoke_tool(self.s, "search", {"query": "Atlas"}, request_id="first"))
        second = payload(_invoke_tool(self.s, "search", {"query": "report"}, request_id="second"))
        self.assertNotEqual(first["retrieval_id"], second["retrieval_id"])

        stale = payload(_invoke_tool(
            self.s,
            "read",
            {"memory_id": "mem-atlas", "retrieval_id": first["retrieval_id"]},
            request_id="stale-read",
        ))
        self.assertEqual(stale["status"], "error")
        self.assertEqual(stale["error"]["code"], "retrieval_turn_mismatch")

    def test_existing_host_token_flow_is_unchanged(self):
        token = begin_turn(self.s.vault, "hermes", "session", "turn")
        searched = payload(_invoke_tool(
            self.s,
            "search",
            {"query": "Atlas", "retrieval_id": token},
            request_id="host-search",
        ))
        self.assertEqual(searched["retrieval_id"], token)
        read = payload(_invoke_tool(
            self.s,
            "read",
            {"memory_id": "mem-atlas", "retrieval_id": token},
            request_id="host-read",
        ))
        self.assertEqual(read["body"], "Atlas uses MySQL.")


if __name__ == "__main__":
    unittest.main()
