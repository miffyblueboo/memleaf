from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf
from memleaf.config import default_config
from memleaf.extraction_budget import ExtractionWorkBudget, SinglePassBudgetBackend
from memleaf.llm import ModelError
from memleaf.llm.openai_compatible import OpenAICompatibleBackend
from memleaf.processing import Processor


class _Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class _BudgetBackend:
    provider = "test"
    model = "test"
    parallel_safe = True
    structured_batch_safe = True
    single_pass_safe = True

    def __init__(self, clock, advances=()):
        self.clock = clock
        self.advances = list(advances)
        self.timeout_caps = []
        self.calls = 0

    def set_call_timeout(self, seconds):
        self.timeout_caps.append(seconds)

    def clear_call_timeout(self):
        pass

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        del prompt, system, purpose, temperature
        self.calls += 1
        if self.advances:
            self.clock.advance(self.advances.pop(0))
        return "{}"


class _SequentialSinglePassBackend:
    provider = "test"
    model = "single-pass-sequential"
    parallel_safe = False
    structured_batch_safe = False
    single_pass_safe = True

    def __init__(self):
        self.calls = []

    @staticmethod
    def _payload(prompt):
        return json.loads(prompt.split("B3_INPUT\n", 1)[1].split("\nReturn", 1)[0])

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        del system, temperature
        self.calls.append(purpose)
        payload = self._payload(prompt)
        user = next(row for row in payload["current_evidence"] if row["role"] == "user")
        assistant = next(row for row in payload["current_evidence"] if row["role"] == "assistant")
        evidence = [{"unit_id": user["unit_id"], "whole_unit": True, "role": "assertion"}]
        no_memory = [{"unit_id": assistant["unit_id"], "reason": "assistant_restatement"}]
        local = payload["local_memory_catalog"]
        if not local:
            item = {
                "candidate_id": "database-engine",
                "decision": "CREATE",
                "type": "fact",
                "scopes": ["global"],
                "evidence": evidence,
                "memory": {
                    "title": "Alpha database engine",
                    "body": user["content"],
                },
            }
        else:
            item = {
                "candidate_id": "database-engine-update",
                "decision": "UPDATE",
                "target_memory_id": local[0]["memory_id"],
                "evidence": evidence,
                "memory": {"body": user["content"]},
            }
        return json.dumps(
            {
                "protocol_version": "b3-single-pass-v1",
                "items": [item],
                "no_memory": no_memory,
            },
            ensure_ascii=False,
        )


class _Response:
    def read(self):
        return json.dumps({
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }).encode("utf-8")

    def close(self):
        pass


class _Opener:
    def __init__(self):
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        return _Response()

    def payload(self):
        return json.loads(self.requests[-1][0].data.decode("utf-8"))


class UnifiedExtractionV2Tests(unittest.TestCase):
    def test_default_single_pass_profile_disables_thinking(self):
        config = default_config()
        self.assertEqual(config["llm"]["thinking"]["single_pass"], "disabled")
        # Existing stages retain their old low-reasoning defaults.
        self.assertEqual(config["llm"]["thinking"]["gate"], "low")
        self.assertEqual(config["llm"]["thinking"]["summarize"], "low")
        self.assertEqual(config["llm"]["thinking"]["compact"], "low")

    def test_deepseek_single_pass_is_json_nonthinking_and_output_bounded(self):
        opener = _Opener()
        backend = OpenAICompatibleBackend(
            base_url="https://example.invalid/v1",
            api_key="secret",
            model="deepseek-v4-flash",
            opener=opener,
            json_mode=True,
            provider_name="deepseek",
            thinking={"single_pass": "disabled"},
        )
        backend.set_call_timeout(0.75)
        backend.complete("prompt", purpose="single_pass")

        payload = opener.payload()
        self.assertEqual(opener.requests[-1][1], 0.75)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_tokens"], 1800)

    def test_budget_caps_primary_then_uses_shared_remaining_time(self):
        clock = _Clock()
        backend = _BudgetBackend(clock, advances=[5.0, 0.0])
        budgeted = SinglePassBudgetBackend(backend, clock=clock)

        budgeted.complete("first", purpose="single_pass")
        budgeted.complete("repair", purpose="single_pass")

        self.assertEqual(backend.calls, 2)
        self.assertEqual(backend.timeout_caps[0], 6.0)
        self.assertEqual(backend.timeout_caps[1], 3.0)
        with self.assertRaises(ModelError) as caught:
            budgeted.complete("third", purpose="single_pass")
        self.assertEqual(caught.exception.code, "model_timeout")
        self.assertEqual(backend.calls, 2)

    def test_budget_rejects_late_result_even_when_backend_ignores_timeout(self):
        clock = _Clock()
        backend = _BudgetBackend(clock, advances=[8.1])
        budgeted = SinglePassBudgetBackend(backend, clock=clock)
        with self.assertRaises(ModelError) as caught:
            budgeted.complete("late", purpose="single_pass")
        self.assertEqual(caught.exception.code, "model_timeout")

    def test_work_budget_counts_preparation_time_and_blocks_late_commit(self):
        clock = _Clock()
        work = ExtractionWorkBudget(clock=clock)
        clock.advance(3.0)  # planning/context preparation consumes the turn budget
        backend = _BudgetBackend(clock, advances=[0.0])
        budgeted = work.wrap_backend(backend)

        budgeted.complete("primary", purpose="single_pass")

        self.assertEqual(backend.timeout_caps, [5.0])
        clock.advance(7.1)
        with self.assertRaises(ModelError) as caught:
            work.ensure_before_commit()
        self.assertEqual(caught.exception.code, "model_timeout")

    def test_process_commits_one_turn_per_session_and_next_call_sees_it(self):
        with tempfile.TemporaryDirectory(prefix="memleaf-single-turn-") as tempdir:
            service = Memleaf(Path(tempdir) / "vault")
            backend = _SequentialSinglePassBackend()
            service.capture(
                "hermes", "session", "turn-1", "user",
                "Alpha database engine is SQLite.", event_id="u1",
            )
            service.capture(
                "hermes", "session", "turn-1", "assistant",
                "Noted.", event_id="a1",
            )
            service.capture(
                "hermes", "session", "turn-2", "user",
                "Alpha database engine is now PostgreSQL.", event_id="u2",
            )
            service.capture(
                "hermes", "session", "turn-2", "assistant",
                "Noted.", event_id="a2",
            )

            first = service.process(source="hermes", session_id="session", model=backend)
            self.assertEqual(first["processed_turns"], 1)
            self.assertEqual(first["memories_written"], 1)
            self.assertEqual(backend.calls, ["single_pass"])
            active = [record.memory for record in service._read_memories_unlocked("knowledge")]
            self.assertEqual(len(active), 1)
            self.assertIn("SQLite", active[0].body)

            second = service.process(source="hermes", session_id="session", model=backend)
            self.assertEqual(second["processed_turns"], 1)
            self.assertEqual(second["memories_written"], 1)
            self.assertEqual(backend.calls, ["single_pass", "single_pass"])
            active = [record.memory for record in service._read_memories_unlocked("knowledge")]
            self.assertEqual(len(active), 1)
            self.assertIn("PostgreSQL", active[0].body)
            self.assertNotIn("SQLite", active[0].body)
            self.assertEqual(len(service._read_memories_unlocked("history")), 1)

    def test_processing_reports_compaction_outside_critical_path(self):
        self.assertEqual(
            Processor._critical_path_compaction_status(),
            {"status": "not_run", "reason": "outside_extraction_critical_path"},
        )


if __name__ == "__main__":
    unittest.main()
