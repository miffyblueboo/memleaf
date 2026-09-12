from __future__ import annotations

import json
import unittest

from memleaf.config import default_config
from memleaf.extraction_budget import SinglePassBudgetBackend
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

    def test_processing_reports_compaction_outside_critical_path(self):
        self.assertEqual(
            Processor._critical_path_compaction_status(),
            {"status": "not_run", "reason": "outside_extraction_critical_path"},
        )


if __name__ == "__main__":
    unittest.main()
