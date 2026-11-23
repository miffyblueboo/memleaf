"""Claude-compatible messages adapter."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from .base import DEFAULT_REQUEST_TIMEOUT, HTTPModelBackend
from .thinking import claude_messages_controls, requested_thinking_mode


class ClaudeCompatibleBackend(HTTPModelBackend):
    provider = "claude"

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

    def _explicit_prompt_cache(self) -> bool:
        from urllib.parse import urlsplit
        host = (urlsplit(self.base_url).hostname or "").casefold()
        return host == "anthropic.com" or host.endswith(".anthropic.com")

    @staticmethod
    def _usage_metrics(value: Mapping[str, Any], thinking_metrics: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = dict(thinking_metrics)
        usage = value.get("usage")
        if not isinstance(usage, Mapping):
            return result
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if isinstance(input_tokens, int) and not isinstance(input_tokens, bool) and 0 <= input_tokens <= 10_000_000:
            # Anthropic input_tokens excludes cache reads/writes; report the full
            # prompt like OpenAI-compatible usage, with cache hits separately.
            cached = [usage.get(key) for key in ("cache_read_input_tokens", "cache_creation_input_tokens")]
            total = input_tokens + sum(v for v in cached if isinstance(v, int) and not isinstance(v, bool) and v >= 0)
            if total <= 10_000_000:
                result["prompt_tokens"] = total
        if isinstance(output_tokens, int) and not isinstance(output_tokens, bool) and 0 <= output_tokens <= 10_000_000:
            result["completion_tokens"] = output_tokens
        if "prompt_tokens" in result and "completion_tokens" in result:
            result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
        cache_read = usage.get("cache_read_input_tokens")
        if isinstance(cache_read, int) and not isinstance(cache_read, bool) and 0 <= cache_read <= 10_000_000:
            result["prompt_cache_hit_tokens"] = cache_read
        return result

    def complete(self, prompt: str, *, system: str = "", purpose: str = "", temperature: float = 0.0) -> str:
        self._set_call_metrics({})
        requested = requested_thinking_mode(self.thinking, purpose)
        controls, thinking_metrics, omit_temperature = claude_messages_controls(self.model, requested)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        if not omit_temperature:
            payload["temperature"] = temperature
        if system:
            # The long extraction/review contracts are a stable prefix. Anthropic
            # caches only explicitly marked blocks; compatible gateways keep the
            # plain string form they already accept.
            payload["system"] = ([{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                                 if self._explicit_prompt_cache() else system)
        payload.update(controls)
        value = self._post_json(
            self.base_url + "/v1/messages",
            payload,
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            stage=purpose,
        )
        self._set_call_metrics(self._usage_metrics(value, thinking_metrics))
        return self._text(value.get("content"), stage=purpose)
