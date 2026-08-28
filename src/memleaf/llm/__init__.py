"""Model backends and explicit routing for stage B."""

from .base import (
    DEFAULT_REQUEST_TIMEOUT,
    MAX_REQUEST_TIMEOUT,
    MIN_REQUEST_TIMEOUT,
    MODEL_ERROR_CODES,
    MODEL_FINISH_REASONS,
    MODEL_RESPONSE_DIAGNOSTIC_FIELDS,
    MODEL_VALIDATION_REASONS,
    CallableBackend,
    FakeBackend,
    HostBackend,
    ModelBackend,
    ModelError,
    ModelUnavailable,
)
from .claude_compatible import ClaudeCompatibleBackend
from .gemini import GeminiBackend
from .openai_compatible import OpenAICompatibleBackend
from .router import ModelRouter
from ..validation import ModelOutputError

__all__ = [
    "CallableBackend",
    "ClaudeCompatibleBackend",
    "DEFAULT_REQUEST_TIMEOUT",
    "FakeBackend",
    "GeminiBackend",
    "HostBackend",
    "MAX_REQUEST_TIMEOUT",
    "MIN_REQUEST_TIMEOUT",
    "ModelBackend",
    "MODEL_ERROR_CODES",
    "MODEL_FINISH_REASONS",
    "MODEL_RESPONSE_DIAGNOSTIC_FIELDS",
    "MODEL_VALIDATION_REASONS",
    "ModelError",
    "ModelOutputError",
    "ModelRouter",
    "ModelUnavailable",
    "OpenAICompatibleBackend",
]
