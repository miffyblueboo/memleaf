from tests.stage_b2a_support import *


class StageB2ATestPart05(StageB2ATestBase):
    def test_non_invalid_gate_error_is_not_retried(self):
        backend = QueueBackend([ModelError("MODEL_TIMEOUT_SECRET", code="model_timeout")])
        service = self.service(backend)
        self.capture_turn(service)

        with self.assertRaises(ModelError):
            service.process()

        self.assertEqual(len(backend.calls), 1)
        marker = self.processed(service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["failure_code"], "model_timeout")
        self.assertNotIn("MODEL_TIMEOUT_SECRET", json.dumps(marker))

    def test_explicit_remember_retries_invalid_summary_once(self):
        remember_key = event_key("remember-retry")
        backend = QueueBackend(
            [
                "```json\n{\"bad\":true}\n```",
                self.summary(remember_key, title="Retried"),
            ]
        )
        service = self.service(backend)

        result = service.remember(
            "remember this retryable fact",
            source="codex",
            session_id="remember-retry",
            turn_id="remember-retry-turn",
            event_id="remember-retry",
        )

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["summarize", "summarize"])
        self.assertIn("Correction:", backend.calls[1]["prompt"])
        self.assertEqual(self.processed(service)["sessions"]["codex/remember-retry"]["processing"]["status"], "idle")

    def test_invalid_second_summary_does_not_write_first_summary(self):
        backend = QueueBackend()
        service = self.service(backend)
        user_key, assistant_key = self.capture_turn(service, user="A durable fact. A second durable fact.")
        candidates = [self.candidate("c1", [user_key]), self.candidate("c2", [user_key], memory="A second durable fact")]
        backend.responses.extend(
            [
                self.gate(candidates),
                self.summary(user_key, title="first"),
                json.dumps({"bad": True}),
                json.dumps({"bad": True}),
                json.dumps({"bad": True}),
            ]
        )

        with self.assertRaises(ModelOutputError):
            service.process()

        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["processing"]["status"], "failed")
        self.assertEqual(state.get("watermark", 0), 0)
        self.assertEqual(state.get("processed_turns", []), [])
        self.assertEqual(len(self.knowledge(service)), 0)

        backend.responses.extend(
            [self.gate(candidates), self.summary(user_key, title="first"), self.summary(user_key, title="second")]
        )
        self.assertEqual(service.process()["memories_written"], 2)

    def test_second_model_failure_does_not_advance_watermark(self):
        backend = QueueBackend()
        service = self.service(backend)
        user_key, assistant_key = self.capture_turn(service, user="A durable fact. A second durable fact.")
        candidates = [self.candidate("c1", [user_key]), self.candidate("c2", [user_key], memory="A second durable fact")]
        backend.responses.extend(
            [self.gate(candidates), self.summary(user_key, title="first"), ModelError("second failed")]
        )

        with self.assertRaises(ModelError):
            service.process()

        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state.get("watermark", 0), 0)
        self.assertEqual(len(self.knowledge(service)), 0)

    def test_failed_marker_records_safe_model_code_and_retry_replaces_it(self):
        backend = QueueBackend()
        service = self.service(backend)
        self.capture_turn(service, user="visible user", assistant="visible assistant")
        backend.responses.append(ModelError("MODEL_RESPONSE_SECRET", code="model_timeout", stage="gate"))

        with self.assertRaises(ModelError):
            service.process()

        failed = self.processed(service)["sessions"]["codex/s"]
        marker = failed["processing"]
        self.assertEqual(marker["status"], "failed")
        self.assertEqual(marker["failure_code"], "model_timeout")
        self.assertEqual(marker["failure_stage"], "gate")
        self.assertNotIn("MODEL_RESPONSE_SECRET", json.dumps(marker))
        self.assertEqual(failed.get("watermark", 0), 0)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").exists())

        backend.responses.append(self.gate([]))
        self.assertEqual(service.process()["processed_turns"], 1)
        retried = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(retried["processing"]["status"], "idle")
        self.assertEqual(retried["watermark"], 1)

    def test_partial_knowledge_write_is_retryable_without_duplicate_files(self):
        backend = QueueBackend()
        service = self.service(backend)
        user_key, assistant_key = self.capture_turn(service, user="A durable fact. A second durable fact.")
        candidates = [self.candidate("c1", [user_key]), self.candidate("c2", [user_key], memory="A second durable fact")]
        responses = [self.gate(candidates), self.summary(user_key, title="first"), self.summary(user_key, title="second")]
        backend.responses.extend(responses)
        import memleaf.memory_writer as memory_writer_module

        original_write = memory_writer_module.atomic_write_text
        knowledge_writes = {"count": 0}

        def flaky_write(path, text):
            if path.parent.name == "knowledge":
                knowledge_writes["count"] += 1
                if knowledge_writes["count"] == 2:
                    raise OSError("controlled write failure")
            return original_write(path, text)

        with patch.object(memory_writer_module, "atomic_write_text", side_effect=flaky_write):
            with self.assertRaises(OSError):
                service.process()

        self.assertEqual(len(self.knowledge(service)), 1)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"].get("watermark", 0), 0)
        backend.responses.extend(responses)
        result = service.process()

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(len(self.knowledge(service)), 2)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["watermark"], 1)

    def test_sources_are_core_generated_and_todo_defaults_active(self):
        backend = QueueBackend()
        service = self.service(backend)
        user_key, assistant_key = self.capture_turn(service, user="I need to submit the report.")
        backend.responses.extend(
            [
                self.gate([self.candidate("todo", [user_key], type="todo")]),
                self.summary(
                    user_key,
                    title="Todo",
                    body="Do the thing",
                    type="todo",
                ),
            ]
        )

        service.process()
        memory = self.knowledge(service)[0].memory

        self.assertEqual(memory.status, "active")
        self.assertEqual({item["event_key"] for item in memory.sources}, {user_key, assistant_key})
        self.assertTrue(all(item["session_id"] == "s" for item in memory.sources))
        self.assertTrue(all(item["conversation_title"] != "MODEL_FORGED_TITLE" for item in memory.sources))
        self.assertTrue(all(item["turn_id"] == "t1" for item in memory.sources))

    def test_source_session_filters_incomplete_turns_and_missing_middle(self):
        backend = QueueBackend()
        service = self.service(backend)
        self.capture_turn(service, source="a", session="s1", turn="t1", user_event="a1u", assistant_event="a1a")
        self.capture_turn(service, source="a", session="s2", turn="t1", user_event="a2u", assistant_event="a2a")
        self.capture_turn(service, source="b", session="s1", turn="t1", user_event="b1u", assistant_event="b1a")
        backend.responses.extend([self.gate([])])

        result = service.process(source="a", session_id="s2")
        self.assertEqual(result["processed_turns"], 1)
        states = self.processed(service)["sessions"]
        self.assertNotIn("watermark", states["a/s1"])
        self.assertNotIn("watermark", states["b/s1"])

        gap = self.service(QueueBackend([self.gate([])]), name="gap")
        self.capture_turn(gap, turn="t1", user_event="g1u", assistant_event="g1a")
        self.capture_turn(gap, turn="t2", user_event="g2u", assistant_event=None)
        self.capture_turn(gap, turn="t3", user_event="g3u", assistant_event="g3a")

        self.assertEqual(gap.process()["processed_turns"], 1)
        self.assertEqual(gap.process()["processed_turns"], 0)
        gap_state = self.processed(gap)["sessions"]["codex/s"]
        self.assertEqual(gap_state["watermark"], 1)
        self.assertEqual(len(gap_state["processed_turns"]), 1)

    def test_orphaned_processing_marker_is_recoverable_and_remember_does_not_overwrite_live_owner(self):
        backend = QueueBackend([self.gate([])])
        service = self.service(backend)
        self.capture_turn(service)
        value = self.processed(service)
        value["sessions"]["codex/s"]["processing"] = {
            "status": "processing",
            "token": "orphan",
            "started_at": "2000-01-01T00:00:00Z",
        }
        service.vault.processed_state_path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(service.process()["processed_turns"], 1)

        live = self.processed(service)
        live["sessions"]["codex/live"] = {
            "processing": {
                "status": "processing",
                "token": "owned",
                "owner_pid": os.getpid(),
                "started_at": "2026-08-23T23:40:00Z",
            }
        }
        service.vault.processed_state_path.write_text(json.dumps(live), encoding="utf-8")
        with self.assertRaises(ProcessingError):
            service.remember("conflicting remember", source="codex", session_id="live", event_id="r")
        self.assertEqual(self.processed(service)["sessions"]["codex/live"]["processing"]["token"], "owned")

    def test_dead_processing_owner_is_recovered_immediately(self):
        backend = QueueBackend([self.gate([])])
        service = self.service(backend)
        self.capture_turn(service)
        value = self.processed(service)
        value["sessions"]["codex/s"]["processing"] = {
            "status": "processing",
            "token": "dead-owner",
            "owner_pid": 12345,
            "started_at": "2026-08-24T00:00:00Z",
        }
        service.vault.processed_state_path.write_text(json.dumps(value), encoding="utf-8")
        with patch("memleaf.process_journal.ProcessJournal._owner_pid_status", return_value=False):
            self.assertEqual(service.process()["processed_turns"], 1)

    def test_legacy_processing_marker_uses_short_grace_period(self):
        backend = QueueBackend([self.gate([])])
        service = self.service(backend)
        self.capture_turn(service)
        value = self.processed(service)
        value["sessions"]["codex/s"]["processing"] = {
            "status": "processing",
            "token": "legacy-live",
            "started_at": "2026-08-23T23:55:00Z",
        }
        service.vault.processed_state_path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(service.process()["processed_turns"], 0)
        self.clock.value += timedelta(minutes=6)
        self.assertEqual(service.process()["processed_turns"], 1)

    def test_process_and_remember_markers_record_owner_pid(self):
        service = self.service(QueueBackend([self.gate([])]))
        self.capture_turn(service)
        observed = {}

        def inspect_process(prompt, **kwargs):
            del prompt, kwargs
            processed = self.processed(service)
            observed["process"] = processed["sessions"]["codex/s"]["processing"]["owner_pid"]
            return self.gate([])

        service.router = QueueBackend([inspect_process])
        service.process()
        self.assertEqual(observed["process"], os.getpid())

        remember_service = self.service(QueueBackend([self.summary(event_key("remember-owner"))]), name="remember-owner")
        observed_remember = {}

        def inspect_remember(prompt, **kwargs):
            del prompt, kwargs
            processed = self.processed(remember_service)
            observed_remember["remember"] = processed["sessions"]["codex/remember"]["processing"]["owner_pid"]
            return self.summary(event_key("remember-owner"))

        remember_service.router = QueueBackend([inspect_remember])
        remember_service.remember("owner marker", source="codex", session_id="remember", event_id="remember-owner")
        self.assertEqual(observed_remember["remember"], os.getpid())

    def test_model_callback_can_acquire_vault_lock_because_calls_are_outside_lock(self):
        active = {"value": False}
        queue = [self.gate([])]
        service = self.service()
        original_lock = service.vault.lock

        @contextmanager
        def probe_lock():
            with original_lock():
                active["value"] = True
                try:
                    yield
                finally:
                    active["value"] = False

        class LockCheckingBackend:
            provider = "fake"
            model = "lock-check"

            def complete(self, prompt, **kwargs):
                if active["value"]:
                    raise AssertionError("model called while vault lock is held")
                with service.vault.lock():
                    pass
                return queue.pop(0)

        service.vault.lock = probe_lock
        service.router = LockCheckingBackend()
        self.capture_turn(service)

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)

    def test_model_unavailable_is_explicit_when_work_exists(self):
        service = self.service()
        self.capture_turn(service)
        with self.assertRaises(ModelUnavailable):
            service.process()
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["processing"]["status"], "failed")
