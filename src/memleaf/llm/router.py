"""Explicit host/API model routing with safe diagnostics."""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Mapping, Optional

from .base import (
    DEFAULT_REQUEST_TIMEOUT,
    CallableBackend,
    HostBackend,
    ModelBackend,
    ModelError,
    ModelUnavailable,
    normalize_request_timeout,
)
from .claude_compatible import ClaudeCompatibleBackend
from .gemini import GeminiBackend
from .openai_compatible import OpenAICompatibleBackend
from ..credentials import credential_text


_JSON_MODE_PROVIDERS = frozenset({"openai", "deepseek"})


class ModelRouter:
    """Select only an explicitly injected host or a fully configured API."""

    def __init__(
        self,
        mode: str = "auto",
        *,
        host: Any = None,
        api: Any = None,
        config: Optional[Mapping[str, Any]] = None,
        logger: Optional[Any] = None,
    ):
        if mode not in ("auto", "host", "api"):
            raise ValueError("invalid model mode")
        self.mode = mode
        self.config = dict(config or {})
        self.logger = logger or logging.getLogger("memleaf.model")
        self.host = self._coerce_host(host)
        self.api = self._coerce_api(api) if api is not None else self._build_api()
        self.diagnostics: list[dict[str, str]] = []

    @classmethod
    def from_config(cls, config: Mapping[str, Any], **kwargs: Any) -> "ModelRouter":
        llm = config.get("llm", config) if isinstance(config, Mapping) else {}
        llm_config = llm if isinstance(llm, Mapping) else {}
        mode = kwargs.pop("mode", llm_config.get("mode", "auto"))
        return cls(mode=mode, config=llm_config, **kwargs)

    @staticmethod
    def _coerce_host(value: Any) -> Optional[ModelBackend]:
        if value is None:
            return None
        if callable(value) and not hasattr(value, "complete"):
            return HostBackend(value)
        if not hasattr(value, "complete"):
            raise TypeError("host backend must expose complete()")
        return value

    @staticmethod
    def _coerce_api(value: Any) -> Optional[ModelBackend]:
        if callable(value) and not hasattr(value, "complete"):
            return CallableBackend(value, model="injected-api")
        if not hasattr(value, "complete"):
            raise TypeError("api backend must expose complete()")
        return value

    def _build_api(self) -> Optional[ModelBackend]:
        config = self.config
        provider = str(config.get("provider", "")).casefold()
        protocol = str(config.get("protocol", "openai")).casefold()
        base_url = config.get("base_url")
        model = config.get("model")
        api_key = credential_text(config.get("api_key"))
        api_key_env = config.get("api_key_env")
        if not all(isinstance(item, str) and item.strip() for item in (base_url, model)):
            return None
        # New installer-created routes store the key directly in the local
        # 0600 memleaf config.  Keep the old environment-name form as a
        # compatibility fallback for existing users, but never let an empty
        # direct value shadow a valid legacy environment configuration.
        if api_key is None:
            if not isinstance(api_key_env, str) or not api_key_env.strip():
                return None
            api_key = credential_text(os.environ.get(api_key_env))
        if api_key is None:
            return None
        try:
            request_timeout = normalize_request_timeout(
                config.get("request_timeout", DEFAULT_REQUEST_TIMEOUT)
            )
        except ValueError:
            return None
        kwargs = {
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "timeout": request_timeout,
        }
        try:
            if protocol in ("claude", "anthropic") or "claude" in provider or "anthropic" in provider:
                return ClaudeCompatibleBackend(**kwargs)
            if protocol == "gemini" or "gemini" in provider:
                return GeminiBackend(**kwargs)
            if protocol in ("openai", "openai-compatible", "openai_compatible") or provider in ("openai", "") or "openai" in provider:
                return OpenAICompatibleBackend(
                    **kwargs,
                    json_mode=provider in _JSON_MODE_PROVIDERS,
                    provider_name=provider or "openai",
                )
        except (ModelError, ValueError, TypeError):
            return None
        return None

    def _diagnose(self, provider: str, model: str, reason: str) -> None:
        item = {"provider": provider or "unknown", "model": model or "unknown", "reason": reason}
        self.diagnostics.append(item)
        try:
            self.logger.info("memleaf model route provider=%s model=%s reason=%s", item["provider"], item["model"], reason)
        except Exception:
            pass

    @staticmethod
    def _identity(backend: Optional[ModelBackend]) -> tuple[str, str]:
        if backend is None:
            return "unknown", "unknown"
        return str(getattr(backend, "provider", "unknown")), str(getattr(backend, "model", "unknown"))

    def _call(self, backend: ModelBackend, prompt: str, *, system: str, purpose: str, temperature: float) -> str:
        try:
            value = backend.complete(prompt, system=system, purpose=purpose, temperature=temperature)
        except ModelError as error:
            error.with_stage(purpose)
            raise
        except Exception as error:
            raise ModelError("model backend failed", stage=purpose) from error
        if not isinstance(value, str):
            raise ModelError("model backend returned non-text output", code="model_invalid_response", stage=purpose)
        return value

    def complete(self, prompt: str, *, system: str = "", purpose: str = "", temperature: float = 0.0) -> str:
        if not isinstance(prompt, str):
            raise TypeError("model prompt must be text")
        if self.mode == "host":
            if self.host is None:
                self._diagnose("host", "unknown", "host_unavailable")
                raise ModelUnavailable("host model is unavailable")
            return self._call(self.host, prompt, system=system, purpose=purpose, temperature=temperature)
        if self.mode == "api":
            if self.api is None:
                self._diagnose("api", "unknown", "api_unavailable")
                raise ModelUnavailable("api model is unavailable")
            return self._call(self.api, prompt, system=system, purpose=purpose, temperature=temperature)

        if self.host is not None:
            try:
                return self._call(self.host, prompt, system=system, purpose=purpose, temperature=temperature)
            except ModelError:
                provider, model = self._identity(self.host)
                self._diagnose(provider, model, "host_failed")
                if self.api is None:
                    raise ModelUnavailable("no configured model backend")
        if self.api is None:
            self._diagnose("unknown", "unknown", "no_configured_backend")
            raise ModelUnavailable("no configured model backend")
        return self._call(self.api, prompt, system=system, purpose=purpose, temperature=temperature)

    __call__ = complete
