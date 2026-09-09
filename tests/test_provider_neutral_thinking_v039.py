from __future__ import annotations

import json
import unittest

from memleaf.llm.claude_compatible import ClaudeCompatibleBackend
from memleaf.llm.gemini import GeminiBackend
from memleaf.llm.openai_compatible import OpenAICompatibleBackend
from memleaf.llm.router import ModelRouter
from memleaf.llm.thinking import requested_thinking_mode
from memleaf.model_execution import ModelExecutor
from memleaf.process_jobs import _safe_model_metrics


class _Response:
    def __init__(self, value):
        self.value = value
    def read(self):
        return json.dumps(self.value).encode("utf-8")
    def close(self):
        pass


class _Opener:
    def __init__(self, value):
        self.value = value
        self.requests = []
    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        return _Response(self.value)
    def payload(self):
        return json.loads(self.requests[-1][0].data.decode("utf-8"))


def _openai_backend(model, *, provider="openai", thinking=None):
    opener = _Opener({
        "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 60},
            "completion_tokens_details": {"reasoning_tokens": 7},
        },
    })
    backend = OpenAICompatibleBackend(
        base_url="https://example.invalid/v1",
        api_key="secret",
        model=model,
        opener=opener,
        json_mode=True,
        provider_name=provider,
        thinking=thinking or {"gate": "low", "summarize": "low", "compact": "low"},
    )
    return backend, opener


def _claude_backend(model, *, thinking=None):
    opener = _Opener({
        "content": [{"type": "text", "text": "{}"}],
        "usage": {"input_tokens": 90, "output_tokens": 10, "cache_read_input_tokens": 50},
    })
    backend = ClaudeCompatibleBackend(
        base_url="https://example.invalid",
        api_key="secret",
        model=model,
        opener=opener,
        thinking=thinking or {"gate": "low", "summarize": "low", "compact": "low"},
    )
    return backend, opener


def _gemini_backend(model, *, thinking=None):
    opener = _Opener({
        "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
        "usageMetadata": {
            "promptTokenCount": 80,
            "candidatesTokenCount": 12,
            "totalTokenCount": 97,
            "cachedContentTokenCount": 40,
            "thoughtsTokenCount": 5,
        },
    })
    backend = GeminiBackend(
        base_url="https://example.invalid",
        api_key="secret",
        model=model,
        opener=opener,
        thinking=thinking or {"gate": "low", "summarize": "low", "compact": "low"},
    )
    return backend, opener


