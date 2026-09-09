"""Gemini generateContent adapter."""

from __future__ import annotations

import urllib.parse
from typing import Any, Callable, Mapping, Optional

from .base import DEFAULT_REQUEST_TIMEOUT, HTTPModelBackend, ModelError
from .thinking import gemini_generate_controls, requested_thinking_mode


class GeminiBackend(HTTPModelBackend):
    provider = "gemini"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        opener: Optional[Callable[..., Any]] = None,
        thinking: Mapping[str, Any] | None = None,
    ):
        super().__init__(base_url=base_url, api_key=api_key, model=model, timeout=timeout, opener=opener)
        self.thinking = dict(thinking) if isinstance(thinking, Mapping) else {}

    @staticmethod
    def _usage_metrics(value: Mapping[str, Any], thinking_metrics: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = dict(thinking_metrics)
        usage = value.get("usageMetadata")
        if not isinstance(usage, Mapping):
            return result
        fields = {
            "promptTokenCount": "prompt_tokens",
            "candidatesTokenCount": "completion_tokens",
            "totalTokenCount": "total_tokens",
            "cachedContentTokenCount": "prompt_cache_hit_tokens",
            "thoughtsTokenCount": "reasoning_tokens",
        }
        for source, target in fields.items():
            item = usage.get(source)
            if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 10_000_000:
                result[target] = item
        return result

    def complete(self, prompt: str, *, system: str = "", purpose: str = "", temperature: float = 0.0) -> str:
        self._set_call_metrics({})
        text = f"{system}\n\n{prompt}" if system else prompt
        requested = requested_thinking_mode(self.thinking, purpose)
        thinking_config, thinking_metrics, omit_temperature = gemini_generate_controls(self.model, requested)
        generation_config: dict[str, Any] = {}
        if not omit_temperature:
            generation_config["temperature"] = temperature
        generation_config.update(thinking_config)
        payload = {
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": generation_config,
        }
        endpoint = "/v1beta/models/" + urllib.parse.quote(self.model, safe="") + ":generateContent"
        url = self.base_url + endpoint + "?key=" + urllib.parse.quote(self.api_key, safe="")
        value = self._post_json(url, payload, {}, stage=purpose)
        self._set_call_metrics(self._usage_metrics(value, thinking_metrics))
        candidates = value.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], Mapping):
            raise ModelError("model response has no candidates", code="model_invalid_response", stage=purpose)
        content = candidates[0].get("content")
        if not isinstance(content, Mapping):
            raise ModelError("model response has no content", code="model_invalid_response", stage=purpose)
        parts = content.get("parts")
        if not isinstance(parts, list):
            raise ModelError("model response has no parts", code="model_invalid_response", stage=purpose)
        return self._text(parts, stage=purpose)
