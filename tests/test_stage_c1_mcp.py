import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.llm import ModelError
from memleaf.mcp_server import _tool_error
from memleaf.retrieval_gate import begin_turn, observe_search
from memleaf.validation import ModelOutputError


ROOT = Path(__file__).resolve().parents[1]
MODERN_META = {
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    }
}


class MCPProcess:
    def __init__(self, vault: Path):
        environment = os.environ.copy()
        source_path = str(ROOT / "src")
        old_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            source_path
            if not old_pythonpath
            else source_path + os.pathsep + old_pythonpath
        )
        self.process = subprocess.Popen(
            [sys.executable, "-m", "memleaf.mcp_server", "--vault", str(vault)],
            cwd=str(ROOT),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.output_lines: list[bytes] = []
        self._closed = False

    def send_raw(self, value: bytes) -> dict:
        assert self.process.stdin is not None
        self.process.stdin.write(value)
        self.process.stdin.flush()
        raw = self.process.stdout.readline() if self.process.stdout is not None else b""
        if not raw:
            raise AssertionError(
                f"MCP server exited before response, returncode={self.process.poll()}"
            )
        self.output_lines.append(raw)
        return json.loads(raw.decode("utf-8"))

    def send(self, message: dict) -> dict:
        return self.send_raw(
            json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )

    def close(self) -> tuple[int, bytes, bytes]:
        if self._closed:
            return self.process.returncode or 0, b"".join(self.output_lines), b""
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        stdout_tail = self.process.stdout.read() if self.process.stdout is not None else b""
        stderr = self.process.stderr.read() if self.process.stderr is not None else b""
        returncode = self.process.wait(timeout=5)
        if stdout_tail:
            self.output_lines.extend(stdout_tail.splitlines(keepends=True))
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()
        self._closed = True
        return returncode, b"".join(self.output_lines), stderr


class StageC1MCPTest(unittest.TestCase):
    def start(self):
        tempdir = tempfile.TemporaryDirectory()
        process = MCPProcess(Path(tempdir.name) / "vault")
        self.addCleanup(process.close)
        self.addCleanup(tempdir.cleanup)
        return process

    @staticmethod
    def assert_tool_success(testcase, response):
        testcase.assertIn("result", response)
        result = response["result"]
        testcase.assertFalse(result.get("isError"))
        testcase.assertEqual(result["content"][0]["type"], "text")
        testcase.assertEqual(
            json.loads(result["content"][0]["text"]),
            result["structuredContent"],
        )
        return result["structuredContent"]

    @staticmethod
    def assert_modern_result(testcase, response):
        result = response["result"]
        testcase.assertEqual(result["resultType"], "complete")
        testcase.assertEqual(
            result["_meta"]["io.modelcontextprotocol/serverInfo"],
            {"name": "memleaf", "version": "0.1.3"},
        )
        return result

    def test_legacy_initialize_notification_tools_and_core_calls(self):
        process = self.start()
        initialized = process.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        )
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        self.assertNotIn("resultType", initialized["result"])
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "memleaf")
        instructions = initialized["result"]["instructions"]
        for requirement in (
            "Hermes host integration",
            "do not repeat those calls through MCP",
            "source and session_id together",
            "Memory Scope Map",
            "call memleaf search at least once",
            "Errors are not no_match",
            "Soft Gate",
            "directory entries as leads",
            "do not infer facts from a title",
            "scope/search/read flow",
            "2000 body characters per page",
            "MCP read requires retrieval_id",
            "NO_MATCH, ERROR, and DEGRADED turns cannot read",
            "as expected_version",
            "memory_version_changed",
            "explicit fallback only",
            "user-visible and assistant-visible text",
            "system or developer messages",
            "hidden reasoning",
            "raw tool output",
            "attachment bodies",
            "complete user+assistant turns",
            "do not process incomplete turns",
            "Use remember only when the user explicitly asks",
            "previously or currently explicitly said not to record",
            "skip capture for it",
            "For text already persisted",
            "use forget_memory or forget_about only when its target is reliably identified",
            "include_history=true only",
            "native memory that the target host already loads",
        ):
            self.assertIn(requirement, instructions)

        # The notification must not produce a line; the next response is read
        # directly after it.
        assert process.process.stdin is not None
        process.process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        process.process.stdin.flush()
        listed = process.send(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "capture",
                "context",
                "scope_catalog",
                "search",
                "read",
                "process",
                "remember",
                "forget_memory",
                "forget_about",
                "rebuild_index",
                "stats",
            },
        )

        capture = process.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "capture",
                    "arguments": {
                        "source": "codex",
                        "session_id": "s",
                        "turn_id": "t1",
                        "role": "user",
                        "content": "hello from c1",
                        "event_id": "e1",
                    },
                },
            }
        )
        self.assertTrue(self.assert_tool_success(self, capture)["stored"])
        context = process.send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "context", "arguments": {"query": "hello"}},
            }
        )
        self.assertIsInstance(self.assert_tool_success(self, context), dict)
        stats = process.send(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "stats", "arguments": {}},
            }
        )
        self.assertEqual(self.assert_tool_success(self, stats)["knowledge"], 0)
        rebuilt = process.send(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "rebuild_index", "arguments": {}},
            }
        )
        self.assertEqual(self.assert_tool_success(self, rebuilt)["knowledge"], 0)

    def test_legacy_version_negotiation_and_unknown_fallback(self):
        process = self.start()
        versions = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
        for request_id, version in enumerate(versions, start=1):
            response = process.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "initialize",
                    "params": {"protocolVersion": version},
                }
            )
            self.assertEqual(response["result"]["protocolVersion"], version)
        fallback = process.send(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "initialize",
                "params": {"protocolVersion": "unknown-version"},
            }
        )
        self.assertEqual(fallback["result"]["protocolVersion"], "2025-11-25")

    def test_modern_discover_and_direct_tool_requests(self):
        process = self.start()
        legacy = process.send(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            }
        )
        self.assertEqual(legacy["result"]["protocolVersion"], "2025-11-25")
        discover = process.send(
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": MODERN_META}
        )
        discover_result = self.assert_modern_result(self, discover)
        self.assertEqual(discover_result["supportedVersions"], ["2026-07-28"])
        self.assertEqual(discover_result["capabilities"], {"tools": {}})
        self.assertEqual(discover_result["cacheScope"], "private")
        self.assertIsInstance(discover_result["ttlMs"], int)
        self.assertGreaterEqual(discover_result["ttlMs"], 0)
        self.assertIsInstance(discover_result["instructions"], str)

        list_params = dict(MODERN_META)
        listed = process.send(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": list_params}
        )
        self.assertEqual(len(self.assert_modern_result(self, listed)["tools"]), 11)
        called = process.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "stats",
                    "arguments": {},
                    **MODERN_META,
                },
            }
        )
        result = self.assert_modern_result(self, called)
        self.assertFalse(result["isError"])
        self.assertIsInstance(result["structuredContent"], dict)

    def test_tool_schemas_have_required_fields_and_strict_properties(self):
        process = self.start()
        response = process.send(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}
        expected = {
            "capture": {"source", "session_id", "turn_id", "role", "content"},
            "context": {"query"},
            "scope_catalog": set(),
            "search": {"query", "retrieval_id"},
            "read": {"memory_id", "retrieval_id"},
            "process": set(),
            "remember": set(),
            "forget_memory": {"memory_id"},
            "forget_about": {"query"},
            "rebuild_index": set(),
            "stats": set(),
        }
        self.assertEqual(set(tools), set(expected))
        for name, tool in tools.items():
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertIs(schema["additionalProperties"], False)
            self.assertEqual(set(schema.get("required", [])), expected[name])
            self.assertIn("properties", schema)
        remember_schema = tools["remember"]["inputSchema"]
        self.assertEqual(
            {tuple(item["required"]) for item in remember_schema["anyOf"]},
            {("content",), ("text",)},
        )

    def test_malformed_unknown_bad_params_and_notifications(self):
        process = self.start()
        parse_error = process.send_raw(b"{not valid json\n")
        self.assertEqual(parse_error["error"]["code"], -32700)
        unknown_method = process.send(
            {"jsonrpc": "2.0", "id": 2, "method": "unknown/method", "params": {}}
        )
        self.assertEqual(unknown_method["error"]["code"], -32601)
        unknown_tool = process.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "not_a_tool", "arguments": {}},
            }
        )
        self.assertEqual(unknown_tool["error"]["code"], -32602)
        bad_arguments = process.send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": [],
            }
        )
        self.assertEqual(bad_arguments["error"]["code"], -32602)
        invalid_tool_arguments = process.send(
            {
                "jsonrpc": "2.0",
                "id": 4.1,
                "method": "tools/call",
                "params": {"name": "stats", "arguments": {"extra": True}},
            }
        )
        self.assertTrue(invalid_tool_arguments["result"]["isError"])

        assert process.process.stdin is not None
        process.process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        process.process.stdin.flush()
        ping = process.send({"jsonrpc": "2.0", "id": 5, "method": "ping"})
        self.assertEqual(ping["result"], {})
        process.process.stdin.write(b'{"jsonrpc":"2.0","method":"unknown/notification"}\n')
        process.process.stdin.flush()
        ping_again = process.send({"jsonrpc": "2.0", "id": 6, "method": "ping"})
        self.assertEqual(ping_again["result"], {})

    def test_stdout_is_json_only_and_stderr_does_not_leak_body_or_secret(self):
        process = self.start()
        secret = "sk-proj-12345678901234567890"
        body = "C1_PRIVATE_MEMORY_BODY"
        response = process.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "capture",
                    "arguments": {
                        "source": "codex",
                        "session_id": "s",
                        "turn_id": "t",
                        "role": "user",
                        "content": f"{body} api_key={secret}",
                    },
                },
            }
        )
        captured = self.assert_tool_success(self, response)
        self.assertNotIn(secret, captured["content"])
        returncode, stdout, stderr = process.close()
        self.assertEqual(returncode, 0)
        for line in stdout.splitlines(keepends=True):
            self.assertEqual(line.count(b"\n"), 1)
            json.loads(line.decode("utf-8"))
        self.assertNotIn(secret.encode("utf-8"), stderr)
        self.assertNotIn(body.encode("utf-8"), stderr)

    def test_memory_and_forget_results_are_json_serializable(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        vault = Path(tempdir.name) / "vault"
        service = Memleaf(vault)
        service.create_memory(
            memory_id="m-c1",
            title="C1 stored memory",
            body="C1 memory body",
            tags=["c1"],
        )
        process = MCPProcess(vault)
        self.addCleanup(process.close)
        retrieval_id = begin_turn(vault, "codex", "c1-session", "c1-read")
        observe_search(vault, retrieval_id, "found", "c1-search")
        read = process.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "read",
                    "arguments": {"memory_id": "m-c1", "retrieval_id": retrieval_id},
                },
            }
        )
        memory = self.assert_tool_success(self, read)
        self.assertEqual(memory["memory_id"], "m-c1")
        self.assertEqual(memory["body"], "C1 memory body")
        forgotten = process.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "forget_about", "arguments": {"query": "C1 stored memory"}},
            }
        )
        result = self.assert_tool_success(self, forgotten)
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["deleted"], ["m-c1"])

    def test_directory_lookup_and_versioned_body_pages_over_stdio(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        service = Memleaf(Path(tempdir.name) / "vault")
        memory = service.create_memory(
            memory_id="directory-page",
            title="Directory topic",
            body="PRIVATE_BODY_" + "中文内容" * 1300,
            tags=["directory"],
            sources=[{"detail": "PRIVATE_SOURCE"}],
        )
        original = memory.to_dict()
        process = MCPProcess(service.vault.root)
        self.addCleanup(process.close)
        def call(name, **arguments):
            return process.send({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            })

        catalog = self.assert_tool_success(
            self,
            process.send({
                "jsonrpc": "2.0", "id": 0, "method": "tools/call",
                "params": {
                    "name": "scope_catalog",
                    "arguments": {
                        "source": "hermes",
                        "session_id": "c1-directory",
                        "turn_id": "c1-search",
                    },
                },
            }),
        )
        retrieval_id = catalog["retrieval_id"]

        for tool in ("context", "search"):
            arguments = {"query": "directory"}
            if tool == "search":
                arguments["retrieval_id"] = retrieval_id
            response = call(tool, **arguments)
            payload = self.assert_tool_success(self, response)
            entries = payload["results" if tool == "search" else "result"]
            if tool == "search":
                self.assertEqual(payload["status"], "found")
            self.assertEqual(len(entries), 1)
            self.assertEqual(set(entries[0]), {"memory_id", "title", "scopes"})
            self.assertNotIn("PRIVATE_", json.dumps(response))
        self.assertEqual(service.read(memory.memory_id).to_dict(), original)
        # The Python API keeps its explicit full-result compatibility; the MCP
        # V2 surface is directory search followed by bounded read.
        full = service.search("directory", view="full")
        self.assertEqual(full[0].body, memory.body)

        first = self.assert_tool_success(
            self,
            call("read", memory_id=memory.memory_id, retrieval_id=retrieval_id, max_chars=99999),
        )
        self.assertEqual(len(first["body"]), 2000)
        self.assertEqual(set(first), {
            "memory_id", "title", "scopes", "body", "offset", "next_offset",
            "has_more", "total_chars", "version",
        })
        page = first
        body = page["body"]
        while page["has_more"]:
            page = self.assert_tool_success(self, call(
                "read", memory_id=memory.memory_id, offset=page["next_offset"],
                expected_version=first["version"], retrieval_id=retrieval_id,
            ))
            self.assertLessEqual(len(page["body"]), 2000)
            self.assertEqual(page["version"], first["version"])
            body += page["body"]
        self.assertEqual(body, memory.body)
        after = service.read(memory.memory_id).to_dict()
        self.assertEqual(after["hit_count"], original["hit_count"] + 1)
        for field in ("updated", "body", "sources", "title"):
            self.assertEqual(after[field], original[field])
        self.assertEqual(list(service.vault.history_path.glob("*.md")), [])

        changed = service.read(memory.memory_id)
        changed.body = "NEW_PRIVATE_BODY"
        service.write_memory(changed)
        stale = call(
            "read",
            memory_id=memory.memory_id,
            offset=2000,
            expected_version=first["version"],
            retrieval_id=retrieval_id,
        )
        self.assertTrue(stale["result"]["isError"])
        error = stale["result"]["structuredContent"]["error"]
        self.assertEqual(error["code"], "memory_version_changed")
        self.assertIn("offset=0", error["message"])
        self.assertNotIn("PRIVATE", json.dumps(stale))
        self.assertEqual(service.read(memory.memory_id).hit_count, after["hit_count"])
        restarted = self.assert_tool_success(
            self,
            call("read", memory_id=memory.memory_id, retrieval_id=retrieval_id),
        )
        self.assertEqual(restarted["body"], changed.body)
        self.assertNotEqual(restarted["version"], first["version"])

    def test_read_page_rejects_invalid_parameters_without_losing_connection(self):
        process = self.start()
        retrieval_id = begin_turn(Path(process.process.args[-1]), "codex", "c1-invalid", "c1-read")
        observe_search(Path(process.process.args[-1]), retrieval_id, "found", "c1-invalid-search")
        for extra in (
            {"offset": -1}, {"offset": True}, {"max_chars": 0},
            {"max_chars": True}, {"expected_version": 123}, {"expected_version": ""},
        ):
            with self.subTest(extra=extra):
                response = process.send({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {
                        "name": "read",
                        "arguments": {"memory_id": "missing", "retrieval_id": retrieval_id, **extra},
                    },
                })
                self.assertTrue(response["result"]["isError"])
        ping = process.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(ping["result"], {})

    def test_process_and_remember_without_model_are_tool_errors_and_server_continues(self):
        process = self.start()
        for event_id, role, content in (
            ("u1", "user", "a pending user turn"),
            ("a1", "assistant", "a pending assistant turn"),
        ):
            process.send(
                {
                    "jsonrpc": "2.0",
                    "id": event_id,
                    "method": "tools/call",
                    "params": {
                        "name": "capture",
                        "arguments": {
                            "source": "codex",
                            "session_id": "process-session",
                            "turn_id": "turn-1",
                            "role": role,
                            "content": content,
                            "event_id": event_id,
                        },
                    },
                }
            )
        process_error = process.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "process",
                    "arguments": {"source": "codex", "session_id": "process-session"},
                },
            }
        )
        process_result = process_error["result"]
        self.assertTrue(process_result["isError"])
        self.assertEqual(process_result["structuredContent"]["error"]["code"], "model_unavailable")

        remember_error = process.send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "remember",
                    "arguments": {
                        "content": "remember this only after an explicit request",
                        "source": "codex",
                        "session_id": "remember-session",
                    },
                },
            }
        )
        self.assertTrue(remember_error["result"]["isError"])
        self.assertEqual(
            remember_error["result"]["structuredContent"]["error"]["code"],
            "model_unavailable",
        )
        stats = process.send(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "stats", "arguments": {}},
            }
        )
        self.assertFalse(stats["result"]["isError"])

    def test_model_error_tool_result_has_only_safe_code_stage_and_message(self):
        response = _tool_error(
            ModelError("MODEL_RESPONSE_SECRET", code="model_timeout", stage="gate")
        )
        self.assertTrue(response["isError"])
        payload = response["structuredContent"]["error"]
        self.assertEqual(payload, {
            "code": "model_timeout",
            "message": "model request timed out",
            "stage": "gate",
        })
        self.assertNotIn("MODEL_RESPONSE_SECRET", json.dumps(response))

        response = _tool_error(
            ModelError("API_KEY_SECRET", code="model_auth_failed", stage="summarize")
        )
        self.assertEqual(response["structuredContent"]["error"]["code"], "model_auth_failed")
        self.assertEqual(response["structuredContent"]["error"]["stage"], "summarize")
        self.assertNotIn("API_KEY_SECRET", json.dumps(response))

        error = ModelError(
            "MODEL_OUTPUT_SECRET",
            code="model_invalid_response",
            stage="gate",
            validation_reason="invalid_json",
        )
        error.attempt_count = 2
        response = _tool_error(error)
        self.assertEqual(
            response["structuredContent"]["error"]["validation_reason"],
            "invalid_json",
        )
        self.assertEqual(response["structuredContent"]["error"]["attempt_count"], 2)
        self.assertNotIn("MODEL_OUTPUT_SECRET", json.dumps(response))

        error.attempt_count = 3
        response = _tool_error(error)
        self.assertEqual(response["structuredContent"]["error"]["attempt_count"], 3)

    def test_gate_model_output_error_is_safe_and_preserves_stage(self):
        error = ModelOutputError("GATE_MODEL_OUTPUT_SECRET")
        error.stage = "gate"
        error.attempt_count = 2
        response = _tool_error(error)
        self.assertTrue(response["isError"])
        self.assertEqual(
            response["structuredContent"]["error"],
            {
                "code": "model_invalid_response",
                "message": "model returned an invalid response",
                "stage": "gate",
                "validation_reason": "schema_violation",
                "validation_detail": "other_schema_violation",
                "attempt_count": 2,
            },
        )
        self.assertNotIn("GATE_MODEL_OUTPUT_SECRET", json.dumps(response))

    def test_summarize_model_output_error_is_safe_and_preserves_stage(self):
        error = ModelOutputError("SUMMARIZE_MODEL_OUTPUT_SECRET")
        error.stage = "summarize"
        error.attempt_count = 2
        response = _tool_error(error)
        self.assertTrue(response["isError"])
        self.assertEqual(
            response["structuredContent"]["error"],
            {
                "code": "model_invalid_response",
                "message": "model returned an invalid response",
                "stage": "summarize",
                "validation_reason": "schema_violation",
                "validation_detail": "other_schema_violation",
                "attempt_count": 2,
            },
        )
        self.assertNotIn("SUMMARIZE_MODEL_OUTPUT_SECRET", json.dumps(response))

    def test_model_output_validation_detail_is_whitelisted_and_safe(self):
        error = ModelOutputError("MODEL_DETAIL_SECRET", validation_detail="invalid_type")
        error.stage = "gate"
        error.attempt_count = 2
        response = _tool_error(error)
        self.assertEqual(response["structuredContent"]["error"]["validation_detail"], "invalid_type")
        self.assertNotIn("MODEL_DETAIL_SECRET", json.dumps(response))

        unknown = ModelOutputError("MODEL_UNKNOWN_DETAIL_SECRET", validation_detail="not-allowed")
        unknown.stage = "summarize"
        response = _tool_error(unknown)
        self.assertEqual(
            response["structuredContent"]["error"]["validation_detail"],
            "other_schema_violation",
        )
        self.assertNotIn("MODEL_UNKNOWN_DETAIL_SECRET", json.dumps(response))

    def test_eof_is_normal_exit(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        process = subprocess.Popen(
            [sys.executable, "-m", "memleaf.mcp_server", "--vault", str(Path(tempdir.name) / "vault")],
            cwd=str(ROOT),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stdout, b"")
        self.assertEqual(stderr, b"")


if __name__ == "__main__":
    unittest.main()
