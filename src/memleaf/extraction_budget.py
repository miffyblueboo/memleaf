"""Outbound-request limits for unified automatic memory extraction.

Ten seconds is a latency objective, not a correctness or cancellation deadline.
Request timeouts belong to the configured transport (``llm.request_timeout``),
not to this wrapper. Slow, valid responses still reach Core validation/commit.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Mapping

from .llm import ModelError


MAX_MODEL_REQUESTS = 2


class SinglePassBudgetBackend:
    """Limit one fixed-route B3 turn to two actual provider requests.

    ``single_pass_safe`` routes map one ``complete()`` to one provider request,
    without hidden host-to-API fallback. ``reserve_request`` optionally
    persists the request ordinal before dispatch, preventing worker restarts
    from reopening attempts consumed by the same durable work item.

    This class neither overrides the backend timeout nor discards a result
    because an elapsed-time target was missed. Actual transport failures and
    Core validation/commit failures remain errors.
    """

    single_pass_safe = True

    def __init__(
        self,
        backend: Any,
        *,
        reserve_request: Callable[[], int | None] | None = None,
    ):
        if not hasattr(backend, "complete"):
            raise TypeError("single-pass backend must expose complete()")
        if reserve_request is not None and not callable(reserve_request):
            raise TypeError("reserve_request must be callable")
        self._backend = backend
        self._requests = 0
        self._reserve_request = reserve_request

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

    def _request_ordinal(self, *, purpose: str) -> int:
        if self._requests >= MAX_MODEL_REQUESTS:
            raise ModelError(
                "single-pass extraction request limit exhausted",
                code="model_failed",
                stage=purpose or "single_pass",
            )
        if self._reserve_request is None:
            return self._requests + 1
        try:
            ordinal = self._reserve_request()
        except Exception as error:
            raise ModelError(
                "single-pass extraction request budget cannot be reserved",
                code="model_failed",
                stage=purpose or "single_pass",
            ) from error
        if type(ordinal) is not int or not 1 <= ordinal <= MAX_MODEL_REQUESTS:
            raise ModelError(
                "single-pass extraction request limit exhausted",
                code="model_failed",
                stage=purpose or "single_pass",
            )
        return ordinal

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        purpose: str = "",
        temperature: float = 0.0,
    ) -> str:
        self._request_ordinal(purpose=purpose)
        # Count only after the durable reservation succeeded. A process kill
        # after this point still leaves the persistent attempt consumed.
        self._requests += 1
        return self._backend.complete(
            prompt,
            system=system,
            purpose=purpose,
            temperature=temperature,
        )

    def consume_call_metrics(self) -> dict[str, Any]:
        consume = getattr(self._backend, "consume_call_metrics", None)
        if not callable(consume):
            return {}
        try:
            value = consume()
        except Exception:
            return {}
        return dict(value) if isinstance(value, Mapping) else {}


def budget_single_pass_backend(
    backend: Any,
    *,
    reserve_request: Callable[[], int | None] | None = None,
) -> SinglePassBudgetBackend:
    if isinstance(backend, SinglePassBudgetBackend):
        return backend
    return SinglePassBudgetBackend(backend, reserve_request=reserve_request)


__all__ = [
    "MAX_MODEL_REQUESTS",
    "SinglePassBudgetBackend",
    "budget_single_pass_backend",
]
