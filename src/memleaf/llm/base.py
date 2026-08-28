"""Small injectable model backend interfaces used by stage B."""

from __future__ import annotations

import json
import inspect
import math
import socket
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Optional, Protocol


DEFAULT_REQUEST_TIMEOUT = 120.0
MIN_REQUEST_TIMEOUT = 1.0
MAX_REQUEST_TIMEOUT = 240.0
MODEL_ERROR_CODES = frozenset(
    {
        "model_timeout",
        "model_auth_failed",
        "model_rate_limited",
        "model_http_error",
        "model_network_error",
        "model_invalid_response",
        "model_unavailable",
        "model_failed",
    }
)
MODEL_ERROR_STAGES = frozenset({"gate", "summarize"})
MODEL_VALIDATION_REASONS = frozenset(
    {"empty_content", "invalid_json", "schema_violation", "response_shape"}
)
MODEL_FINISH_REASONS = frozenset(
    {
        "stop",
        "length",
        "tool_calls",
        "function_call",
        "content_filter",
        "insufficient_system_resource",
        "unknown",
    }
)
MODEL_RESPONSE_DIAGNOSTIC_FIELDS = frozenset(
    {
        "finish_reason",
        "completion_tokens",
        "content_present",
        "content_chars",
        "reasoning_present",
        "reasoning_chars",
    }
)


def normalize_request_timeout(value: Any, *, default: float = DEFAULT_REQUEST_TIMEOUT) -> float:
    """Validate the bounded model request timeout without accepting booleans."""

    if value is None:
        value = default
    if isinstance(value, bool):
        raise ValueError("llm.request_timeout must be between 1 and 240 seconds")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError("llm.request_timeout must be between 1 and 240 seconds") from None
    if not math.isfinite(parsed) or not MIN_REQUEST_TIMEOUT <= parsed <= MAX_REQUEST_TIMEOUT:
        raise ValueError("llm.request_timeout must be between 1 and 240 seconds")
    return int(parsed) if parsed.is_integer() else parsed


def _safe_error_code(value: Any) -> str:
    return value if isinstance(value, str) and value in MODEL_ERROR_CODES else "model_failed"


def _safe_error_stage(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value in MODEL_ERROR_STAGES else None


def _safe_validation_reason(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value in MODEL_VALIDATION_REASONS else None


class ModelError(RuntimeError):
    """A model call or response could not be completed safely."""

    def __init__(
        self,
        message: str = "model failed",
        *,
        code: str = "model_failed",
        stage: str | None = None,
        validation_reason: str | None = None,
    ):
        # Existing callers may still provide a descriptive local message.  The
        # public MCP/marker paths use only ``code`` and ``stage``.
        super().__init__(message)
        self.code = _safe_error_code(code)
        self.stage = _safe_error_stage(stage)
        self.validation_reason = _safe_validation_reason(validation_reason)
        self.response_diagnostics: dict[str, Any] = {}

    def with_stage(self, stage: str | None) -> "ModelError":
        if self.stage is None:
            self.stage = _safe_error_stage(stage)
        return self

    def with_response_diagnostics(self, value: Mapping[str, Any] | None) -> "ModelError":
        """Attach only bounded response-shape statistics to this safe error."""

        if not isinstance(value, Mapping):
            return self
        safe: dict[str, Any] = {}
        finish_reason = value.get("finish_reason")
        safe["finish_reason"] = (
            finish_reason if isinstance(finish_reason, str) and finish_reason in MODEL_FINISH_REASONS else "unknown"
        )
        completion_tokens = value.get("completion_tokens")
        safe["completion_tokens"] = (
            completion_tokens
            if isinstance(completion_tokens, int)
            and not isinstance(completion_tokens, bool)
            and 0 <= completion_tokens <= 1_000_000
            else None
        )
        for key in ("content_present", "reasoning_present"):
            safe[key] = value.get(key) if isinstance(value.get(key), bool) else False
        for key in ("content_chars", "reasoning_chars"):
            item = value.get(key)
            safe[key] = item if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 1_000_000 else 0
        self.response_diagnostics = safe
        return self


class ModelUnavailable(ModelError):
    """No explicitly configured model capability is available."""

    def __init__(self, message: str = "model unavailable", *, stage: str | None = None):
        super().__init__(message, code="model_unavailable", stage=stage)


class ModelBackend(Protocol):
    provider: str
    model: str

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        purpose: str = "",
        temperature: float = 0.0,
    ) -> str:
        ...


class CallableBackend:
    """Adapt an explicitly injected host/test callback to ``ModelBackend``."""

    provider = "host"

    def __init__(self, callback: Callable[..., str], *, model: str = "host"):
        if not callable(callback):
            raise TypeError("model callback must be callable")
        self.callback = callback
        self.model = model

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        purpose: str = "",
        temperature: float = 0.0,
    ) -> str:
        full_kwargs = {
            "system": system,
            "purpose": purpose,
            "temperature": temperature,
        }
        try:
            signature = inspect.signature(self.callback)
        except (TypeError, ValueError):
            # If a callable does not expose a signature, use one explicit
            # calling convention.  Never retry after callback execution.
            value = self.callback(prompt, **full_kwargs)
        else:
            try:
                signature.bind(prompt, **full_kwargs)
            except TypeError:
                try:
                    signature.bind(prompt)
                except TypeError as error:
                    raise TypeError("model callback must accept prompt") from error
                value = self.callback(prompt)
            else:
                value = self.callback(prompt, **full_kwargs)
        if not isinstance(value, str):
            raise ModelError(
                "model callback returned non-text output",
                code="model_invalid_response",
                validation_reason="response_shape",
            )
        return value


