import json
import os
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest import mock

from memleaf import Memleaf
from memleaf.config import default_config, load_config, save_config
from memleaf.inbox import complete_turns, parse_inbox
from memleaf.prompts import (
    GATE_SYSTEM,
    RELATIVE_TIME_CORRECTION,
    SUMMARIZE_SYSTEM,
    gate_prompt,
    summarize_prompt,
)
from memleaf.validation import ModelOutputError
from memleaf.index import event_key, turn_key
from memleaf.llm import (
    CallableBackend,
    ClaudeCompatibleBackend,
    FakeBackend,
    ModelError,
    GeminiBackend,
    ModelRouter,
    ModelUnavailable,
    OpenAICompatibleBackend,
)
from memleaf.processing import Processor, _event_payload
from memleaf.validation import parse_gate_output, parse_summarize_output


class _Response:
    def __init__(self, value):
        self.value = json.dumps(value).encode("utf-8")
        self.closed = False

    def read(self):
        return self.value

    def close(self):
        self.closed = True


class _Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        return _Response(self.response)


class _RawResponse:
    def __init__(self, value):
        self.value = value
        self.closed = False

    def read(self):
        return self.value

    def close(self):
        self.closed = True


class _ErrorOpener:
    def __init__(self, error):
        self.error = error

    def __call__(self, request, timeout):
        del request, timeout
        raise self.error


