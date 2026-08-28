"""Gemini generateContent adapter."""

from __future__ import annotations

import urllib.parse
from typing import Any, Callable, Mapping, Optional

from .base import DEFAULT_REQUEST_TIMEOUT, HTTPModelBackend, ModelError


class GeminiBackend(HTTPModelBackend):
    provider = "gemini"

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float = DEFAULT_REQUEST_TIMEOUT, opener: Optional[Callable[..., Any]] = None):
        super().__init__(base_url=base_url, api_key=api_key, model=model, timeout=timeout, opener=opener)

    def complete(self, prompt: str, *, system: str = "", purpose: str = "", temperature: float = 0.0) -> str:
        text = f"{system}\n\n{prompt}" if system else prompt
        payload = {
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {"temperature": temperature},
        }
        endpoint = "/v1beta/models/" + urllib.parse.quote(self.model, safe="") + ":generateContent"
        url = self.base_url + endpoint + "?key=" + urllib.parse.quote(self.api_key, safe="")
        value = self._post_json(url, payload, {}, stage=purpose)
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
