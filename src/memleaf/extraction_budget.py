"""Latency and outbound-request budget for unified memory extraction.

The budget is deliberately attached to one logical single-pass stage instead
of to an HTTP adapter.  That keeps the product invariant (at most two actual
model requests) independent from provider-specific prompt/transport details.
"""
from __future__ import annotations

import time
from typing import Any, Mapping

from .llm import ModelError


MAX_MODEL_REQUESTS = 2
TARGET_TOTAL_SECONDS = 10.0
MODEL_TIME_BUDGET_SECONDS = 8.0
PRIMARY_REQUEST_MAX_SECONDS = 6.0


class SinglePassBudgetBackend:
    """Wrap one safe backend with the extraction request/time budget.

    `single_pass_safe` routes are already constrained so one `complete()` maps
    to one provider request (no host->API fallback).  The wrapper therefore
    makes the two-call limit a true outbound-request limit for the production
    B3 route, not merely a parser-attempt count.
    """

    single_pass_safe = True

    def __init__(self, backend: Any, *, clock: Any = time.monotonic):
        if not hasattr(backend, "complete"):
            raise TypeError("single-pass backend must expose complete()")
        self._backend = backend
        self._clock = clock
        self._started = float(clock())
        self._deadline = self._started + MODEL_TIME_BUDGET_SECONDS
        self._requests = 0

    @property
    def provider(self) -> str:
        return str(getattr(self._backend, "provider", "unknown"))

    @property
    def model(self) -> str:
        return str(getattr(self._backend, "model", "unknown"))

    @property
    def parallel_safe(self) -> bool:
        return getattr(self._backend, "parallel_safe", False) is True

    @property
    def structured_batch_safe(self) -> bool:
        return getattr(self._backend, "structured_batch_safe", False) is True

    @property
    def request_count(self) -> int:
        return self._requests

    def _remaining(self) -> float:
        return self._deadline - float(self._clock())

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        purpose: str = "",
        temperature: float = 0.0,
    ) -> str:
        remaining = self._remaining()
        if self._requests >= MAX_MODEL_REQUESTS or remaining <= 0:
            raise ModelError(
                "single-pass extraction budget exhausted",
                code="model_timeout",
                stage=purpose or "single_pass",
            )
        cap = min(
            PRIMARY_REQUEST_MAX_SECONDS if self._requests == 0 else remaining,
            remaining,
        )
        if cap <= 0:
            raise ModelError(
                "single-pass extraction budget exhausted",
                code="model_timeout",
                stage=purpose or "single_pass",
            )
        self._requests += 1

        set_timeout = getattr(self._backend, "set_call_timeout", None)
        clear_timeout = getattr(self._backend, "clear_call_timeout", None)
        if callable(set_timeout):
            set_timeout(cap)
        try:
            value = self._backend.complete(
                prompt,
                system=system,
                purpose=purpose,
                temperature=temperature,
            )
        finally:
            if callable(clear_timeout):
                try:
                    clear_timeout()
                except Exception:
                    pass

        # A callback/custom transport may ignore the timeout hook.  Such a
        # late result must never become writable merely because it eventually
        # returned.
        if self._remaining() < 0:
            raise ModelError(
                "single-pass extraction result arrived after deadline",
                code="model_timeout",
                stage=purpose or "single_pass",
            )
        return value

    def consume_call_metrics(self) -> dict[str, Any]:
        consume = getattr(self._backend, "consume_call_metrics", None)
        if not callable(consume):
            return {}
        try:
            value = consume()
        except Exception:
            return {}
        return dict(value) if isinstance(value, Mapping) else {}


def budget_single_pass_backend(backend: Any) -> SinglePassBudgetBackend:
    if isinstance(backend, SinglePassBudgetBackend):
        return backend
    return SinglePassBudgetBackend(backend)


__all__ = [
    "MAX_MODEL_REQUESTS",
    "MODEL_TIME_BUDGET_SECONDS",
    "PRIMARY_REQUEST_MAX_SECONDS",
    "TARGET_TOTAL_SECONDS",
    "SinglePassBudgetBackend",
    "budget_single_pass_backend",
]