class StageB1Test(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.vault_path = Path(self.tempdir.name) / "vault"
        self.service = Memleaf(self.vault_path)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_rebuild_preserves_session_state_and_removes_stale_events(self):
        self.service.capture("codex", "s", "turn-1", "user", "hello", event_id="live")
        processed_path = self.vault_path / "_index" / "processed.json"
        current = json.loads(processed_path.read_text(encoding="utf-8"))
        stale = "f" * 64
        current["events"][stale] = {"event_key": stale, "event_id": "should-not-survive"}
        current["event_keys"].append(stale)
        state = {
            "watermark": 7,
            "processed_turn_index": 7,
            "processing": {"status": "processing", "turn_index": 8},
            "eligible": [{"turn_index": 7, "eligible_at": "2026-01-01T00:00:00Z"}],
            "host_owned": {"opaque": True},
        }
        current["sessions"]["codex/s"] = state
        processed_path.write_text(json.dumps(current), encoding="utf-8")

        self.service.rebuild_index()

        rebuilt = json.loads(processed_path.read_text(encoding="utf-8"))
        live_key = event_key("live")
        self.assertIn(live_key, rebuilt["event_keys"])
        self.assertNotIn(stale, rebuilt["event_keys"])
        self.assertNotIn(stale, rebuilt["events"])
        self.assertEqual(rebuilt["sessions"]["codex/s"], state)

    def test_turn_key_survives_redaction_and_separates_colliding_display_ids(self):
        raw_one = "password=one"
        raw_two = "password=two"
        self.service.capture("codex", "s", raw_one, "user", "one", event_id="one-user")
        self.service.capture("codex", "s", raw_one, "assistant", "answer", event_id="one-assistant")
        self.service.capture("codex", "s", raw_two, "user", "two", event_id="two-user")
        turns = parse_inbox(self.service.vault)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0].turn_key, turn_key(raw_one))
        self.assertEqual(turns[1].turn_key, turn_key(raw_two))
        self.assertTrue(turns[0].complete)
        self.assertFalse(turns[1].complete)
        self.assertEqual(turns[0].events[0].turn_id, turns[1].events[0].turn_id)
        self.assertNotEqual(turns[0].turn_key, turns[1].turn_key)
        text = (self.vault_path / "inbox" / "codex" / "s.md").read_text(encoding="utf-8")
        self.assertNotIn(raw_one, text)
        self.assertNotIn(raw_two, text)

    def test_complete_turn_requires_exactly_one_assistant(self):
        self.service.capture("codex", "s", "turn-1", "user", "one", event_id="u1")
        self.service.capture("codex", "s", "turn-1", "assistant", "answer", event_id="a1")
        self.service.capture("codex", "s", "turn-1", "assistant", "extra", event_id="a2")
        turns = parse_inbox(self.service.vault)
        self.assertEqual(len(turns), 1)
        self.assertFalse(turns[0].complete)
        self.assertEqual(complete_turns(self.service.vault), [])

    def test_complete_turn_allows_multiple_users_before_one_assistant(self):
        self.service.capture("codex", "s", "turn-group", "user", "first", event_id="u1")
        self.service.capture("codex", "s", "turn-group", "user", "second", event_id="u2")
        self.service.capture("codex", "s", "turn-group", "assistant", "answer", event_id="a1")
        turns = parse_inbox(self.service.vault)
        self.assertEqual(len(turns), 1)
        self.assertTrue(turns[0].complete)
        self.assertEqual(complete_turns(self.service.vault), turns)

    def test_event_timestamp_is_preserved_in_extraction_payload_and_prompts(self):
        anchor = "2026-09-01T02:01:41Z"
        with mock.patch("memleaf.capture._timestamp", return_value=anchor):
            self.service.capture("codex", "calendar", "turn-1", "user", "Finish before Wednesday", event_id="calendar-user")
            self.service.capture(
                "codex",
                "calendar",
                "turn-1",
                "assistant",
                "I will track the deadline.",
                event_id="calendar-assistant",
            )

        turn = parse_inbox(self.service.vault)[0]
        events = _event_payload(turn)
        self.assertEqual([event["timestamp"] for event in events], [anchor, anchor])
        candidate = {
            "candidate_id": "calendar-deadline",
            "memory": "Finish before Wednesday",
            "evidence_event_ids": [event_key("calendar-user")],
            "duplicate": False,
            "worth": True,
            "type": "todo",
            "scopes": ["global"],
            "scope_source": "model",
        }
        gate_text = gate_prompt(events)
        summary_text = summarize_prompt(candidate, events)
        self.assertIn(anchor, gate_text)
        self.assertIn(anchor, summary_text)
        for prompt_system in (GATE_SYSTEM, SUMMARIZE_SYSTEM):
            self.assertIn("ISO-8601 UTC timestamp", prompt_system)
            self.assertIn("YYYY-MM-DD", prompt_system)
            self.assertIn("本周X/这周X/下周X/上周X", prompt_system)
            self.assertIn("every Wednesday", prompt_system)

    def test_relative_summary_is_rejected_then_absolute_retry_is_committed(self):
        anchor = "2026-09-01T02:01:41Z"
        evidence_key = event_key("relative-retry-user")
        gate = {
            "candidates": [{
                "candidate_id": "relative-deadline",
                "memory": "Complete before Wednesday",
                "evidence_event_ids": [evidence_key],
                "duplicate": False,
                "worth": True,
                "type": "todo",
                "scopes": ["global"],
                "scope_source": "model",
            }]
        }

        def summary(body):
            return {
                "title": "Project deadline",
                "body": body,
                "tags": ["deadline"],
                "type": "todo",
                "scopes": ["global"],
                "scope_source": "model",
                "sources": [{"event_key": evidence_key}],
                "status": "active",
            }

        responses = [
            json.dumps(gate),
            json.dumps(summary("在本周三（9/3）前完成。")),
            json.dumps(summary("在2026-09-02前完成。")),
        ]
        calls = []

        def callback(prompt, **kwargs):
            calls.append((prompt, kwargs["purpose"]))
            return responses.pop(0)

        with mock.patch("memleaf.capture._timestamp", return_value=anchor):
            self.service.capture("codex", "relative-retry", "turn-1", "user", "Finish before Wednesday", event_id="relative-retry-user")
            self.service.capture(
                "codex",
                "relative-retry",
                "turn-1",
                "assistant",
                "The deadline is confirmed.",
                event_id="relative-retry-assistant",
            )

        result = self.service.process(
            source="codex",
            session_id="relative-retry",
            model=FakeBackend(callback, model="relative-retry"),
        )
        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(len(calls), 3)
        self.assertEqual([purpose for _, purpose in calls], ["gate", "summarize", "summarize"])
        self.assertIn(anchor, calls[0][0])
        self.assertIn(RELATIVE_TIME_CORRECTION, calls[2][0])
        self.assertIn("event supporting each date as the anchor", calls[2][0])
        self.assertIn("every one-off calendar date must be written only as YYYY-MM-DD", calls[2][0])
        self.assertIn("parenthesized numeric date", calls[2][0])
        self.assertIn("trust the event timestamp plus the weekday meaning", calls[2][0])
        self.assertIn("Previous output violated: relative_time.", calls[2][0])
        memory = self.service.read(result["memory_ids"][0])
        self.assertIsNotNone(memory)
        self.assertIn("2026-09-02", memory.body)
        self.assertNotIn("本周三", memory.body)

    def test_recurring_weekday_expression_is_not_rejected_as_relative_date(self):
        for body in (
            "每周三例会。",
            "The recurring meeting is every Wednesday.",
            "会议时间：2026-09-01（周二）16:30。",
        ):
            with self.subTest(body=body):
                summary = {
                    "title": "Recurring meeting",
                    "body": body,
                    "tags": ["meeting"],
                    "type": "event",
                    "scopes": ["global"],
                    "sources": [{"event_key": "event-a"}],
                }
                parsed = parse_summarize_output(json.dumps(summary), current_event_keys=["event-a"])
                self.assertEqual(parsed["body"], body)

    def test_relative_summary_without_timestamp_is_rejected_and_only_title_body_are_scanned(self):
        relative = {
            "title": "截止今天",
            "body": "完成项目交付。",
            "tags": [],
            "type": "project",
            "scopes": ["global"],
            "sources": [{"event_key": "event-a", "conversation_title": "今天的会话"}],
        }
        with self.assertRaises(ModelOutputError) as raised:
            parse_summarize_output(json.dumps(relative), current_event_keys=["event-a"])
        self.assertEqual(raised.exception.validation_detail, "relative_time")

        recurring_metadata = dict(relative, title="交付安排", body="每周三例会。")
        parsed = parse_summarize_output(json.dumps(recurring_metadata), current_event_keys=["event-a"])
        self.assertEqual(parsed["sources"][0]["conversation_title"], "今天的会话")

    def test_three_relative_summary_failures_keep_watermark_and_inbox(self):
        anchor = "2026-09-01T02:01:41Z"
        evidence_key = event_key("relative-failure-user")
        gate = {
            "candidates": [{
                "candidate_id": "relative-failure",
                "memory": "Complete before Wednesday",
                "evidence_event_ids": [evidence_key],
                "duplicate": False,
                "worth": True,
                "type": "todo",
                "scopes": ["global"],
                "scope_source": "model",
            }]
        }
        relative_summary = {
            "title": "Project deadline",
            "body": "在本周三前完成。",
            "tags": ["deadline"],
            "type": "todo",
            "scopes": ["global"],
            "scope_source": "model",
            "sources": [{"event_key": evidence_key}],
            "status": "active",
        }
        responses = [json.dumps(gate)] + [json.dumps(relative_summary)] * 3
        calls = []

        def callback(prompt, **kwargs):
            calls.append(kwargs["purpose"])
            return responses.pop(0)

        with mock.patch("memleaf.capture._timestamp", return_value=anchor):
            self.service.capture("codex", "relative-failure", "turn-1", "user", "Finish before Wednesday", event_id="relative-failure-user")
            self.service.capture(
                "codex",
                "relative-failure",
                "turn-1",
                "assistant",
                "The deadline is confirmed.",
                event_id="relative-failure-assistant",
            )

        with self.assertRaises(ModelOutputError) as raised:
            self.service.process(
                source="codex",
                session_id="relative-failure",
                model=FakeBackend(callback, model="relative-failure"),
            )
        self.assertEqual(raised.exception.validation_detail, "relative_time")
        self.assertEqual(raised.exception.attempt_count, 3)
        self.assertEqual(calls, ["gate", "summarize", "summarize", "summarize"])
        processed = json.loads(self.service.vault.processed_index_path.read_text(encoding="utf-8"))
        state = processed["sessions"]["codex/relative-failure"]
        self.assertEqual(state.get("watermark", 0), 0)
        self.assertEqual(state["processing"]["status"], "failed")
        inbox = self.vault_path / "inbox" / "codex" / "relative-failure.md"
        self.assertTrue(inbox.is_file())
        self.assertIn("Finish before Wednesday", inbox.read_text(encoding="utf-8"))

    def test_process_retries_schema_violation_until_third_gate_attempt_and_commits(self):
        responses = [
            json.dumps({"candidates": "invalid"}),
            json.dumps({"candidates": "invalid"}),
            json.dumps({"candidates": []}),
        ]
        calls = []

        def callback(prompt, **kwargs):
            calls.append((prompt, kwargs))
            return responses.pop(0)

        backend = FakeBackend(callback, model="schema-retry")
        self.service.capture("codex", "schema-retry", "turn-1", "user", "visible user", event_id="u1")
        self.service.capture(
            "codex",
            "schema-retry",
            "turn-1",
            "assistant",
            "visible assistant",
            event_id="a1",
        )

        result = self.service.process(source="codex", session_id="schema-retry", model=backend)

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(len(calls), 3)
        self.assertEqual([kwargs["purpose"] for _, kwargs in calls], ["gate", "gate", "gate"])
        self.assertTrue(all("Correction:" in prompt for prompt, _ in calls[1:]))
        state = json.loads(self.service.vault.processed_index_path.read_text(encoding="utf-8"))["sessions"][
            "codex/schema-retry"
        ]
        self.assertEqual(state["watermark"], 1)
        self.assertEqual(state["processing"]["status"], "idle")

    def test_process_three_schema_violations_fail_with_complete_gate_diagnostics(self):
        invalid = json.dumps({"candidates": "invalid"})
        responses = [invalid, invalid, invalid]
        calls = []

        def callback(prompt, **kwargs):
            calls.append((prompt, kwargs))
            return responses.pop(0)

        backend = FakeBackend(callback, model="schema-failure")
        self.service.capture("codex", "schema-failure", "turn-1", "user", "visible user", event_id="u1")
        self.service.capture(
            "codex",
            "schema-failure",
            "turn-1",
            "assistant",
            "visible assistant",
            event_id="a1",
        )

        with self.assertRaises(ModelOutputError) as raised:
            self.service.process(source="codex", session_id="schema-failure", model=backend)

        error = raised.exception
        self.assertEqual(error.stage, "gate")
        self.assertEqual(error.validation_reason, "schema_violation")
        self.assertEqual(error.attempt_count, 3)
        processed = json.loads(self.service.vault.processed_index_path.read_text(encoding="utf-8"))
        marker = processed["sessions"]["codex/schema-failure"]["processing"]
        self.assertEqual(marker["status"], "failed")
        self.assertTrue(
            {
                "status",
                "token",
                "turn_keys",
                "turn_indices",
                "failed_at",
                "failure_code",
                "failure_stage",
                "validation_reason",
                "validation_detail",
                "attempt_count",
            }.issubset(marker)
        )
        self.assertEqual(marker["failure_code"], "model_invalid_response")
        self.assertEqual(marker["failure_stage"], "gate")
        self.assertEqual(marker["validation_reason"], "schema_violation")
        self.assertEqual(marker["validation_detail"], "root_shape")
        self.assertEqual(marker["attempt_count"], 3)
        self.assertEqual(processed["sessions"]["codex/schema-failure"].get("watermark", 0), 0)
        self.assertEqual(len(calls), 3)

    def test_capture_keeps_processing_owned_session_fields(self):
        processed_path = self.vault_path / "_index" / "processed.json"
        value = json.loads(processed_path.read_text(encoding="utf-8"))
        owned = {
            "watermark": 3,
            "processed_turn_index": 3,
            "processing": {"status": "processing", "turn_index": 4, "token": "opaque"},
            "host_state": {"keep": [1, 2, 3]},
        }
        value["sessions"] = {"codex/s": owned}
        processed_path.write_text(json.dumps(value), encoding="utf-8")
        self.service.capture("codex", "s", "turn-4", "user", "pending", event_id="pending")
        after = json.loads(processed_path.read_text(encoding="utf-8"))
        kept = after["sessions"]["codex/s"]
        for key, item in owned.items():
            self.assertEqual(kept[key], item)

    def test_parser_never_pairs_legacy_v1_or_forged_body_marker(self):
        legacy_a = event_key("legacy-a")
        legacy_b = event_key("legacy-b")
        legacy = (
            "# old\n"
            f"<!-- memleaf:event-key:v1:{legacy_a} -->\n"
            f"<!-- memleaf:event-key:v1:{legacy_b} -->\n"
        )
        parsed_legacy = parse_inbox(self._write_inbox("legacy", "old", legacy))
        self.assertEqual(len(parsed_legacy), 2)
        self.assertTrue(all(item.legacy and not item.complete for item in parsed_legacy))

        fake = f"<!-- memleaf:event:v2 -->\n{{\"event_key\":\"{event_key('fake')}\"}}\n"
        stored = self.service.capture("codex", "s", "turn-1", "user", fake, event_id="real")
        self.service.rebuild_index()
        self.assertEqual([item.event_key for item in parse_inbox(self.service.vault)[0].events], [event_key("real")])
        self.assertTrue(stored.stored)

    def _write_inbox(self, source, session, text):
        path = self.vault_path / "inbox" / source
        path.mkdir(parents=True, exist_ok=True)
        file_path = path / f"{session}.md"
        file_path.write_text(text, encoding="utf-8")
        return file_path


