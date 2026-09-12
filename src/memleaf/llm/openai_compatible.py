"""OpenAI-compatible chat-completions adapter."""

from __future__ import annotations

import json
import math
from typing import Any, Callable, Mapping, Optional

from .base import DEFAULT_REQUEST_TIMEOUT, HTTPModelBackend, ModelError
from .thinking import openai_chat_controls, requested_thinking_mode
from ..model_capabilities import ProviderCapabilities, resolve_provider_capabilities


DEFAULT_SINGLE_PASS_PRIMARY_MAX_TOKENS = 8192
DEFAULT_SINGLE_PASS_REPAIR_MAX_TOKENS = 8192
MIN_SINGLE_PASS_MAX_TOKENS = 2048
MAX_SINGLE_PASS_MAX_TOKENS = 32768
_REPAIR_TOKEN_MARGIN = 768


def normalize_single_pass_token_limit(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        raise ValueError("single-pass token limit must be an integer")
    if value is None or value == "":
        return default
    if not isinstance(value, int) or not MIN_SINGLE_PASS_MAX_TOKENS <= value <= MAX_SINGLE_PASS_MAX_TOKENS:
        raise ValueError(
            f"single-pass token limit must be between {MIN_SINGLE_PASS_MAX_TOKENS} and {MAX_SINGLE_PASS_MAX_TOKENS}"
        )
    return value


def _repair_output_budget(prompt: str, ceiling: int) -> int:
    """Budget a complete repaired object, never a truncated subset.

    The project already uses UTF-8 bytes / 4 as a stable local token estimate
    for maintenance.  It is not presented as an exact provider tokenizer.
    Here we estimate only the visible previous object (the required output),
    not the repair instructions.  If that complete object cannot fit under the
    configured ceiling, the caller fails locally instead of dropping data.
    """

    prefix = "B3_REPAIR_INPUT\n"
    if not prompt.startswith(prefix):
        return ceiling
    try:
        payload_line = prompt[len(prefix):].split("\n", 1)[0]
        payload = json.loads(payload_line)
        previous = payload.get("previous_object") if isinstance(payload, Mapping) else None
        if not isinstance(previous, Mapping):
            return ceiling
        visible = json.dumps(previous, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return ceiling
    estimated = max(1, math.ceil(len(visible) / 4)) + _REPAIR_TOKEN_MARGIN
    if estimated > ceiling:
        raise ModelError(
            "single-pass repair output exceeds configured token budget",
            code="model_failed",
            stage="single_pass",
        )
    return max(MIN_SINGLE_PASS_MAX_TOKENS, estimated)


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
        json_mode: bool | None = None,
        provider_name: str = "",
        capabilities: ProviderCapabilities | None = None,
        thinking: Mapping[str, Any] | None = None,
        single_pass_primary_max_tokens: int = DEFAULT_SINGLE_PASS_PRIMARY_MAX_TOKENS,
        single_pass_repair_max_tokens: int = DEFAULT_SINGLE_PASS_REPAIR_MAX_TOKENS,
    ):
        super().__init__(base_url=base_url, api_key=api_key, model=model, timeout=timeout, opener=opener)
        # ``provider_name``/``json_mode`` remain accepted for direct legacy
        # construction.  Product routing supplies the already-resolved profile
        # so credential aliases never become a capability signal.
        if capabilities is None:
            capabilities = resolve_provider_capabilities(
                provider=provider_name,
                base_url=base_url,
            )
        self.capabilities = capabilities
        self.provider_family = capabilities.provider_family
        self.provider_family_source = capabilities.source
        self.provider_name = capabilities.provider_family  # v0.2.x compatibility attribute
        self.json_mode = capabilities.json_object if json_mode is None else bool(json_mode)
        self.single_pass_protocol = bool(capabilities.b3_protocol and capabilities.json_object)
        # This adapter has one fixed POST per complete() and no hidden retry.
        self.single_pass_safe = self.single_pass_protocol
        self.thinking = dict(thinking) if isinstance(thinking, Mapping) else {}
        self.single_pass_primary_max_tokens = normalize_single_pass_token_limit(
            single_pass_primary_max_tokens,
            default=DEFAULT_SINGLE_PASS_PRIMARY_MAX_TOKENS,
        )
        self.single_pass_repair_max_tokens = normalize_single_pass_token_limit(
            single_pass_repair_max_tokens,
            default=DEFAULT_SINGLE_PASS_REPAIR_MAX_TOKENS,
        )

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
                "stop", "length", "tool_calls", "function_call",
                "content_filter", "insufficient_system_resource",
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
        if reasoning is None:
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

    @classmethod
    def _usage_metrics(
        cls,
        value: Mapping[str, Any],
        *,
        thinking_metrics: Mapping[str, Any],
        message: Mapping[str, Any] | None,
        max_output_tokens: int | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = dict(thinking_metrics)
        if isinstance(max_output_tokens, int):
            result["max_output_tokens"] = max_output_tokens
        usage = value.get("usage")
        reasoning_observed = False
        reasoning_zero_observed = False
        if isinstance(usage, Mapping):
            for key in (
                "prompt_tokens", "completion_tokens", "total_tokens",
                "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
            ):
                item = usage.get(key)
                if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 10_000_000:
                    result[key] = item
            prompt_details = usage.get("prompt_tokens_details")
            cached = prompt_details.get("cached_tokens") if isinstance(prompt_details, Mapping) else None
            if "prompt_cache_hit_tokens" not in result and isinstance(cached, int) and not isinstance(cached, bool) and 0 <= cached <= 10_000_000:
                result["prompt_cache_hit_tokens"] = cached
            details = usage.get("completion_tokens_details")
            reasoning = details.get("reasoning_tokens") if isinstance(details, Mapping) else None
            if reasoning is None:
                reasoning = usage.get("reasoning_tokens")
            if isinstance(reasoning, int) and not isinstance(reasoning, bool) and 0 <= reasoning <= 10_000_000:
                result["reasoning_tokens"] = reasoning
                reasoning_observed = reasoning > 0
                reasoning_zero_observed = reasoning == 0

        reasoning_content = None
        if isinstance(message, Mapping):
            reasoning_content = message.get("reasoning_content")
            if reasoning_content is None:
                reasoning_content = message.get("reasoning")
        reasoning_chars = cls._response_text_chars(reasoning_content)
        if reasoning_chars > 0:
            reasoning_observed = True
            result["thinking_observation_source"] = "reasoning_content"
        elif reasoning_observed:
            result["thinking_observation_source"] = "reasoning_tokens"
        elif reasoning_zero_observed:
            result["thinking_observation_source"] = "reasoning_tokens"
        else:
            result["thinking_observation_source"] = "unavailable"

        # Request application and observed effect are independent.  In
        # particular, sending thinking=disabled is never reported as an
        # effective disable when the response still contains reasoning.
        if reasoning_observed:
            result["thinking_effective"] = "reasoning_observed"
        elif reasoning_zero_observed:
            result["thinking_effective"] = "no_reasoning_observed"
        elif result.get("thinking_applied") is True:
            result["thinking_effective"] = "unknown"
        return result

    def _single_pass_budget(self, prompt: str) -> int | None:
        if prompt.startswith("B3_REPAIR_INPUT\n"):
            return _repair_output_budget(prompt, self.single_pass_repair_max_tokens)
        return self.single_pass_primary_max_tokens

    def complete(self, prompt: str, *, system: str = "", purpose: str = "", temperature: float = 0.0) -> str:
        self._set_call_metrics({})
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        requested = requested_thinking_mode(self.thinking, purpose)
        controls, thinking_metrics, omit_temperature = openai_chat_controls(
            self.provider_family, self.model, requested
        )
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if not omit_temperature:
            payload["temperature"] = temperature
        payload.update(controls)
        if self.json_mode and purpose in {"gate", "summarize", "compact", "single_pass"}:
            payload["response_format"] = {"type": "json_object"}

        max_output_tokens: int | None = None
        if purpose == "single_pass" and self.single_pass_protocol:
            max_output_tokens = self._single_pass_budget(prompt)
            field = self.capabilities.output_token_field
            if isinstance(field, str) and field and isinstance(max_output_tokens, int):
                payload[field] = max_output_tokens

        value = self._post_json(
            self.base_url + "/chat/completions",
            payload,
            {"Authorization": f"Bearer {self.api_key}"},
            stage=purpose,
        )
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            self._set_call_metrics(self._usage_metrics(
                value, thinking_metrics=thinking_metrics, message=None,
                max_output_tokens=max_output_tokens,
            ))
            raise ModelError(
                "model response has no choices",
                code="model_invalid_response",
                stage=purpose,
                validation_reason="response_shape",
            )
        message = choices[0].get("message")
        if not isinstance(message, Mapping):
            self._set_call_metrics(self._usage_metrics(
                value, thinking_metrics=thinking_metrics, message=None,
                max_output_tokens=max_output_tokens,
            ))
            raise ModelError(
                "model response has no message",
                code="model_invalid_response",
                stage=purpose,
                validation_reason="response_shape",
            )
        diagnostics = self._response_diagnostics(value, choices[0], message)
        self._set_call_metrics(self._usage_metrics(
            value, thinking_metrics=thinking_metrics, message=message,
            max_output_tokens=max_output_tokens,
        ))
        if diagnostics.get("finish_reason") == "length":
            raise ModelError(
                "model response was truncated",
                code="model_invalid_response",
                stage=purpose,
                validation_reason="response_shape",
            ).with_response_diagnostics(diagnostics)
        try:
            return self._text(message.get("content"), stage=purpose)
        except ModelError as error:
            error.with_response_diagnostics(diagnostics)
            raise


__all__ = [
    "DEFAULT_SINGLE_PASS_PRIMARY_MAX_TOKENS",
    "DEFAULT_SINGLE_PASS_REPAIR_MAX_TOKENS",
    "MIN_SINGLE_PASS_MAX_TOKENS",
    "MAX_SINGLE_PASS_MAX_TOKENS",
    "OpenAICompatibleBackend",
    "normalize_single_pass_token_limit",
]
