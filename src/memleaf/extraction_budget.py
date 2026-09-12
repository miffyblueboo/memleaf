"""Outbound-request accounting and advisory extraction latency metrics.

Ten seconds is a performance target, not permission to cancel useful work or
discard a valid memory. The transport owns the configured request timeout;
this module only limits request count and observes complete-turn latency.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Iterable, Mapping

from .llm import ModelError


MAX_MODEL_REQUESTS = 2
TARGET_TOTAL_SECONDS = 10.0


class SinglePassBudgetBackend:
    """Limit one logical B3 turn to two actual provider requests.

    ``single_pass_safe`` routes map one complete() call to one request, without
    hidden host-to-API fallback. The optional durable reservation preserves
    consumed attempts across worker restarts. Neither the first request nor
    its repair overrides the transport's configured ``llm.request_timeout``.
    """

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
    def single_pass_safe(self) -> bool:
        """Preserve the underlying adapter/route request-boundary guarantee."""

        return getattr(self._backend, "single_pass_safe", False) is True

    @property
    def single_pass_protocol(self) -> bool:
        """Preserve protocol identity while this wrapper enforces request count."""

        return getattr(self._backend, "single_pass_protocol", False) is True

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
                "single-pass extraction budget exhausted",
                code="model_timeout",
                stage=purpose or "single_pass",
            )
        if self._reserve_request is None:
            return self._requests + 1
        try:
            ordinal = self._reserve_request()
        except Exception as error:
            raise ModelError(
                "single-pass extraction request budget cannot be reserved",
                code="model_timeout",
                stage=purpose or "single_pass",
            ) from error
        if type(ordinal) is not int or not 1 <= ordinal <= MAX_MODEL_REQUESTS:
            raise ModelError(
                "single-pass extraction budget exhausted",
                code="model_timeout",
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
        # Count only after durable reservation succeeds. A kill from this point
        # onward still consumes that attempt, without imposing a wall deadline.
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


class ExtractionTiming:
    """Observe one processing attempt; never authorize or reject a write.

    This monotonic timer includes local planning/context, model work, validation
    and commit. A worker restart begins a new measured attempt; persisted wall
    timestamps from older releases are not latency-based write restrictions.
    """

    def __init__(self, *, clock: Any = None):
        self._clock = time.monotonic if clock is None else clock
        self._started = self._clock()
        self._commit_started: float | None = None

    def begin_commit(self) -> None:
        self._commit_started = self._clock()

    def finish(self, *, failed: bool = False) -> dict[str, int]:
        ended = self._clock()
        seconds = max(0.0, ended - self._started)
        commit_started = self._commit_started
        planning_end = ended if commit_started is None else commit_started
        return {
            "target_duration_ms": int(TARGET_TOTAL_SECONDS * 1000),
            "turn_count": 1,
            "failed_turn_count": int(failed),
            "over_target_turn_count": int(seconds > TARGET_TOTAL_SECONDS),
            "successful_within_target_count": int(not failed and seconds <= TARGET_TOTAL_SECONDS),
            "commit_accepted_count": int(not failed and commit_started is not None),
            "total_duration_ms": int(seconds * 1000),
            "max_turn_duration_ms": int(seconds * 1000),
            "planning_duration_ms": int(max(0.0, planning_end - self._started) * 1000),
            "commit_duration_ms": int(max(0.0, ended - planning_end) * 1000),
        }


def aggregate_extraction_metrics(values: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Sum allowlisted numeric metrics; never copy arbitrary content to status."""

    summed = (
        "turn_count", "failed_turn_count", "over_target_turn_count",
        "successful_within_target_count", "commit_accepted_count", "total_duration_ms",
        "planning_duration_ms", "commit_duration_ms",
    )
    result = {key: 0 for key in summed}
    result["target_duration_ms"] = int(TARGET_TOTAL_SECONDS * 1000)
    result["max_turn_duration_ms"] = 0
    for value in values:
        if not isinstance(value, Mapping):
            continue
        for key in (*summed, "max_turn_duration_ms"):
            item = value.get(key)
            if type(item) is not int or item < 0:
                continue
            if key == "max_turn_duration_ms":
                result[key] = max(result[key], item)
            else:
                result[key] += item
    return result


def budget_single_pass_backend(
    backend: Any,
    *,
    reserve_request: Callable[[], int | None] | None = None,
) -> SinglePassBudgetBackend:
    if isinstance(backend, SinglePassBudgetBackend):
        return backend
    return SinglePassBudgetBackend(
        backend,
        reserve_request=reserve_request,
    )


__all__ = [
    "MAX_MODEL_REQUESTS",
    "TARGET_TOTAL_SECONDS",
    "ExtractionTiming",
    "aggregate_extraction_metrics",
    "SinglePassBudgetBackend",
    "budget_single_pass_backend",
]
