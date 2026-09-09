"""OpenAI-compatible chat-completions adapter."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from .base import DEFAULT_REQUEST_TIMEOUT, HTTPModelBackend, ModelError


class OpenAICompatibleBackend(HTTPModelBackend):
    provider = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        opener: Optional[Callable[..., Any]] = None,
        json_mode: bool = False,
        provider_name: str = "openai",
        thinking: Mapping[str, Any] | None = None,
    ):
        super().__init__(base_url=base_url, api_key=api_key, model=model, timeout=timeout, opener=opener)
        self.json_mode = bool(json_mode)
        self.provider_name = provider_name.casefold() if isinstance(provider_name, str) else "openai"
        self.thinking = dict(thinking) if isinstance(thinking, Mapping) else {}

    @staticmethod
    def _response_text_chars(value: Any) -> int:
        if isinstance(value, str):
            return min(len(value), 1_000_000)
        if isinstance(value, list):
            total = 0
            for item in value:
                if isinstance(item, str):
                    total += len(item)
                elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                    total += len(item["text"])
            return min(total, 1_000_000)
        return 0

    @classmethod
    def _response_diagnostics(
        cls,
        value: Mapping[str, Any],
        choice: Mapping[str, Any],
        message: Mapping[str, Any],
    ) -> dict[str, Any]:
        finish_reason = choice.get("finish_reason")
        if not isinstance(finish_reason, str):
            finish_reason = "unknown"
        else:
            finish_reason = finish_reason.casefold()
            if finish_reason not in {
                "stop",
                "length",
                "tool_calls",
                "function_call",
                "content_filter",
                "insufficient_system_resource",
            }:
                finish_reason = "unknown"
        usage = value.get("usage")
        completion_tokens = usage.get("completion_tokens") if isinstance(usage, Mapping) else None
        if not (
            isinstance(completion_tokens, int)
            and not isinstance(completion_tokens, bool)
            and 0 <= completion_tokens <= 1_000_000
        ):
            completion_tokens = None
        content = message.get("content")
        reasoning = message.get("reasoning_content")
        if not isinstance(reasoning, str):
            reasoning = message.get("reasoning")
        content_chars = cls._response_text_chars(content)
        reasoning_chars = cls._response_text_chars(reasoning)
        return {
            "finish_reason": finish_reason,
            "completion_tokens": completion_tokens,
            "content_present": content_chars > 0,
            "content_chars": content_chars,
            "reasoning_present": reasoning_chars > 0,
            "reasoning_chars": reasoning_chars,
        }

    def _thinking_mode(self, purpose: str) -> str:
        value = self.thinking.get(purpose, "low")
        return value if value in {"default", "disabled", "low", "high", "max"} else "low"

    @staticmethod
    def _usage_metrics(value: Mapping[str, Any], *, thinking_mode: str) -> dict[str, Any]:
        usage = value.get("usage")
        result: dict[str, Any] = {"thinking_mode": thinking_mode}
        if not isinstance(usage, Mapping):
            return result
        for key in (
            "prompt_tokens", "completion_tokens", "total_tokens",
            "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
        ):
            item = usage.get(key)
            if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 10_000_000:
                result[key] = item
        details = usage.get("completion_tokens_details")
        reasoning = details.get("reasoning_tokens") if isinstance(details, Mapping) else None
        if isinstance(reasoning, int) and not isinstance(reasoning, bool) and 0 <= reasoning <= 10_000_000:
            result["reasoning_tokens"] = reasoning
        return result

    def complete(self, prompt: str, *, system: str = "", purpose: str = "", temperature: float = 0.0) -> str:
        self._set_call_metrics({})
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        thinking_mode = self._thinking_mode(purpose)
        if self.provider_name == "deepseek" and thinking_mode != "default":
            if thinking_mode == "disabled":
                payload["thinking"] = {"type": "disabled"}
            else:
                payload["thinking"] = {"type": "enabled"}
                payload["reasoning_effort"] = thinking_mode
        if self.json_mode and purpose in {"gate", "summarize", "compact"}:
            payload["response_format"] = {"type": "json_object"}
        value = self._post_json(
            self.base_url + "/chat/completions",
            payload,
            {"Authorization": f"Bearer {self.api_key}"},
            stage=purpose,
        )
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise ModelError(
                "model response has no choices",
                code="model_invalid_response",
                stage=purpose,
                validation_reason="response_shape",
            )
        message = choices[0].get("message")
        if not isinstance(message, Mapping):
            raise ModelError(
                "model response has no message",
                code="model_invalid_response",
                stage=purpose,
                validation_reason="response_shape",
            )
        self._set_call_metrics(self._usage_metrics(value, thinking_mode=thinking_mode))
        try:
            return self._text(message.get("content"), stage=purpose)
        except ModelError as error:
            error.with_response_diagnostics(self._response_diagnostics(value, choices[0], message))
            raise