class RouterAndAdapterTest(unittest.TestCase):
    @staticmethod
    def _request_payload(provider, purpose="gate"):
        opener = _Opener({"choices": [{"message": {"content": "{}"}}]})
        router = ModelRouter.from_config(
            {
                "llm": {
                    "mode": "api",
                    "provider": provider,
                    "protocol": "openai",
                    "base_url": "https://example.invalid/v1",
                    "api_key": "test-secret",
                    "model": "small-model",
                }
            }
        )
        router.api._opener = opener
        router.complete("prompt", purpose=purpose)
        return json.loads(opener.requests[0][0].data.decode("utf-8"))

    def test_supported_openai_json_mode_adds_response_format_and_token_limit(self):
        for provider in ("openai", "deepseek"):
            for purpose in ("gate", "summarize"):
                with self.subTest(provider=provider, purpose=purpose):
                    payload = self._request_payload(provider, purpose)
                    self.assertEqual(payload["response_format"], {"type": "json_object"})
                    self.assertEqual(payload["max_tokens"], 4096)
                    if provider == "deepseek":
                        self.assertEqual(payload["thinking"], {"type": "disabled"})
                    else:
                        self.assertNotIn("thinking", payload)

    def test_deepseek_thinking_is_disabled_only_for_json_extraction_stages(self):
        compact = self._request_payload("deepseek", "compact")
        self.assertNotIn("thinking", compact)
        regular = self._request_payload("deepseek", "chat")
        self.assertNotIn("response_format", regular)
        self.assertNotIn("thinking", regular)

    def test_invalid_json_extraction_retries_twice_then_stops(self):
        schema_error = ModelOutputError(
            "invalid schema",
            validation_reason="schema_violation",
        )
        self.assertTrue(Processor._allows_next_json_attempt(schema_error, 1))
        self.assertTrue(Processor._allows_next_json_attempt(schema_error, 2))
        self.assertFalse(Processor._allows_next_json_attempt(schema_error, 3))
        self.assertFalse(
            Processor._allows_next_json_attempt(
                ModelError("timeout", code="model_timeout"),
                1,
            )
        )

    def test_openai_response_empty_content_has_safe_finish_and_usage_diagnostics(self):
        opener = _Opener(
            {
                "id": "RESPONSE_ID_SECRET",
                "model": "MODEL_NAME_SECRET",
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {
                            "content": "",
                            "reasoning_content": "REASONING_CONTENT_SECRET",
                        },
                    }
                ],
                "usage": {"completion_tokens": 4096},
            }
        )
        backend = OpenAICompatibleBackend(
            base_url="https://provider.invalid/v1",
            api_key="API_KEY_SECRET",
            model="model",
            opener=opener,
            json_mode=True,
        )
        with self.assertRaises(ModelError) as raised:
            backend.complete("prompt", purpose="gate")
        error = raised.exception
        self.assertEqual(error.code, "model_invalid_response")
        self.assertEqual(error.validation_reason, "empty_content")
        self.assertEqual(
            error.response_diagnostics,
            {
                "finish_reason": "length",
                "completion_tokens": 4096,
                "content_present": False,
                "content_chars": 0,
                "reasoning_present": True,
                "reasoning_chars": len("REASONING_CONTENT_SECRET"),
            },
        )
        self.assertNotIn("SECRET", str(error))

    def test_openai_response_diagnostics_safely_normalize_untrusted_values(self):
        opener = _Opener(
            {
                "choices": [
                    {
                        "finish_reason": "UNTRUSTED_FINISH_REASON",
                        "message": {"content": ""},
                    }
                ],
                "usage": {"completion_tokens": 99_999_999},
            }
        )
        backend = OpenAICompatibleBackend(
            base_url="https://provider.invalid/v1",
            api_key="key",
            model="model",
            opener=opener,
        )
        with self.assertRaises(ModelError) as raised:
            backend.complete("prompt", purpose="summarize")
        self.assertEqual(raised.exception.response_diagnostics["finish_reason"], "unknown")
        self.assertIsNone(raised.exception.response_diagnostics["completion_tokens"])

    def test_deepseek_finish_reason_insufficient_system_resource_is_preserved(self):
        opener = _Opener(
            {
                "choices": [
                    {
                        "finish_reason": "insufficient_system_resource",
                        "message": {"content": "   "},
                    }
                ],
                "usage": {"completion_tokens": 4096},
            }
        )
        backend = OpenAICompatibleBackend(
            base_url="https://provider.invalid/v1",
            api_key="key",
            model="model",
            opener=opener,
            json_mode=True,
            provider_name="deepseek",
        )
        with self.assertRaises(ModelError) as raised:
            backend.complete("prompt", purpose="gate")
        self.assertEqual(raised.exception.validation_reason, "empty_content")
        self.assertEqual(
            raised.exception.response_diagnostics["finish_reason"],
            "insufficient_system_resource",
        )
        self.assertEqual(raised.exception.response_diagnostics["completion_tokens"], 4096)

    def test_openai_whitespace_content_keeps_diagnostics_and_allows_third_attempt(self):
        responses = [
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "   "},
                    }
                ],
                "usage": {"completion_tokens": 4096},
            },
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "\t  "},
                    }
                ],
                "usage": {"completion_tokens": 4096},
            },
            {"choices": [{"finish_reason": "stop", "message": {"content": '{"candidates":[]}'}}]},
        ]

        def opener(request, timeout):
            del request, timeout
            return _Response(responses.pop(0))

        backend = OpenAICompatibleBackend(
            base_url="https://provider.invalid/v1",
            api_key="key",
            model="model",
            opener=opener,
            json_mode=True,
            provider_name="deepseek",
        )
        with tempfile.TemporaryDirectory() as temporary:
            service = Memleaf(Path(temporary) / "vault", model=backend)
            service.capture("codex", "whitespace", "t1", "user", "visible user", event_id="wu")
            service.capture("codex", "whitespace", "t1", "assistant", "visible assistant", event_id="wa")
            result = service.process(source="codex", session_id="whitespace")

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(responses, [])

    def test_unknown_openai_compatible_provider_keeps_legacy_request_shape(self):
        payload = self._request_payload("custom-compatible")
        self.assertNotIn("response_format", payload)
        self.assertNotIn("max_tokens", payload)

    def test_gate_and_summary_prompts_include_parseable_minimal_examples(self):
        for value in ("preference", "fact", "project", "todo", "event", "identity", "other", "null"):
            self.assertIn(value, GATE_SYSTEM)
        for value in ("model", "user", "session_context", "insufficient_context"):
            self.assertIn(value, GATE_SYSTEM)
        self.assertIn("30", GATE_SYSTEM)
        self.assertIn("event_key", GATE_SYSTEM)
        self.assertIn("turn_id", GATE_SYSTEM)
        self.assertIn("event_id", GATE_SYSTEM)
        self.assertNotIn("event-key-placeholder", GATE_SYSTEM)
        for value in ("preference", "fact", "project", "todo", "event", "identity", "other"):
            self.assertIn(value, SUMMARIZE_SYSTEM)
        self.assertIn("event_key", SUMMARIZE_SYSTEM)
        self.assertIn("completed_at", SUMMARIZE_SYSTEM)
        events = [{"event_key": "event-key-real", "role": "user", "content": "visible fact"}]
        candidate = {
            "candidate_id": "todo-1",
            "memory": "a supported task",
            "evidence_event_ids": ["event-key-real"],
            "duplicate": False,
            "worth": True,
            "type": "todo",
            "scopes": ["project:demo"],
            "scope_source": "user",
        }
        gate_text = gate_prompt(events)
        summary_text = summarize_prompt(candidate, events)
        self.assertNotIn("event-key-placeholder", GATE_SYSTEM + SUMMARIZE_SYSTEM + gate_text + summary_text)
        decoder = json.JSONDecoder()

        def example(prompt):
            start = prompt.index("Minimal valid JSON example")
            start = prompt.index("{", start)
            return decoder.raw_decode(prompt[start:])[0]

        gate_example = example(gate_text)
        summary_example = example(summary_text)
        gate_key = gate_example["candidates"][0]["evidence_event_ids"][0]
        summary_key = summary_example["sources"][0]["event_key"]
        self.assertEqual(gate_key, "event-key-real")
        self.assertEqual(summary_key, "event-key-real")
        self.assertEqual(summary_example["type"], "todo")
        self.assertEqual(summary_example["scopes"], ["project:demo"])
        self.assertEqual(summary_example["scope_source"], "user")
        self.assertEqual(
            parse_gate_output(json.dumps(gate_example), current_event_keys=[gate_key]),
            gate_example,
        )
        parsed_summary = parse_summarize_output(json.dumps(summary_example), current_event_keys=[summary_key])
        self.assertEqual(parsed_summary["title"], summary_example["title"])
        self.assertEqual(parsed_summary["sources"][0]["event_key"], summary_key)
        self.assertNotIn("Minimal valid JSON example", gate_prompt([]))

    def test_gate_worth_is_based_on_future_reuse_not_content_category(self):
        gate_text = GATE_SYSTEM.lower()
        normalized_gate_text = " ".join(gate_text.split())
        for phrase in (
            "reasonable, concrete future reuse",
            "later answer or action",
            "forget a commitment",
            "repeat an investigation",
            "repeat a mistake",
            "no plausible future use",
            "one candidate for one future use",
            "zero or one candidate",
            "draft awaiting confirmation",
            "temporary error",
            "unconfirmed suggestion",
            "independent future use",
            "email body/signature/contact details",
            "test pass/fail",
            "audit findings",
            "verification procedures",
            "operational health/status",
            "assistant's own summary",
            "status marker",
        ):
            self.assertIn(phrase, normalized_gate_text)
        self.assertIn("content type, source, and form do not decide worth", normalized_gate_text)
        for category in ("email", "daily report", "troubleshooting result", "tool result", "process detail"):
            self.assertIn(category, normalized_gate_text)
        self.assertIn("may be worth keeping or discarding", normalized_gate_text)
        self.assertIn("bypasses", normalized_gate_text)
        self.assertNotIn("never keep email", normalized_gate_text)
        self.assertNotIn("always keep troubleshooting", normalized_gate_text)

    def test_gate_and_summary_use_one_complete_future_use_topic(self):
        gate_text = GATE_SYSTEM.lower()
        normalized_gate_text = " ".join(gate_text.split())
        for phrase in (
            "smallest complete",
            "same entity or project",
            "list, overall state, subset",
            "progress, deadline, and next step",
            "independently retrieved and updated",
            "adjacent overlapping snapshots",
            "ordinary turns should produce zero or one candidate",
            "genuinely independent future questions or actions",
        ):
            self.assertIn(phrase, normalized_gate_text)

        summary_text = SUMMARIZE_SYSTEM.lower()
        normalized_summary_text = " ".join(summary_text.split())
        for phrase in (
            "independently retrievable and updateable future-use topic",
            "related active memleaf memories",
            "future question/action",
            "set update_memory_id",
            "retain still-valid information",
            "replace",
            "latest confirmed state",
            "keep type identical",
            "adjacent",
            "new sibling memory",
            "genuinely different",
            "future question/action",
            "email's body",
            "temporary path",
            "file byte count",
            "message id",
            "test pass/fail",
            "audit conclusion",
            "verification procedure",
            "operational health/status",
            "assistant-only claim",
        ):
            self.assertIn(phrase, normalized_summary_text)

    def test_summarize_update_target_must_be_a_related_active_memory_when_supplied(self):
        summary = {
            "title": "Project status",
            "body": "The project is ready for delivery.",
            "tags": ["project"],
            "type": "project",
            "scopes": ["project:demo"],
            "sources": [{"event_key": "event-a"}],
            "update_memory_id": "mem-related",
        }
        parsed = parse_summarize_output(
            json.dumps(summary),
            current_event_keys=["event-a"],
            related_memory_ids=["MEM-RELATED"],
        )
        self.assertEqual(parsed["update_memory_id"], "mem-related")

        with self.assertRaises(ModelOutputError) as raised:
            parse_summarize_output(
                json.dumps(summary),
                current_event_keys=["event-a"],
                related_memory_ids=["mem-other"],
            )
        self.assertEqual(raised.exception.validation_detail, "invalid_update_target")

        # Direct callers that do not provide related IDs retain the old
        # validation behavior; MemoryWriter still verifies active/type safety.
        self.assertEqual(
            parse_summarize_output(json.dumps(summary), current_event_keys=["event-a"])["update_memory_id"],
            "mem-related",
        )

    def test_request_timeout_defaults_are_merged_and_router_passes_120_seconds(self):
        self.assertEqual(default_config()["llm"]["request_timeout"], 120)
        self.assertFalse(default_config()["llm"]["diagnostic_logging"])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.yaml"
            path.write_text("llm:\n  mode: auto\n", encoding="utf-8")
            loaded = load_config(path, vault=Path(temporary))
            self.assertEqual(loaded["llm"]["request_timeout"], 120)
            self.assertFalse(loaded["llm"]["diagnostic_logging"])
            config = default_config(Path(temporary))
            config["llm"].pop("diagnostic_logging")
            save_config(path, config)
            self.assertFalse(load_config(path, vault=Path(temporary))["llm"]["diagnostic_logging"])
            path.write_text("llm:\n  diagnostic_logging: yes\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path, vault=Path(temporary))

        opener = _Opener({"choices": [{"message": {"content": "ok"}}]})
        router = ModelRouter.from_config(
            {
                "llm": {
                    "mode": "api",
                    "provider": "openai",
                    "protocol": "openai",
                    "base_url": "https://example.invalid/v1",
                    "api_key": "test-secret",
                    "model": "small-model",
                    "request_timeout": 120,
                }
            }
        )
        self.assertEqual(router.api.timeout, 120)
        router.api._opener = opener
        self.assertEqual(router.complete("prompt", purpose="gate"), "ok")
        self.assertEqual(opener.requests[0][1], 120)

    def test_request_timeout_bounds_are_enforced_for_old_and_new_configs(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.yaml"
            for value in (1, 240):
                path.write_text(f"llm:\n  request_timeout: {value}\n", encoding="utf-8")
                self.assertEqual(load_config(path, vault=Path(temporary))["llm"]["request_timeout"], value)
            for value in (0, 241, "false"):
                path.write_text(f"llm:\n  request_timeout: {value}\n", encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config(path, vault=Path(temporary))

    def test_http_model_errors_are_classified_without_provider_text(self):
        cases = (
            (TimeoutError("TIMEOUT_SECRET"), "model_timeout"),
            (urllib.error.HTTPError("https://example.invalid", 401, "AUTH_SECRET", {}, None), "model_auth_failed"),
            (urllib.error.HTTPError("https://example.invalid", 429, "RATE_SECRET", {}, None), "model_rate_limited"),
            (urllib.error.URLError("NETWORK_SECRET"), "model_network_error"),
        )
        for error, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                backend = OpenAICompatibleBackend(
                    base_url="https://example.invalid/v1",
                    api_key="key",
                    model="model",
                    opener=_ErrorOpener(error),
                )
                with self.assertRaises(ModelError) as raised:
                    backend.complete("prompt", purpose="gate")
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(raised.exception.stage, "gate")
                self.assertNotIn("SECRET", str(raised.exception))

        backend = OpenAICompatibleBackend(
            base_url="https://example.invalid/v1",
            api_key="key",
            model="model",
            opener=lambda request, timeout: _RawResponse(b"INVALID_RESPONSE_SECRET"),
        )
        with self.assertRaises(ModelError) as raised:
            backend.complete("prompt", purpose="summarize")
        self.assertEqual(raised.exception.code, "model_invalid_response")
        self.assertEqual(raised.exception.stage, "summarize")
        self.assertNotIn("INVALID_RESPONSE_SECRET", str(raised.exception))

    def test_from_config_applies_mode_and_auto_routing(self):
        calls = []

        def host(prompt):
            calls.append("host")
            return "host-result"

        def api(prompt):
            calls.append("api")
            return "api-result"

        router = ModelRouter.from_config(
            {"llm": {"mode": "auto"}},
            host=FakeBackend(host, model="h"),
            api=FakeBackend(api, model="a"),
        )
        self.assertEqual(router.mode, "auto")
        self.assertEqual(router.complete("secret prompt"), "host-result")
        self.assertEqual(calls, ["host"])

        router = ModelRouter.from_config(
            {"llm": {"mode": "host"}},
            host=FakeBackend(host),
            api=FakeBackend(api),
        )
        self.assertEqual(router.mode, "host")
        self.assertEqual(router.complete("x"), "host-result")

    def test_auto_falls_back_only_after_host_failure_and_unavailable_is_explicit(self):
        calls = []

        def bad(prompt):
            calls.append("host")
            raise RuntimeError("failure")

        def good(prompt):
            calls.append("api")
            return "api"

        router = ModelRouter(mode="auto", host=FakeBackend(bad), api=FakeBackend(good))
        self.assertEqual(router.complete("x"), "api")
        self.assertEqual(calls, ["host", "api"])
        self.assertEqual(router.diagnostics[-1]["reason"], "host_failed")

        unavailable = ModelRouter.from_config({"llm": {"mode": "auto"}})
        with self.assertRaises(ModelUnavailable):
            unavailable.complete("x")

    def test_api_key_prefers_direct_config_and_keeps_legacy_environment_fallback(self):
        old = os.environ.pop("MEMLEAF_B1_KEY", None)
        try:
            config = {
                "llm": {
                    "mode": "api",
                    "provider": "openai",
                    "protocol": "openai",
                    "base_url": "https://example.invalid/v1",
                    "api_key_env": "MEMLEAF_B1_KEY",
                    "api_key": "direct-secret",
                    "model": "fake-model",
                }
            }
            router = ModelRouter.from_config(config)
            self.assertEqual(router.api.api_key, "direct-secret")
            os.environ["MEMLEAF_B1_KEY"] = "env-secret"
            router = ModelRouter.from_config(config)
            self.assertEqual(router.mode, "api")
            self.assertEqual(router.api.api_key, "direct-secret")
            legacy = dict(config["llm"])
            legacy["api_key"] = ""
            router = ModelRouter.from_config({"llm": legacy})
            self.assertEqual(router.api.api_key, "env-secret")
        finally:
            if old is not None:
                os.environ["MEMLEAF_B1_KEY"] = old
            else:
                os.environ.pop("MEMLEAF_B1_KEY", None)

    def test_callable_backend_inspects_signature_and_calls_at_most_once(self):
        calls = []

        def callback(prompt):
            calls.append(prompt)
            raise TypeError("internal callback error")

        with self.assertRaises(TypeError):
            CallableBackend(callback).complete("prompt")
        self.assertEqual(calls, ["prompt"])

    def test_http_adapters_use_expected_requests_without_network(self):
        opener = _Opener({"choices": [{"message": {"content": "openai"}}]})
        result = OpenAICompatibleBackend(
            base_url="https://example.invalid/v1", api_key="key", model="m", opener=opener
        ).complete("prompt", system="system")
        request = opener.requests[0][0]
        self.assertEqual(result, "openai")
        self.assertEqual(request.full_url, "https://example.invalid/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer key")
        self.assertEqual(json.loads(request.data.decode("utf-8"))["messages"][0], {"role": "system", "content": "system"})

        opener = _Opener({"content": [{"type": "text", "text": "claude"}]})
        result = ClaudeCompatibleBackend(
            base_url="https://example.invalid", api_key="key", model="m", opener=opener
        ).complete("prompt", system="system")
        request = opener.requests[0][0]
        self.assertEqual(result, "claude")
        self.assertEqual(request.full_url, "https://example.invalid/v1/messages")
        self.assertEqual(request.get_header("X-api-key"), "key")
        self.assertEqual(json.loads(request.data.decode("utf-8"))["system"], "system")

        opener = _Opener({"candidates": [{"content": {"parts": [{"text": "gemini"}]}}]})
        result = GeminiBackend(
            base_url="https://example.invalid", api_key="key", model="gemini-test", opener=opener
        ).complete("prompt")
        request = opener.requests[0][0]
        self.assertEqual(result, "gemini")
        self.assertIn("/v1beta/models/gemini-test:generateContent?key=key", request.full_url)
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(json.loads(request.data.decode("utf-8"))["contents"][0]["parts"][0]["text"], "prompt")


class ValidationTest(unittest.TestCase):
    def test_gate_accepts_zero_candidates_and_rejects_bad_evidence_or_types(self):
        self.assertEqual(parse_gate_output('{"candidates":[]}', ["event-a"]), {"candidates": []})
        valid = {
            "candidates": [{
                "candidate_id": "c1",
                "memory": "user prefers dark mode",
                "evidence_event_ids": ["event-a"],
                "duplicate": False,
                "worth": True,
                "type": "preference",
                "scopes": ["global"],
                "scope_source": "model",
            }]
        }
        self.assertEqual(parse_gate_output(json.dumps(valid), ["event-a"])["candidates"][0]["candidate_id"], "c1")
        for mutation in (
            {"evidence_event_ids": ["other"]},
            {"duplicate": 1},
            {"type": "unsupported"},
            {"scopes": ["unscoped"], "scope_source": "model"},
            {"extra": "semantic"},
        ):
            bad = json.loads(json.dumps(valid))
            bad["candidates"][0].update(mutation)
            with self.assertRaises(ModelOutputError):
                parse_gate_output(json.dumps(bad), ["event-a"])

    def test_gate_schema_details_are_specific_and_multiple_types_remain_valid(self):
        base = {
            "candidate_id": "c1",
            "memory": "supported memory",
            "evidence_event_ids": ["event-a"],
            "duplicate": False,
            "worth": True,
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
        }
        cases = (
            (dict(base, type="requirement"), "invalid_type"),
            (dict(base, reason="R" * 31), "reason_too_long"),
            (dict(base, evidence_event_ids=["wrong-event"]), "invalid_evidence"),
        )
        for candidate, detail in cases:
            with self.subTest(detail=detail):
                with self.assertRaises(ModelOutputError) as raised:
                    parse_gate_output(json.dumps({"candidates": [candidate]}), ["event-a"])
                self.assertEqual(raised.exception.validation_detail, detail)
                self.assertNotIn("wrong-event", str(raised.exception))

        valid = {
            "candidates": [
                dict(base, candidate_id="project", type="project"),
                dict(base, candidate_id="todo", type="todo"),
                dict(base, candidate_id="fact", type="fact"),
            ]
        }
        self.assertEqual(len(parse_gate_output(json.dumps(valid), ["event-a"])["candidates"]), 3)
        self.assertIsNone(ModelOutputError("secret", validation_detail="not-a-detail").validation_detail)

    def test_strict_json_rejects_fences_tail_and_summary_requires_atomic_fields(self):
        for raw in ("```json\n{\"candidates\":[]}\n```", '{"candidates":[]} trailing'):
            with self.assertRaises(ModelOutputError):
                parse_gate_output(raw, [])
        summary = {
            "title": "Dark mode",
            "body": "User prefers dark mode.",
            "tags": ["preference"],
            "type": "preference",
            "scopes": ["global"],
            "sources": [{"event_key": "event-a"}],
            "update_memory_id": "mem-old",
        }
        self.assertEqual(parse_summarize_output(json.dumps(summary), current_event_keys=["event-a"])["title"], "Dark mode")
        for key in ("title", "body", "tags", "type", "scopes", "sources"):
            bad = dict(summary)
            del bad[key]
            with self.assertRaises(ModelOutputError):
                parse_summarize_output(json.dumps(bad), current_event_keys=["event-a"])

        multiline = dict(summary, body="first line\nsecond line")
        self.assertIn("\n", parse_summarize_output(json.dumps(multiline), current_event_keys=["event-a"])["body"])
        with self.assertRaises(ModelOutputError):
            parse_summarize_output(
                json.dumps(dict(summary, title="bad\ntitle")),
                current_event_keys=["event-a"],
            )

    def test_summary_todo_fields_and_scope_constraints_are_strict(self):
        base = {
            "title": "Do it",
            "body": "Do the thing.",
            "tags": [],
            "type": "todo",
            "scopes": ["global"],
            "sources": [{"event_key": "event-a"}],
        }
        completed = dict(base, status="completed", completed_at="2026-01-01T00:00:00Z")
        self.assertEqual(parse_summarize_output(json.dumps(completed), current_event_keys=["event-a"])["status"], "completed")
        for bad in (
            dict(base, status="completed"),
            dict(base, status="active", completed_at="2026-01-01T00:00:00Z"),
            dict(base, scopes=[], scope_source="model"),
            dict(base, status="unknown"),
            dict(base, unknown_semantic=True),
        ):
            with self.assertRaises(ModelOutputError):
                parse_summarize_output(json.dumps(bad), current_event_keys=["event-a"])

    def test_gate_rejects_duplicate_ids_conflicting_flags_and_null_worth_type(self):
        base = {
            "candidate_id": "same",
            "memory": "fact",
            "evidence_event_ids": ["event-a"],
            "duplicate": False,
            "worth": True,
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
        }
        duplicate_ids = {"candidates": [base, dict(base)]}
        with self.assertRaises(ModelOutputError):
            parse_gate_output(json.dumps(duplicate_ids), ["event-a"])
        for mutation in (
            {"duplicate": True, "worth": True},
            {"type": None},
        ):
            bad = {"candidates": [dict(base, **mutation)]}
            with self.assertRaises(ModelOutputError):
                parse_gate_output(json.dumps(bad), ["event-a"])

    def test_gate_update_target_is_only_for_a_related_active_worthy_candidate(self):
        base = {
            "candidate_id": "state-change",
            "memory": "the email was sent",
            "evidence_event_ids": ["event-a"],
            "duplicate": False,
            "worth": True,
            "type": "event",
            "scopes": ["global"],
            "scope_source": "model",
            "update_memory_id": "mem-draft",
        }
        parsed = parse_gate_output(
            json.dumps({"candidates": [base]}),
            current_event_keys=["event-a"],
            related_memory_ids=["mem-draft"],
        )
        self.assertEqual(parsed["candidates"][0]["update_memory_id"], "mem-draft")

        invalid_cases = (
            (dict(base, update_memory_id="mem-unknown"), ["mem-draft"]),
            (dict(base, worth=False, type=None), ["mem-draft"]),
            (dict(base, duplicate=True, worth=False, type=None), ["mem-draft"]),
            (dict(base, duplicate_memory_id="mem-draft"), ["mem-draft"]),
            (dict(base), None),
        )
        for candidate, related in invalid_cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ModelOutputError) as raised:
                    parse_gate_output(
                        json.dumps({"candidates": [candidate]}),
                        current_event_keys=["event-a"],
                        related_memory_ids=related,
                    )
                self.assertEqual(raised.exception.validation_detail, "invalid_update_target")

    def test_scopes_allow_only_safe_known_forms(self):
        base = {
            "candidate_id": "c",
            "memory": "fact",
            "evidence_event_ids": ["event-a"],
            "duplicate": False,
            "worth": True,
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
        }
        for scope in ("domain:work", "portfolio:alpha_1", "project:中文项目"):
            valid = {"candidates": [dict(base, scopes=[scope])]}
            self.assertEqual(parse_gate_output(json.dumps(valid), ["event-a"])["candidates"][0]["scopes"], [scope])
        invalid = (
            ("project:", "model"),
            ("project:a/b", "model"),
            ("project:a b", "model"),
            ("team:unknown", "model"),
            ("unscoped", "model"),
            ("", "insufficient_context"),
        )
        for scope, source in invalid:
            with self.assertRaises(ModelOutputError):
                parse_gate_output(
                    json.dumps({"candidates": [dict(base, scopes=[] if scope == "" else [scope], scope_source=source)]}),
                    ["event-a"],
                )
        with self.assertRaises(ModelOutputError):
            parse_gate_output(
                json.dumps({"candidates": [dict(base, scopes=["global", "unscoped"], scope_source="insufficient_context")]}),
                ["event-a"],
            )

    def test_summary_evidence_and_sources_are_limited_to_current_turn_fields(self):
        summary = {
            "title": "Traceable",
            "body": "Body",
            "tags": [],
            "type": "fact",
            "scopes": ["global"],
            "sources": [{
                "event_key": "event-a",
                "session_id": "s",
                "turn_id": "t",
                "conversation_title": "Conversation",
                "evidence_event_ids": ["event-b"],
            }],
            "evidence_event_ids": ["event-a"],
        }
        self.assertEqual(
            parse_summarize_output(json.dumps(summary), current_event_keys=["event-a", "event-b"])["title"],
            "Traceable",
        )
        for bad in (
            dict(summary, evidence_event_ids=["other"]),
            dict(summary, sources=[dict(summary["sources"][0], event_key="other")]),
            dict(summary, sources=[dict(summary["sources"][0], evidence_event_ids=["other"])]),
            dict(summary, sources=[dict(summary["sources"][0], arbitrary="not allowed")]),
        ):
            with self.assertRaises(ModelOutputError):
                parse_summarize_output(json.dumps(bad), current_event_keys=["event-a", "event-b"])


if __name__ == "__main__":
    unittest.main()
