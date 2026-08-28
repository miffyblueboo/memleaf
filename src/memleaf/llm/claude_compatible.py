"""Claude-compatible messages adapter."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from .base import DEFAULT_REQUEST_TIMEOUT, HTTPModelBackend, ModelError


class ClaudeCompatibleBackend(HTTPModelBackend):
    provider = "claude"

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float = DEFAULT_REQUEST_TIMEOUT, opener: Optional[Callable[..., Any]] = None):
        super().__init__(base_url=base_url, api_key=api_key, model=model, timeout=timeout, opener=opener)

    def complete(self, prompt: str, *, system: str = "", purpose: str = "", temperature: float = 0.0) -> str:
        payload = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        if system:
            payload["system"] = system
        value = self._post_json(
            self.base_url + "/v1/messages",
            payload,
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            stage=purpose,
        )
        return self._text(value.get("content"), stage=purpose)
