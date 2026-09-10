from tests.hermes_provider_support import *


class HermesProviderTestsPart03(HermesProviderTestBase):
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

    def test_read_diagnostics_keep_token_after_client_reconnect_without_relogging_history(self) -> None:
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
        self.assertEqual(len(read_logs), 2)
        self.assertTrue(all("retrieval_present=True" in line for line in read_logs))
        self.assertTrue(all("retrieval_match=True" in line for line in read_logs))
        self.assertNotIn("rtv-reconnect", output)

    def test_read_diagnostics_skip_cumulative_history_but_keep_current_wrong_token(self) -> None:
        old_call = {
            "id": "read-old",
            "function": {
                "name": "mcp__memleaf__read",
                "arguments": json.dumps(
                    {"memory_id": "MEMORY_OLD", "retrieval_id": "rtv-old"}
                ),
            },
        }
        current_call = {
            "id": "read-current",
            "function": {
                "name": "mcp__memleaf__read",
                "arguments": json.dumps(
                    {"memory_id": "MEMORY_CURRENT", "retrieval_id": "rtv-current"}
                ),
            },
        }
        wrong_token_call = {
            "id": "read-current-wrong-token",
            "function": {
                "name": "mcp__memleaf__read",
                "arguments": json.dumps(
                    {"memory_id": "MEMORY_WRONG", "retrieval_id": "rtv-other"}
                ),
            },
        }
        first_messages = [
            {"role": "assistant", "tool_calls": [old_call]},
            {
                "role": "tool",
                "tool_call_id": "read-old",
                "content": json.dumps({"memory_id": "MEMORY_OLD", "body": "old"}),
            },
        ]
        cumulative_messages = [
            *first_messages,
            {"role": "assistant", "tool_calls": [current_call, wrong_token_call]},
            {
                "role": "tool",
                "tool_call_id": "read-current",
                "content": json.dumps({"memory_id": "MEMORY_CURRENT", "body": "current"}),
            },
            {
                "role": "tool",
                "tool_call_id": "read-current-wrong-token",
                "content": json.dumps({"memory_id": "MEMORY_WRONG", "body": "wrong"}),
            },
        ]
        seen_call_keys: set[str] = set()

        with patch.object(provider_module.logger, "info") as info:
            provider_module.MemleafMemoryProvider._observe_search_messages(
                first_messages,
                "rtv-old",
                seen_call_keys=seen_call_keys,
            )
            provider_module.MemleafMemoryProvider._observe_search_messages(
                cumulative_messages,
                "rtv-current",
                seen_call_keys=seen_call_keys,
            )

        output = "\n".join(call.args[0] % call.args[1:] for call in info.call_args_list)
        read_logs = [line for line in output.splitlines() if "retrieval-read" in line]
        self.assertEqual(len(read_logs), 3)
        self.assertIn("retrieval_match=True result=ok", read_logs[0])
        self.assertIn("retrieval_match=True result=ok", read_logs[1])
        self.assertIn("retrieval_match=False result=uncontrolled_success", read_logs[2])

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

    def test_mcp_client_preserves_process_status_audit_wrapper(self) -> None:
        client = provider_module._MCPClient("memleaf-mcp", str(self.root / "vault"), 5, 300)
        with patch.object(client, "_start_locked"), patch.object(
            client,
            "_request_locked",
            return_value={
                "structuredContent": {
                    "job_id": "job-1",
                    "status": "succeeded",
                    "completed": True,
                    "result": {"memories_written": 2},
                }
            },
        ):
            value = client.call_tool("process_status", {"job_id": "job-1"})
        self.assertEqual(value["status"], "succeeded")
        self.assertEqual(value["result"]["memories_written"], 2)

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
                {"role": "assistant", "tool_calls": [
                    {"id": "external-call", "function": {"name": "mail.read", "arguments": "{}"}},
                ]},
                {"role": "tool", "tool_call_id": "external-call", "content": "TOOL_SECRET"},
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
        self.assertNotIn("tool_evidence", calls[1][1])
        self.assertEqual(calls[2][1], {"source": "hermes", "session_id": "session-1", "background": True})
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

    def test_scope_validation_details_are_preserved(self) -> None:
        for detail in ("scope_not_grounded", "scope_drift"):
            with self.subTest(detail=detail):
                value = {
                    "isError": True,
                    "structuredContent": {
                        "error": {
                            "code": "model_invalid_response",
                            "stage": "gate" if detail == "scope_not_grounded" else "summarize",
                            "validation_reason": "schema_violation",
                            "validation_detail": detail,
                            "attempt_count": 3,
                        }
                    },
                }
                fields = provider_module._mcp_error_fields(value)
                self.assertIsNotNone(fields)
                self.assertEqual(fields[4], detail)
