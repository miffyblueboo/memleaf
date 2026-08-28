import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.index import event_key
from memleaf.llm import ModelError, ModelUnavailable
from memleaf.processing import ProcessingError, Processor
from memleaf.validation import ModelOutputError


class QueueBackend:
    provider = "fake"
    model = "b2a-test"

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        self.calls.append({"prompt": prompt, "system": system, "purpose": purpose})
        if not self.responses:
            raise ModelError("fake queue exhausted")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(prompt, system=system, purpose=purpose, temperature=temperature)
        return response


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class StageB2ATest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.path = Path(self.tempdir.name) / "vault"

    def tearDown(self):
        self.tempdir.cleanup()

    def service(self, backend=None, native=None, name="vault"):
        path = self.path if name == "vault" else Path(self.tempdir.name) / name
        return Memleaf(path, model=backend, clock=self.clock, native_memory_reader=native)

    @staticmethod
    def gate(candidates):
        return json.dumps({"candidates": candidates})

    @staticmethod
    def candidate(
        candidate_id,
        evidence,
        *,
        memory="a durable fact",
        duplicate=False,
        worth=True,
        type="fact",
        update_memory_id=None,
    ):
        value = {
            "candidate_id": candidate_id,
            "memory": memory,
            "evidence_event_ids": list(evidence),
            "duplicate": duplicate,
            "worth": worth,
            "type": type,
            "scopes": ["global"],
            "scope_source": "model",
        }
        if update_memory_id is not None:
            value["update_memory_id"] = update_memory_id
        return value

    @staticmethod
    def summary(event_key_value, title="Fact", body="A durable fact", type="fact", **extra):
        value = {
            "title": title,
            "body": body,
            "tags": ["b2a"],
            "type": type,
            "scopes": ["global"],
            "scope_source": "model",
            "sources": [
                {
                    "event_key": event_key_value,
                    "session_id": "model-forged-session",
                    "turn_id": "model-forged-turn",
                    "conversation_title": "MODEL_FORGED_TITLE",
                }
            ],
        }
        value.update(extra)
        return json.dumps(value)

    def capture_turn(
        self,
        service,
        *,
        source="codex",
        session="s",
        turn="t1",
        user_event="u1",
        assistant_event="a1",
        user="user visible fact",
        assistant="assistant visible response",
    ):
        service.capture(source, session, turn, "user", user, event_id=user_event)
        if assistant_event is not None:
            service.capture(source, session, turn, "assistant", assistant, event_id=assistant_event)
        return event_key(user_event), event_key(assistant_event) if assistant_event else None

    def processed(self, service):
        return json.loads(service.vault.processed_index_path.read_text(encoding="utf-8"))

    def knowledge(self, service):
        return service._read_memories_unlocked("knowledge")

    def test_process_zero_candidates_is_success_and_marks_eligibility(self):
        backend = QueueBackend()
        service = self.service(backend)
        user_key, assistant_key = self.capture_turn(service)
        backend.responses.append(self.gate([]))

        result = service.process(source="codex", session_id="s")

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(len(self.knowledge(service)), 0)
        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["watermark"], 1)
        self.assertEqual(len(state["processed_turns"]), 1)
        entry = state["processed_turns"][0]
        self.assertEqual(set(entry["event_keys"]), {user_key, assistant_key})
        self.assertEqual(
            entry["eligible_cleanup_at"],
            "2026-08-25T00:00:00Z",
        )
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").exists())

    def test_two_candidates_are_separate_deterministic_memories_and_repeat_is_noop(self):
        backend = QueueBackend()
        service = self.service(backend)
        user_key, assistant_key = self.capture_turn(service)
        backend.responses.extend(
            [
                self.gate(
                    [
                        self.candidate("c1", [user_key]),
                        self.candidate("c2", [assistant_key], memory="another durable fact"),
                    ]
                ),
                self.summary(user_key, title="First"),
                self.summary(assistant_key, title="Second"),
            ]
        )

        first = service.process()
        calls_after_first = len(backend.calls)
        second = service.process()

        self.assertEqual(first["memories_written"], 2)
        self.assertEqual(len(set(first["memory_ids"])), 2)
        self.assertEqual(len(self.knowledge(service)), 2)
        self.assertEqual(second["processed_turns"], 0)
        self.assertEqual(second["memories_written"], 0)
        self.assertEqual(len(backend.calls), calls_after_first)
        self.assertEqual(len(self.knowledge(service)), 2)

    def test_draft_turn_is_discarded_and_final_email_state_is_the_only_memory(self):
        backend = QueueBackend()
        service = self.service(backend, name="email-final-only")

        draft_user, draft_assistant = self.capture_turn(
            service,
            turn="draft",
            user_event="draft-user",
            assistant_event="draft-assistant",
            user="Prepare an email to Chen Zhongkai for my confirmation.",
            assistant="The email draft is ready for confirmation.",
        )
        backend.responses.append(self.gate([]))
        draft_result = service.process()
        self.assertEqual(draft_result["memories_written"], 0)
        self.assertEqual(len(self.knowledge(service)), 0)

        final_user, final_assistant = self.capture_turn(
            service,
            turn="sent",
            user_event="sent-user",
            assistant_event="sent-assistant",
            user="Confirm sending the Chen Zhongkai email now.",
            assistant="The email was sent to Chen Zhongkai.",
        )
        final_candidate = self.candidate(
            "email-sent",
            [final_user, final_assistant],
            memory="The email to Chen Zhongkai was sent.",
            type="event",
        )
        backend.responses.extend(
            [
                self.gate([final_candidate]),
                self.summary(
                    final_user,
                    title="Chen Zhongkai email sent",
                    body="The email to Chen Zhongkai was sent.",
                    type="event",
                ),
            ]
        )
        result = service.process()

        self.assertEqual(result["memories_written"], 1)
        active = self.knowledge(service)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].memory.body, "The email to Chen Zhongkai was sent.")
        self.assertNotIn("draft", active[0].memory.body.lower())
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertEqual(
            [memory.memory_id for memory in service.search("draft", include_history=False, todo_status="all")],
            [],
        )
        found = service.search("Chen Zhongkai email", include_history=False, todo_status="all")
        self.assertEqual([memory.memory_id for memory in found], [active[0].memory.memory_id])

    def test_gate_update_target_is_forwarded_when_summary_omits_it_and_archives_old_state(self):
        backend = QueueBackend()
        service = self.service(backend, name="email-update-target")
        old = service.create_memory(
            memory_id="mem-draft",
            title="Chen Zhongkai email draft",
            body="A draft email to Chen Zhongkai is awaiting confirmation.",
            tags=["email"],
            type="event",
        )
        user_key, assistant_key = self.capture_turn(
            service,
            turn="sent",
            user_event="sent-user",
            assistant_event="sent-assistant",
            user="Confirm sending the Chen Zhongkai email.",
            assistant="The Chen Zhongkai email has been sent.",
        )
        candidate = self.candidate(
            "email-sent",
            [user_key, assistant_key],
            memory="The Chen Zhongkai email was sent.",
            type="event",
            update_memory_id=old.memory_id,
        )
        backend.responses.extend(
            [
                self.gate([candidate]),
                self.summary(
                    user_key,
                    title="Chen Zhongkai email sent",
                    body="The Chen Zhongkai email was sent.",
                    type="event",
                ),
            ]
        )

        result = service.process()

        self.assertEqual(result["memories_written"], 1)
        active = self.knowledge(service)
        self.assertEqual([record.memory.memory_id for record in active], [old.memory_id])
        self.assertEqual(active[0].memory.body, "The Chen Zhongkai email was sent.")
        history = service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.body, old.body)
        self.assertEqual(history[0].memory.extra["active_memory_id"], old.memory_id)
        self.assertEqual(
            [memory.memory_id for memory in service.search("draft", include_history=False, todo_status="all")],
            [],
        )
        self.assertEqual(
            [memory.memory_id for memory in service.search("Chen Zhongkai email", include_history=False, todo_status="all")],
            [old.memory_id],
        )

    def test_natural_project_owner_update_reuses_id_and_archives_old_state(self):
        backend = QueueBackend()
        service = self.service(backend, name="natural-owner-update")

        first_user, first_assistant = self.capture_turn(
            service,
            source="hermes",
            session="s",
            turn="owner-a",
            user_event="owner-a-user",
            assistant_event="owner-a-assistant",
            user="ML-STATE-20260827 项目负责人是甲。",
            assistant="已确认 ML-STATE-20260827 当前负责人为甲。",
        )
        backend.responses.extend(
            [
                self.gate(
                    [
                        self.candidate(
                            "ml-state-owner",
                            [first_user, first_assistant],
                            memory="ML-STATE-20260827 项目负责人是甲。",
                            type="identity",
                        )
                    ]
                ),
                self.summary(
                    first_user,
                    title="ML-STATE-20260827 项目负责人",
                    body="ML-STATE-20260827 项目负责人是甲。",
                    type="identity",
                ),
            ]
        )
        first_result = service.process(source="hermes", session_id="s")
        self.assertEqual(first_result["processed_turns"], 1)
        first_memory = self.knowledge(service)[0].memory

        second_user, second_assistant = self.capture_turn(
            service,
            source="hermes",
            session="s",
            turn="owner-b",
            user_event="owner-b-user",
            assistant_event="owner-b-assistant",
            user="ML-STATE-20260827 同一项目负责人更新为乙。",
            assistant="已确认今后以乙为准。",
        )
        update_candidate = self.candidate(
            "ml-state-owner-update",
            [second_user, second_assistant],
            memory="ML-STATE-20260827 项目负责人更新为乙。",
            type="identity",
            update_memory_id=first_memory.memory_id,
        )
        backend.responses.extend(
            [
                self.gate([update_candidate]),
                self.summary(
                    second_user,
                    title="ML-STATE-20260827 项目负责人",
                    body="ML-STATE-20260827 项目负责人已更新为乙。",
                    type="identity",
                    update_memory_id=first_memory.memory_id,
                ),
            ]
        )
        second_result = service.process(source="hermes", session_id="s")

        self.assertEqual(first_result["memory_ids"], [first_memory.memory_id])
        self.assertEqual(second_result["processed_turns"], 1)
        active = self.knowledge(service)
        self.assertEqual([record.memory.memory_id for record in active], [first_memory.memory_id])
        self.assertEqual(active[0].memory.body, "ML-STATE-20260827 项目负责人已更新为乙。")
        history = service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.body, "ML-STATE-20260827 项目负责人是甲。")
        self.assertEqual(history[0].memory.extra["active_memory_id"], first_memory.memory_id)
        self.assertEqual(
            [memory.memory_id for memory in service.search("ML-STATE-20260827 项目负责人", include_history=False, todo_status="all")],
            [first_memory.memory_id],
        )
        self.assertEqual(
            [memory.memory_id for memory in service.search("负责人是甲", include_history=False, todo_status="all")],
            [],
        )
        processed = self.processed(service)["sessions"]["hermes/s"]
        self.assertEqual(processed["watermark"], 2)
        self.assertEqual(processed["processing"]["status"], "idle")

    def test_gate_and_summary_update_target_mismatch_fails_before_writing(self):
        backend = QueueBackend()
        service = self.service(backend, name="email-update-mismatch")
        old = service.create_memory(
            memory_id="mem-draft",
            title="Email draft",
            body="A draft email is awaiting confirmation.",
            type="event",
        )
        other = service.create_memory(
            memory_id="mem-other",
            title="Other email",
            body="An unrelated email fact.",
            type="event",
        )
        user_key, _ = self.capture_turn(
            service,
            turn="sent",
            user_event="sent-user",
            assistant_event="sent-assistant",
            user="Confirm the email is sent.",
        )
        candidate = self.candidate(
            "email-sent",
            [user_key],
            memory="The email was sent.",
            type="event",
            update_memory_id=old.memory_id,
        )
        backend.responses.extend(
            [
                self.gate([candidate]),
                self.summary(user_key, title="Sent", body="The email was sent.", type="event", update_memory_id=other.memory_id),
            ]
        )

        with self.assertRaises(ModelOutputError) as raised:
            service.process()

        self.assertEqual(raised.exception.validation_detail, "invalid_update_target")
        self.assertEqual(service.read(old.memory_id).body, old.body)
        self.assertEqual(service.read(other.memory_id).body, other.body)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)

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
        processed_path = service.vault.processed_index_path
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
            Processor,
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
        user_key, assistant_key = self.capture_turn(service)
        candidates = [self.candidate("c1", [user_key]), self.candidate("c2", [assistant_key])]
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
            [self.gate(candidates), self.summary(user_key, title="first"), self.summary(assistant_key, title="second")]
        )
        self.assertEqual(service.process()["memories_written"], 2)

    def test_second_model_failure_does_not_advance_watermark(self):
        backend = QueueBackend()
        service = self.service(backend)
        user_key, assistant_key = self.capture_turn(service)
        candidates = [self.candidate("c1", [user_key]), self.candidate("c2", [assistant_key])]
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
        user_key, assistant_key = self.capture_turn(service)
        candidates = [self.candidate("c1", [user_key]), self.candidate("c2", [assistant_key])]
        responses = [self.gate(candidates), self.summary(user_key, title="first"), self.summary(assistant_key, title="second")]
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
        user_key, assistant_key = self.capture_turn(service)
        backend.responses.extend(
            [
                self.gate([self.candidate("todo", [user_key], type="todo")]),
                self.summary(
                    assistant_key,
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
        service.vault.processed_index_path.write_text(json.dumps(value), encoding="utf-8")
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
        service.vault.processed_index_path.write_text(json.dumps(live), encoding="utf-8")
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
        service.vault.processed_index_path.write_text(json.dumps(value), encoding="utf-8")
        with patch("memleaf.processing.os.kill", side_effect=ProcessLookupError):
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
        service.vault.processed_index_path.write_text(json.dumps(value), encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
