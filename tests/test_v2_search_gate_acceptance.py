"""End-to-end V2 search-gate semantics in an isolated Vault and MCP process."""

from __future__ import annotations

import json
from pathlib import Path
import secrets
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.retrieval_gate import validate_turn
from tests.test_stage_c1_mcp import MCPProcess


class NoAdmissionBackend:
    def complete(self, prompt, *, system="", purpose="", temperature=0):
        del prompt, system, temperature
        if purpose != "gate":
            raise AssertionError("a pure query must not reach summarization")
        return '{"candidates":[]}'


class V2SearchGateAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="memleaf-v2-search-gate-")
        self.addCleanup(temporary.cleanup)
        self.service = Memleaf(Path(temporary.name) / "vault", model=NoAdmissionBackend())
        self.service.create_memory(
            memory_id="business-huaan",
            title="示例保险项目交付材料与负责人",
            body="示例保险项目交付材料正在准备，当前负责人为乙。",
            tags=["示例保险", "交付材料"],
            aliases=["示例保险项目"],
            scopes=["global"],
        )
        self.service.create_memory(
            memory_id="generic-project",
            title="通用项目工作方法",
            body="用于处理一般项目事项。",
            tags=["项目"],
            scopes=["global"],
        )
        self.process = MCPProcess(self.service.vault.root)
        self.addCleanup(self.process.close)
        self.request_id = 0

    def call(self, name: str, *, request_id: int | None = None, **arguments):
        if request_id is None:
            self.request_id += 1
            request_id = self.request_id
        else:
            self.request_id = max(self.request_id, request_id)
        response = self.process.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return response["result"]

    def success(self, name: str, **arguments):
        result = self.call(name, **arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def test_found_found_no_match_updates_gate_without_writing_query_memories(self) -> None:
        no_match_query = secrets.token_hex(20)
        preflight = self.service.search_candidates(no_match_query)
        self.assertEqual("no_match", preflight["status"])
        queries = [
            ("turn-example-materials", "示例保险项目交付材料", "FOUND"),
            ("turn-example-owner", "示例保险项目负责人", "FOUND"),
            ("turn-no-match", no_match_query, "NO_MATCH"),
        ]
        observed: list[tuple[str, str, str]] = []
        for turn_id, query, expected_status in queries:
            catalog = self.success(
                "scope_catalog",
                source="hermes",
                session_id="isolated-search-session",
                turn_id=turn_id,
            )
            # Automatic injection is still scope-only.
            rendered_catalog = json.dumps(catalog, ensure_ascii=False)
            for hidden in (
                "business-huaan",
                "generic-project",
                "示例保险项目交付材料与负责人",
                "用于处理一般项目事项",
            ):
                self.assertNotIn(hidden, rendered_catalog)

            retrieval_id = catalog["retrieval_id"]
            search = self.success("search", query=query, retrieval_id=retrieval_id)
            self.assertEqual(expected_status.casefold(), search["status"])
            if expected_status == "FOUND":
                self.assertEqual(["business-huaan"], [item["memory_id"] for item in search["results"]])
                page = self.success(
                    "read",
                    memory_id=search["results"][0]["memory_id"],
                    retrieval_id=retrieval_id,
                )
                self.assertIn("示例保险", page["body"])
            else:
                self.assertEqual([], search["results"])

            state = validate_turn(self.service.vault, retrieval_id)
            self.assertEqual(expected_status, state["status"])
            self.assertGreaterEqual(state["search_attempts"], 1)
            if expected_status == "FOUND":
                self.assertGreaterEqual(state["read_count"], 1)
                self.assertGreater(state["read_chars"], 0)
            else:
                self.assertEqual(0, state["read_count"])
                self.assertEqual(0, state["read_chars"])
            observed.append((turn_id, query, retrieval_id))

        ledger = json.loads(
            (self.service.vault.index_path / "retrieval_gate.json").read_text(encoding="utf-8")
        )
        for _, _, retrieval_id in observed:
            entry = ledger["entries"][retrieval_id]
            self.assertGreaterEqual(entry["search_attempts"], 1)
            self.assertTrue(entry["seen_call_hashes"])

        knowledge_before = {path.name for path in self.service.vault.list_markdown("knowledge")}
        for index, (_, query, _) in enumerate(observed, start=1):
            self.service.capture("hermes", "isolated-search-session", str(index), "user", query)
            self.service.capture(
                "hermes",
                "isolated-search-session",
                str(index),
                "assistant",
                "已根据检索结果回答。",
            )
        processed = self.service.process(source="hermes", session_id="isolated-search-session")
        self.assertEqual([], processed["memory_ids"])
        self.assertEqual(0, processed["memories_written"])
        self.assertEqual(knowledge_before, {path.name for path in self.service.vault.list_markdown("knowledge")})
        self.assertEqual([], list(self.service.vault.list_markdown("history")))


if __name__ == "__main__":
    unittest.main()