class HostBackend(CallableBackend):
    """An explicit host callback; never discovers or invokes a host itself."""

    provider = "host"


class FakeBackend(CallableBackend):
    """Named test helper; it has no network behavior."""

    provider = "fake"


class HTTPModelBackend:
    """Common standard-library HTTP transport for compatible adapters."""

    provider = "api"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        opener: Optional[Callable[..., Any]] = None,
    ):
        if not isinstance(base_url, str) or not base_url.strip():
            raise ModelUnavailable("api base URL is not configured")
        if not isinstance(api_key, str) or not api_key:
            raise ModelUnavailable("api key is not available")
        if not isinstance(model, str) or not model.strip():
            raise ModelUnavailable("api model is not configured")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = normalize_request_timeout(timeout)
        self._opener = opener or urllib.request.urlopen

    @staticmethod
    def _is_timeout_reason(value: Any) -> bool:
        if isinstance(value, (TimeoutError, socket.timeout)):
            return True
        return type(value).__name__.casefold() in {"timeouterror", "sockettimeout"}

    def _post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        *,
        stage: str | None = None,
    ) -> Mapping[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", **dict(headers)},
            method="POST",
        )
        try:
            response = self._opener(request, timeout=self.timeout)
            try:
                raw = response.read()
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except urllib.error.HTTPError as error:
            status = getattr(error, "code", None)
            if status in (401, 403):
                code = "model_auth_failed"
                message = "model authentication failed"
            elif status == 429:
                code = "model_rate_limited"
                message = "model request was rate limited"
            else:
                code = "model_http_error"
                message = "model HTTP request failed"
            try:
                error.close()
            except Exception:
                pass
            raise ModelError(message, code=code, stage=stage) from None
        except TimeoutError:
            raise ModelError("model request timed out", code="model_timeout", stage=stage) from None
        except urllib.error.URLError as error:
            if self._is_timeout_reason(getattr(error, "reason", None)):
                raise ModelError("model request timed out", code="model_timeout", stage=stage) from None
            raise ModelError("model network request failed", code="model_network_error", stage=stage) from None
        except OSError:
            raise ModelError("model network request failed", code="model_network_error", stage=stage) from None
        except Exception:
            # An injected opener must not be able to expose arbitrary provider
            # exception text through the model boundary.
            raise ModelError("model network request failed", code="model_network_error", stage=stage) from None
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ModelError(
                    "model returned invalid response",
                    code="model_invalid_response",
                    stage=stage,
                    validation_reason="invalid_json",
                ) from None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            raise ModelError(
                "model returned invalid JSON",
                code="model_invalid_response",
                stage=stage,
                validation_reason="invalid_json",
            ) from None
        if not isinstance(value, Mapping):
            raise ModelError(
                "model returned an invalid response",
                code="model_invalid_response",
                stage=stage,
                validation_reason="response_shape",
            )
        return value

    @staticmethod
    def _text(value: Any, *, stage: str | None = None) -> str:
        if isinstance(value, str):
            if value.strip():
                return value
        elif isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            text = "".join(parts)
            if text.strip():
                return text
        raise ModelError(
            "model response has no text",
            code="model_invalid_response",
            stage=stage,
            validation_reason="empty_content",
        )
