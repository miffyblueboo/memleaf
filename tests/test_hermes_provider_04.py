from tests.hermes_provider_support import *


class HermesProviderTestsPart04(HermesProviderTestBase):
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

    def test_mixed_future_use_validation_detail_is_preserved_and_logged(self) -> None:
        value = {
            "isError": True,
            "structuredContent": {
                "error": {
                    "code": "model_invalid_response",
                    "stage": "gate",
                    "validation_reason": "schema_violation",
                    "validation_detail": "mixed_future_use",
                    "attempt_count": 3,
                }
            },
        }
        fields = provider_module._mcp_error_fields(value)
        self.assertIsNotNone(fields)
        self.assertEqual(fields[4], "mixed_future_use")
        error = provider_module._MCPToolError(
            code="model_invalid_response",
            stage="gate",
            validation_reason="schema_violation",
            validation_detail="mixed_future_use",
            attempt_count=3,
        )
        self.assertEqual(error.validation_detail, "mixed_future_use")

        provider = self.provider(
            responses=[
                {"stored": True},
                {"stored": True},
                value,
            ]
        )
        with patch.object(provider_module.logger, "info") as info:
            provider.sync_turn("visible user", "visible assistant", session_id="mixed-future-use-session")
        output = "\n".join(call.args[0] % call.args[1:] for call in info.call_args_list)
        self.assertIn("validation_detail=mixed_future_use", output)
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

    def test_successful_process_exposes_external_body_capture_limit(self) -> None:
        provider = self.provider(
            responses=[
                {"stored": True},
                {"stored": True},
                {
                    "execution_status": "ok",
                    "external_evidence_status": "metadata_only",
                    "external_evidence": {
                        "status": "metadata_only",
                        "external_record_count": 1,
                        "retained_body_count": 0,
                        "metadata_only_record_count": 1,
                        "capture_policy": {
                            "tool_evidence_mode": "metadata",
                            "include_attachments": False,
                            "body_retention": "metadata",
                        },
                    },
                },
                {"scopes": [], "has_more": False, "next_cursor": None},
            ]
        )
        provider.sync_turn(
            "review the external result",
            "The processing call completed.",
            session_id="metadata-session",
        )

        notice = provider.prefetch("continue", session_id="metadata-session")

        self.assertIn("completed successfully", notice)
        self.assertIn("external records=1", notice)
        self.assertIn("retained external bodies=0", notice)
        self.assertIn("does not confirm that external content was extracted", notice)
        self.assertNotIn("deferred", notice.casefold())
        self.assertNotIn("pending", notice.casefold())

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
        self.assertIn("after your final answer", prompt)
        self.assertIn("successful explicit remember or update tool result", prompt)
        self.assertIn("记好了", prompt)
        self.assertIn("已落库", prompt)
        self.assertIn("current conversation", prompt)

    def test_core_process_schema_retry_is_single_provider_call(self) -> None:
        """Provider delegates bounded schema retries to the public process API."""

        session_id = "schema-retry-session"
        vault = self.root / "schema-retry-vault"
        service = Memleaf(vault)

        @semantic_fixture
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
        processed = json.loads(service.vault.processed_state_path.read_text(encoding="utf-8"))
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
        self.assertEqual(core.calls[-1][1], {"source": "hermes", "session_id": session_id, "background": True})
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

        processed_path = service.vault.processed_state_path
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