class ProviderNeutralThinkingV039Tests(unittest.TestCase):
    def test_policy_default_is_low_for_every_memleaf_model_stage(self):
        for purpose in ("gate", "summarize", "compact"):
            self.assertEqual(requested_thinking_mode({}, purpose), "low")
        self.assertEqual(requested_thinking_mode({}, "chat"), "default")

    def test_openai_reasoning_model_receives_low_effort_without_temperature(self):
        backend, opener = _openai_backend("gpt-5.6-sol")
        backend.complete("prompt", purpose="gate")
        payload = opener.payload()
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertNotIn("temperature", payload)
        metrics = backend.consume_call_metrics()
        self.assertEqual(metrics["thinking_mode"], "low")
        self.assertEqual(metrics["thinking_effective"], "low")
        self.assertEqual(metrics["thinking_control"], "openai_reasoning_effort")
        self.assertEqual(metrics["prompt_cache_hit_tokens"], 60)
        self.assertEqual(metrics["reasoning_tokens"], 7)

    def test_openai_nonreasoning_model_avoids_unknown_effort_field(self):
        backend, opener = _openai_backend("gpt-4.1")
        backend.complete("prompt", purpose="gate")
        payload = opener.payload()
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["temperature"], 0.0)
        metrics = backend.consume_call_metrics()
        self.assertEqual(metrics["thinking_mode"], "low")
        self.assertEqual(metrics["thinking_effective"], "unsupported")

    def test_deepseek_keeps_explicit_low_thinking(self):
        backend, opener = _openai_backend("deepseek-v4-flash", provider="deepseek")
        backend.complete("prompt", purpose="gate")
        payload = opener.payload()
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertNotIn("temperature", payload)
        self.assertEqual(backend.consume_call_metrics()["thinking_control"], "deepseek_thinking_effort")

    def test_current_claude_models_receive_low_effort_safely(self):
        backend, opener = _claude_backend("claude-opus-4-8")
        backend.complete("prompt", purpose="gate")
        payload = opener.payload()
        self.assertEqual(payload["thinking"], {"type": "adaptive"})
        self.assertEqual(payload["output_config"], {"effort": "low"})
        self.assertNotIn("temperature", payload)
        metrics = backend.consume_call_metrics()
        self.assertEqual(metrics["thinking_effective"], "low")
        self.assertEqual(metrics["thinking_control"], "anthropic_adaptive_effort")

        for model in ("claude-opus-5", "claude-fable-5"):
            backend, opener = _claude_backend(model)
            backend.complete("prompt", purpose="gate")
            payload = opener.payload()
            self.assertEqual(payload["output_config"], {"effort": "low"})
            self.assertNotIn("thinking", payload)
            self.assertNotIn("temperature", payload)

    def test_claude_opus_45_receives_low_effort_without_forcing_adaptive_thinking(self):
        backend, opener = _claude_backend("claude-opus-4-5-20251101")
        backend.complete("prompt", purpose="gate")
        payload = opener.payload()
        self.assertEqual(payload["output_config"], {"effort": "low"})
        self.assertNotIn("thinking", payload)
        self.assertEqual(payload["temperature"], 0.0)
        metrics = backend.consume_call_metrics()
        self.assertEqual(metrics["thinking_mode"], "low")
        self.assertEqual(metrics["thinking_effective"], "low")
        self.assertEqual(metrics["thinking_control"], "anthropic_effort")

    def test_older_claude_model_fails_safe_without_invalid_effort(self):
        backend, opener = _claude_backend("claude-haiku-4-5-20251001")
        backend.complete("prompt", purpose="gate")
        payload = opener.payload()
        self.assertNotIn("output_config", payload)
        self.assertNotIn("thinking", payload)
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(backend.consume_call_metrics()["thinking_effective"], "unsupported")

    def test_gemini_3_uses_low_level_and_25_uses_low_budget(self):
        backend, opener = _gemini_backend("gemini-3.1-pro-preview")
        backend.complete("prompt", purpose="gate")
        config = opener.payload()["generationConfig"]
        self.assertEqual(config["thinkingConfig"], {"thinkingLevel": "low"})
        self.assertNotIn("temperature", config)
        metrics = backend.consume_call_metrics()
        self.assertEqual(metrics["thinking_effective"], "low")
        self.assertEqual(metrics["thinking_control"], "gemini_thinking_level")
        self.assertEqual(metrics["reasoning_tokens"], 5)

        backend, opener = _gemini_backend("gemini-3.1-flash-lite-image-preview")
        backend.complete("prompt", purpose="gate")
        config = opener.payload()["generationConfig"]
        self.assertEqual(config["thinkingConfig"], {"thinkingLevel": "minimal"})
        self.assertNotIn("temperature", config)
        metrics = backend.consume_call_metrics()
        self.assertEqual(metrics["thinking_mode"], "low")
        self.assertEqual(metrics["thinking_effective"], "minimal")

        backend, opener = _gemini_backend("gemini-2.5-flash")
        backend.complete("prompt", purpose="gate")
        config = opener.payload()["generationConfig"]
        self.assertEqual(config["thinkingConfig"], {"thinkingBudget": 1024})
        self.assertEqual(backend.consume_call_metrics()["thinking_control"], "gemini_thinking_budget")

    def test_old_gemini_model_omits_unsupported_thinking_config(self):
        backend, opener = _gemini_backend("gemini-2.0-flash")
        backend.complete("prompt", purpose="gate")
        self.assertNotIn("thinkingConfig", opener.payload()["generationConfig"])
        self.assertEqual(backend.consume_call_metrics()["thinking_effective"], "unsupported")

    def test_router_passes_one_policy_to_all_api_adapters(self):
        cases = (
            ("openai", "openai", "gpt-5.6-sol"),
            ("anthropic", "claude", "claude-opus-5"),
            ("gemini", "gemini", "gemini-3.1-pro-preview"),
        )
        for provider, protocol, model in cases:
            router = ModelRouter.from_config({"llm": {
                "mode": "api",
                "provider": provider,
                "protocol": protocol,
                "base_url": "https://example.invalid",
                "api_key": "secret",
                "model": model,
                "thinking": {"gate": "low", "summarize": "low", "compact": "low"},
            }})
            self.assertIsNotNone(router.api)
            self.assertEqual(router.api.thinking["gate"], "low")

    def test_structural_metric_projection_preserves_only_fixed_thinking_fields(self):
        raw = {"calls": [{
            "stage": "gate",
            "operation": "gate_primary",
            "call_index": 1,
            "request_duration_ms": 3,
            "retry": False,
            "failed": False,
            "thinking_mode": "low",
            "thinking_effective": "minimal",
            "thinking_control": "gemini_thinking_level",
            "prompt": "SECRET",
            "evil": "SECRET",
        }]}
        safe = _safe_model_metrics(raw)
        row = safe["calls"][0]
        self.assertEqual(row["thinking_effective"], "minimal")
        self.assertEqual(row["thinking_control"], "gemini_thinking_level")
        self.assertNotIn("prompt", row)
        self.assertNotIn("evil", row)


if __name__ == "__main__":
    unittest.main()
