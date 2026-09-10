from tests.stage_b2a_support import *


class StageB2ATestPart04(StageB2ATestBase):
    def test_relative_time_candidate_is_deferred_without_blocking_other_candidates(self):
        anchor = "2026-09-02T02:01:41Z"
        backend = QueueBackend()
        service = self.service(backend, name="relative-candidate-isolation")
        with patch("memleaf.capture._timestamp", return_value=anchor):
            user_key, assistant_key = self.capture_turn(
                service,
                turn="relative-candidate-isolation",
                user_event="relative-candidate-isolation-user",
                assistant_event="relative-candidate-isolation-assistant",
            )
        ambiguous = self.candidate(
            "ambiguous-date",
            [user_key],
            memory="项目截止日期需要确认。",
            type="todo",
        )
        valid = self.candidate(
            "valid-fact",
            [user_key],
            memory="项目负责人已确认。",
            type="fact",
        )
        bad_summary = self.summary(
            user_key,
            title="截止本周末",
            body="本周末完成。",
            type="todo",
        )
        good_summary = self.summary(
            user_key,
            title="项目负责人",
            body="项目负责人已确认。",
            type="fact",
        )
        backend.responses.extend(
            [self.gate([ambiguous, valid]), bad_summary, bad_summary, bad_summary, good_summary]
        )

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(result["deferred_candidates"], 1)
        self.assertEqual(len(self.knowledge(service)), 1)
        self.assertEqual(self.knowledge(service)[0].memory.body, "项目负责人已确认。")
        entry = self.processed(service)["sessions"]["codex/s"]["processed_turns"][0]
        self.assertEqual(
            [item["candidate_id"] for item in entry["deferred_candidates"]],
            ["ambiguous-date"],
        )
        self.assertEqual(entry["deferred_candidates"][0]["reason"], "relative_time")
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["processing"]["status"], "idle")
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["watermark"], 1)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").is_file())

    def test_shared_update_target_is_reconciled_before_writer(self):
        """Multiple admitted updates need one complete model decision before commit."""
        backend = QueueBackend()
        service = self.service(backend, name="shared-update-target")
        existing = service.create_memory(memory_id="mem-existing", title="Existing fact",
            body="The existing fact.", type="fact", scopes=["global"])
        user_key, _ = self.capture_turn(service, turn="shared-update",
            user_event="shared-user", assistant_event="shared-assistant",
            user="The first update is true. The second update is also true.", assistant="Noted.")
        candidates = [
            self.candidate("update-one", [user_key], memory="The first update is true.",
                update_memory_id=existing.memory_id),
            self.candidate("update-two", [user_key], memory="The second update is also true.",
                update_memory_id=existing.memory_id),
        ]
        merged = json.loads(self.summary(user_key, title="Existing fact",
            body="The existing fact now includes both updates.", update_memory_id=existing.memory_id))
        backend.responses.extend([
            self.gate(candidates),
            self.summary(user_key, title="Existing fact", body="The first update is true.",
                update_memory_id=existing.memory_id),
            self.summary(user_key, title="Existing fact", body="The second update is also true.",
                update_memory_id=existing.memory_id),
            json.dumps({"decision": "UPDATE", "candidate_ids": ["update-one", "update-two"],
                "summary": merged}),
        ])
        result = service.process()
        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls],
            ["gate", "summarize", "summarize", "summarize"])
        self.assertIn("SAME_TARGET_RECONCILIATION", backend.calls[-1]["prompt"])
        self.assertEqual(service.read(existing.memory_id).body,
            "The existing fact now includes both updates.")
        self.assertEqual(len(service.vault.list_markdown("history")), 1)
        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["processing"]["status"], "idle")
        self.assertEqual(state["watermark"], 1)
        rows = state["processed_turns"][0]["candidate_dispositions"]
        self.assertEqual({r["candidate_id"] for r in rows}, {"update-one", "update-two"})
        self.assertTrue(all(r["disposition"] == "UPDATE" for r in rows))
        self.assertEqual(len({r["operation_id"] for r in rows}), 1)

    def test_three_invalid_group_responses_defer_updates_and_keep_inbox(self):
        """An invalid reconciliation never writes a partial update or drops evidence."""
        backend = QueueBackend()
        service = self.service(backend, name="shared-update-failure")
        existing = service.create_memory(memory_id="mem-existing", title="Existing fact",
            body="The existing fact.", type="fact", scopes=["global"])
        user_key, _ = self.capture_turn(service, turn="shared-update-failure",
            user_event="failure-user", assistant_event="failure-assistant",
            user="The first update is true. The second update is also true.", assistant="Noted.")
        candidates = [
            self.candidate("update-one", [user_key], memory="The first update is true.",
                update_memory_id=existing.memory_id),
            self.candidate("update-two", [user_key], memory="The second update is also true.",
                update_memory_id=existing.memory_id),
        ]
        backend.responses.extend([
            self.gate(candidates),
            self.summary(user_key, title="Existing fact", body="The first update is true.",
                update_memory_id=existing.memory_id),
            self.summary(user_key, title="Existing fact", body="The second update is also true.",
                update_memory_id=existing.memory_id),
            *[json.dumps({"decision": "NO_CHANGE", "candidate_ids": ["update-one"]})] * 3,
        ])
        result = service.process()
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["deferred_candidates"], 2)
        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["processing"]["status"], "idle")
        self.assertEqual(state["watermark"], 1)
        rows = state["processed_turns"][0]["candidate_dispositions"]
        self.assertTrue(all(r["disposition"] == "DEFERRED" for r in rows))
        self.assertEqual({r["reason"] for r in rows}, {"same_turn_reconciliation_failed"})
        self.assertEqual(len(self.knowledge(service)), 1)
        self.assertEqual(service.read(existing.memory_id).body, existing.body)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").exists())
        self.assertEqual(len(backend.calls), 6)
        self.assertTrue(all("SAME_TARGET_RECONCILIATION" in c["prompt"] for c in backend.calls[-3:]))

    def test_writer_batch_defense_labels_duplicate_update_conflict(self):
        service = self.service(QueueBackend(), name="writer-duplicate-update-defense")
        existing = service.create_memory(
            memory_id="mem-existing",
            title="Existing fact",
            body="The existing fact.",
            type="fact",
            scopes=["global"],
        )
        self.capture_turn(
            service,
            turn="writer-defense",
            user_event="writer-defense-user",
            assistant_event="writer-defense-assistant",
        )
        turn = parse_inbox(service.vault)[0]
        request = {
            "summary": {"type": "fact", "update_memory_id": existing.memory_id},
            "memory_id": "mem-one",
            "turn": turn,
        }
        with self.assertRaises(ModelOutputError) as raised:
            MemoryWriter(service)._preflight([request, dict(request, memory_id="mem-two")])
        self.assertEqual(raised.exception.validation_detail, "duplicate_update_target")
        self.assertEqual(raised.exception.validation_reason, "schema_violation")
        self.assertEqual(raised.exception.stage, "summarize")

    def test_writer_batch_defense_labels_deterministic_id_conflict(self):
        service = self.service(QueueBackend(), name="writer-deterministic-id-defense")
        self.capture_turn(
            service,
            turn="writer-deterministic",
            user_event="writer-deterministic-user",
            assistant_event="writer-deterministic-assistant",
        )
        turn = parse_inbox(service.vault)[0]
        request = {
            "summary": {"type": "fact"},
            "memory_id": "mem-same",
            "turn": turn,
        }
        with self.assertRaises(ModelOutputError) as raised:
            MemoryWriter(service)._preflight([request, dict(request)])
        self.assertEqual(raised.exception.validation_reason, "schema_violation")
        self.assertEqual(raised.exception.validation_detail, "other_schema_violation")
        self.assertEqual(raised.exception.stage, "summarize")

    def test_remember_skips_gate_native_duplicate_still_writes_and_retries_idempotently(self):
        backend = QueueBackend()

        def native_reader(query):
            return [{"title": "native", "body": "remember this", "hidden": "DO_NOT_PROMPT"}]

        service = self.service(backend, native=native_reader)
        remember_key = event_key("remember-event")
        backend.responses.append(self.summary(remember_key, title="Remembered", body="remember this"))

        first = service.remember(
            "remember this",
            source="codex",
            session_id="remember-session",
            turn_id="remember-turn",
            event_id="remember-event",
        )
        calls_after_first = len(backend.calls)
        second = service.remember(
            "remember this",
            source="codex",
            session_id="remember-session",
            turn_id="remember-turn",
            event_id="remember-event",
        )

        self.assertEqual([call["purpose"] for call in backend.calls], ["summarize"])
        self.assertEqual(first["memories_written"], 1)
        self.assertEqual(second["memory_ids"], first["memory_ids"])
        self.assertEqual(len(backend.calls), calls_after_first)
        self.assertEqual(len(self.knowledge(service)), 1)
        self.assertNotIn("DO_NOT_PROMPT", backend.calls[0]["prompt"])

    def test_prompt_contains_only_visible_turn_and_related_memories(self):
        prompts = []

        def inspect_prompt(prompt, **kwargs):
            prompts.append(prompt)
            return self.gate([])

        backend = QueueBackend([inspect_prompt])

        def native_reader(query):
            return [{"title": "native title", "body": "native related fact", "system_prompt": "PRIVATE"}]

        service = self.service(backend, native=native_reader)
        service.create_memory(title="local title", body="visible user statement local related fact", tags=["local"])
        user_key, _ = self.capture_turn(
            service,
            user="visible user statement",
            assistant="visible assistant answer",
        )
        processed_path = service.vault.processed_state_path
        value = self.processed(service)
        value["sessions"]["codex/s"]["scope"] = "project:visible"
        value["sessions"]["codex/s"]["system_prompt"] = "PRIVATE_SESSION_STATE"
        processed_path.write_text(json.dumps(value), encoding="utf-8")

        service.process()

        self.assertEqual(len(prompts), 1)
        prompt = prompts[0]
        for visible in ("visible user statement", "visible assistant answer", "local related fact", "native related fact", "project:visible"):
            self.assertIn(visible, prompt)
        for hidden in ("PRIVATE", "PRIVATE_SESSION_STATE", "system_prompt"):
            self.assertNotIn(hidden, prompt)
        self.assertIn(user_key, prompt)

    def test_invalid_gate_leaves_retryable_state_and_no_model_is_needed_when_idle(self):
        invalid_gate = json.dumps({"candidates": "invalid"})
        backend = QueueBackend([invalid_gate, invalid_gate, invalid_gate])
        service = self.service(backend)
        self.capture_turn(service)

        with self.assertRaises(ModelOutputError):
            service.process()

        failed = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(failed["processing"]["status"], "failed")
        self.assertNotIn("processed_turns", failed)
        self.assertEqual(len(self.knowledge(service)), 0)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").exists())

        backend.responses.append(self.gate([]))
        self.assertEqual(service.process()["processed_turns"], 1)
        service.router = None
        self.assertEqual(service.process()["processed_turns"], 0)

    def test_invalid_gate_retries_once_with_correction_and_commits_once(self):
        backend = QueueBackend(["```json\n{\"candidates\":[]}\n```", self.gate([])])
        service = self.service(backend)
        self.capture_turn(service)

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(len(self.knowledge(service)), 0)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate"])
        self.assertIn("Correction:", backend.calls[1]["prompt"])
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["processing"]["status"], "idle")
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["watermark"], 1)

    def test_schema_violation_gets_third_correction_attempt_and_commits(self):
        invalid = json.dumps({"candidates": "invalid"})
        backend = QueueBackend([invalid, invalid, self.gate([])])
        service = self.service(backend, name="schema-retry-three")
        self.capture_turn(service)

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(len(backend.calls), 3)
        self.assertTrue(all("Correction:" in call["prompt"] for call in backend.calls[1:]))
        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["processing"]["status"], "idle")
        self.assertEqual(state["watermark"], 1)

    def test_three_schema_violations_fail_with_final_stage_diagnostics(self):
        invalid = json.dumps({"candidates": "invalid"})
        backend = QueueBackend([invalid, invalid, invalid])
        service = self.service(backend, name="schema-retry-failure")
        self.capture_turn(service)

        with self.assertRaises(ModelOutputError):
            service.process()

        marker = self.processed(service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["status"], "failed")
        self.assertEqual(marker["failure_code"], "model_invalid_response")
        self.assertEqual(marker["failure_stage"], "gate")
        self.assertEqual(marker["validation_reason"], "schema_violation")
        self.assertEqual(marker["validation_detail"], "root_shape")
        self.assertEqual(marker["attempt_count"], 3)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"].get("watermark", 0), 0)
        self.assertEqual(len(backend.calls), 3)

    def test_invalid_type_correction_hint_is_safe_and_does_not_echo_first_output(self):
        secret = "FIRST_GATE_OUTPUT_SECRET"
        backend = QueueBackend()
        service = self.service(backend, name="correction-detail")
        user_key, _ = self.capture_turn(service, user="visible user fact")
        invalid = self.gate(
            [self.candidate("bad", [user_key], memory=secret, type="requirement")]
        )
        backend.responses.extend([invalid, self.gate([])])

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertIn("Previous output violated: invalid_type.", backend.calls[1]["prompt"])
        self.assertIn("whenever type is non-null", backend.calls[1]["prompt"])
        self.assertNotIn(secret, backend.calls[1]["prompt"])

    def test_empty_fenced_and_missing_gate_shapes_retry_once(self):
        invalid_outputs = ("", "```json\n{\"candidates\":[]}\n```", "{}")
        for index, invalid_output in enumerate(invalid_outputs):
            with self.subTest(index=index):
                backend = QueueBackend([invalid_output, self.gate([])])
                service = self.service(backend, name=f"gate-retry-{index}")
                self.capture_turn(service)

                result = service.process()

                self.assertEqual(result["processed_turns"], 1)
                self.assertEqual(len(backend.calls), 2)
                self.assertEqual(
                    self.processed(service)["sessions"]["codex/s"]["processing"]["status"],
                    "idle",
                )

    def test_two_invalid_gate_attempts_save_safe_diagnostics_and_keep_inbox(self):
        invalid = "```json\nGATE_OUTPUT_SECRET\n```"
        backend = QueueBackend([invalid, invalid, invalid])
        service = self.service(backend)
        self.capture_turn(service)

        with self.assertRaises(ModelOutputError):
            service.process()

        marker = self.processed(service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["failure_code"], "model_invalid_response")
        self.assertEqual(marker["failure_stage"], "gate")
        self.assertEqual(marker["validation_reason"], "invalid_json")
        self.assertEqual(marker["attempt_count"], 3)
        self.assertNotIn("GATE_OUTPUT_SECRET", json.dumps(marker))
        self.assertNotIn("GATE_OUTPUT_SECRET", backend.calls[1]["prompt"])
        self.assertEqual(self.processed(service)["sessions"]["codex/s"].get("watermark", 0), 0)
        self.assertEqual(len(self.knowledge(service)), 0)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").exists())

    def test_diagnostics_are_opt_in_bounded_and_structural_only(self):
        service = self.service(QueueBackend())
        self.assertFalse(service.vault.logs_path.exists())
        config = service.vault.config()
        config["llm"]["diagnostic_logging"] = True
        save_config(service.vault.config_path, config)
        secret = "MODEL_BODY_SECRET sk-test-123 https://secret.invalid/v1"
        invalid = self.gate(
            [
                dict(
                    self.candidate("c1", ["wrong-event"], memory=secret),
                    unknown_field=secret,
                )
            ]
        )
        backend = QueueBackend([invalid, invalid, invalid])
        service.router = backend
        self.capture_turn(service)

        with self.assertRaises(ModelOutputError):
            service.process()

        path = service.vault.logs_path / "model-diagnostics.jsonl"
        self.assertTrue(path.is_file())
        if os.name == "posix":  # Windows security is governed by inherited ACLs, not mode bits.
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertLessEqual(path.stat().st_size, 256 * 1024)
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 3)
        for index, entry in enumerate(lines):
            self.assertEqual(entry["stage"], "gate")
            self.assertEqual(entry["source"], "codex")
            self.assertEqual(entry["session_id"], "s")
            self.assertEqual(entry["turn_index"], 1)
            self.assertEqual(entry["attempt_count"], index + 1)
            self.assertEqual(entry["top_level_type"], "object")
            self.assertEqual(entry["candidate_count"], 1)
            self.assertEqual(entry["unknown_fields_count"], 1)
            self.assertEqual(len(entry["output_sha256"]), 64)
            self.assertNotIn(secret, json.dumps(entry))
            self.assertNotIn("sk-test-123", json.dumps(entry))
            self.assertNotIn("secret.invalid", json.dumps(entry))
        self.assertEqual(
            self.processed(service)["sessions"]["codex/s"]["processing"]["validation_detail"],
            "unknown_fields",
        )

    def test_diagnostic_write_failure_preserves_model_output_error(self):
        config_service = self.service(QueueBackend())
        config = config_service.vault.config()
        config["llm"]["diagnostic_logging"] = True
        save_config(config_service.vault.config_path, config)
        invalid = json.dumps({"candidates": "invalid"})
        backend = QueueBackend([invalid, invalid, invalid])
        config_service.router = backend
        self.capture_turn(config_service)
        with patch.object(
            ModelExecutor,
            "_write_model_diagnostic",
            side_effect=OSError("diagnostic write secret"),
        ):
            with self.assertRaises(ModelOutputError):
                config_service.process()
        marker = self.processed(config_service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["failure_code"], "model_invalid_response")
        self.assertEqual(marker["validation_detail"], "root_shape")

    def test_dangling_diagnostic_symlink_cannot_receive_logs_or_mask_model_error(self):
        service = self.service(QueueBackend(), name="dangling-diagnostic")
        config = service.vault.config()
        config["llm"]["diagnostic_logging"] = True
        save_config(service.vault.config_path, config)
        service.vault.logs_path.mkdir(mode=0o700)
        target = Path(self.tempdir.name) / "diagnostic-target"
        diagnostic_path = service.vault.logs_path / "model-diagnostics.jsonl"
        diagnostic_path.symlink_to(target)
        invalid = json.dumps({"candidates": "invalid"})
        backend = QueueBackend([invalid, invalid, invalid])
        service.router = backend
        self.capture_turn(service)

        with self.assertRaises(ModelOutputError):
            service.process()

        self.assertTrue(diagnostic_path.is_symlink())
        self.assertFalse(target.exists())
        marker = self.processed(service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["failure_code"], "model_invalid_response")

    def test_backend_empty_content_model_error_retries_once_and_commits(self):
        backend = QueueBackend(
            [
                ModelError(
                    "BACKEND_EMPTY_SECRET",
                    code="model_invalid_response",
                    validation_reason="empty_content",
                ),
                self.gate([]),
            ]
        )
        service = self.service(backend)
        self.capture_turn(service)

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate"])
        self.assertIn("Correction:", backend.calls[1]["prompt"])
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["watermark"], 1)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["processing"]["status"], "idle")

    def test_empty_content_gets_one_additional_attempt_and_third_valid_gate_commits(self):
        empty = lambda: ModelError(
            "EMPTY_RESPONSE_SECRET",
            code="model_invalid_response",
            validation_reason="empty_content",
        )
        backend = QueueBackend([empty(), empty(), self.gate([])])
        service = self.service(backend, name="three-attempt-success")
        self.capture_turn(service)

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(len(backend.calls), 3)
        self.assertIn("Previous output violated: empty_content.", backend.calls[1]["prompt"])
        self.assertIn("Previous output violated: empty_content.", backend.calls[2]["prompt"])
        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["processing"]["status"], "idle")
        self.assertEqual(state["watermark"], 1)

    def test_three_empty_contents_fail_at_attempt_three_without_watermark_or_inbox_loss(self):
        backend = QueueBackend(
            [
                ModelError("EMPTY_ONE_SECRET", code="model_invalid_response", validation_reason="empty_content"),
                ModelError("EMPTY_TWO_SECRET", code="model_invalid_response", validation_reason="empty_content"),
                ModelError("EMPTY_THREE_SECRET", code="model_invalid_response", validation_reason="empty_content"),
            ]
        )
        service = self.service(backend, name="three-attempt-failure")
        self.capture_turn(service)

        with self.assertRaises(ModelError):
            service.process()

        state = self.processed(service)["sessions"]["codex/s"]
        marker = state["processing"]
        self.assertEqual(marker["status"], "failed")
        self.assertEqual(marker["failure_code"], "model_invalid_response")
        self.assertEqual(marker["validation_reason"], "empty_content")
        self.assertEqual(marker["attempt_count"], 3)
        self.assertEqual(state.get("watermark", 0), 0)
        self.assertEqual(len(self.knowledge(service)), 0)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").exists())
        self.assertEqual(len(backend.calls), 3)

    def test_model_response_diagnostics_are_opt_in_and_structural_only(self):
        config_service = self.service(QueueBackend(), name="response-diagnostic")
        config = config_service.vault.config()
        config["llm"]["diagnostic_logging"] = True
        save_config(config_service.vault.config_path, config)
        errors = []
        for index in range(3):
            error = ModelError(
                f"MODEL_BODY_SECRET_{index}",
                code="model_invalid_response",
                validation_reason="empty_content",
            )
            error.with_response_diagnostics(
                {
                    "finish_reason": "length",
                    "completion_tokens": 4096,
                    "content_present": False,
                    "content_chars": 0,
                    "reasoning_present": True,
                    "reasoning_chars": 128,
                    "url": "https://secret.invalid",
                    "api_key": "API_KEY_SECRET",
                }
            )
            errors.append(error)
        backend = QueueBackend(errors)
        config_service.router = backend
        self.capture_turn(config_service)

        with self.assertRaises(ModelError):
            config_service.process()

        path = config_service.vault.logs_path / "model-diagnostics.jsonl"
        self.assertTrue(path.is_file())
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(entries), 3)
        for entry in entries:
            self.assertEqual(entry["finish_reason"], "length")
            self.assertEqual(entry["completion_tokens"], 4096)
            self.assertFalse(entry["content_present"])
            self.assertEqual(entry["content_chars"], 0)
            self.assertTrue(entry["reasoning_present"])
            self.assertEqual(entry["reasoning_chars"], 128)
            self.assertNotIn("SECRET", json.dumps(entry))
            self.assertNotIn("secret.invalid", json.dumps(entry))

    def test_committed_first_turn_survives_second_turn_empty_content_failure(self):
        backend = QueueBackend()
        service = self.service(backend, name="committed-before-failure")
        first_user_key, _ = self.capture_turn(
            service,
            turn="t1",
            user_event="first-u",
            assistant_event="first-a",
            user="first turn visible",
        )
        backend.responses.extend(
            [
                self.gate([self.candidate("first-memory", [first_user_key])]),
                self.summary(first_user_key, title="First committed memory", body="First durable memory"),
            ]
        )
        self.assertEqual(service.process()["processed_turns"], 1)
        first_memory = self.knowledge(service)[0].memory
        first_memory_id = first_memory.memory_id
        first_memory_path = service.vault.memory_path(first_memory_id, "knowledge")
        first_memory_text = first_memory_path.read_text(encoding="utf-8")

        backend.responses.extend(
            [
                ModelError("EMPTY_ONE", code="model_invalid_response", validation_reason="empty_content"),
                ModelError("EMPTY_TWO", code="model_invalid_response", validation_reason="empty_content"),
                ModelError("EMPTY_THREE", code="model_invalid_response", validation_reason="empty_content"),
            ]
        )
        self.capture_turn(
            service,
            turn="t2",
            user_event="second-u",
            assistant_event="second-a",
            user="second turn visible",
        )
        with self.assertRaises(ModelError):
            service.process()

        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["watermark"], 1)
        self.assertEqual([item.memory.memory_id for item in self.knowledge(service)], [first_memory_id])
        self.assertTrue(first_memory_path.is_file())
        self.assertEqual(first_memory_path.read_text(encoding="utf-8"), first_memory_text)
        inbox_path = service.vault.inbox_path / "codex" / "s.md"
        self.assertTrue(inbox_path.exists())
        self.assertIn("second turn visible", inbox_path.read_text(encoding="utf-8"))
