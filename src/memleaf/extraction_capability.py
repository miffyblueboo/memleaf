"""Capability checks for unified automatic extraction.

Protocol compatibility and hard latency safety are intentionally separate.
A backend may understand the B3 single-pass contract without being able to
honor transport-level cancellation/deadlines. Automatic extraction can use
one semantic protocol in both cases, while Processor applies the strict
8/10-second budget only to ``single_pass_safe`` transports.
"""
from __future__ import annotations

from typing import Any

from .llm import CallableBackend, ModelRouter


def _callable_protocol_override(backend: CallableBackend) -> bool | None:
    """Return a caller-declared protocol override for callback adapters.

    Raw callbacks default to the unified B3 contract. A callback may set
    ``single_pass_protocol = False`` when it intentionally implements the
    legacy staged test/compatibility protocol. Product code never needs this
    for ordinary host callbacks; the escape hatch prevents capability
    inference from rewriting explicitly versioned compatibility fixtures.
    """

    callback = getattr(backend, "callback", None)
    value = getattr(callback, "single_pass_protocol", None)
    return value if isinstance(value, bool) else None


def _direct_protocol_capable(backend: Any) -> bool:
    """Return whether one concrete backend can speak the B3 output contract."""

    if backend is None:
        return False
    if getattr(backend, "single_pass_safe", False) is True:
        return True
    declared = getattr(backend, "single_pass_protocol", None)
    if isinstance(declared, bool):
        return declared
    if isinstance(backend, CallableBackend):
        override = _callable_protocol_override(backend)
        if override is not None:
            return override
        # Raw Python callbacks are adapted by ModelExecutor into
        # CallableBackend. They can consume memleaf's B3 prompt, but are not
        # strict-deadline safe because caller-owned code may ignore
        # timeout/cancellation entirely.
        return True
    return False


def supports_single_pass_protocol(backend: Any) -> bool:
    """Return B3 protocol capability without claiming strict transport SLA."""

    if _direct_protocol_capable(backend):
        return True
    if not isinstance(backend, ModelRouter):
        return False

    if backend.mode == "api":
        return _direct_protocol_capable(backend.api)
    if backend.mode == "host":
        return _direct_protocol_capable(backend.host)

    # B3 auto routing is fixed to the first reachable route: ModelRouter does
    # not perform hidden host->API fallback for purpose="single_pass". Only
    # the actually selected route therefore needs to speak the B3 contract.
    selected = backend.host if backend.host is not None else backend.api
    return _direct_protocol_capable(selected)


def _direct_requires_inline_system(backend: Any) -> bool:
    """Return whether a concrete callback may receive only the prompt value.

    CallableBackend intentionally supports legacy ``callback(prompt)``
    signatures. In that mode its ``system`` argument cannot reach the caller,
    so B3 must inline the system contract into the prompt rather than silently
    dropping the planner rules. This says nothing about latency safety.
    """

    return isinstance(backend, CallableBackend)


def requires_inline_single_pass_system(backend: Any) -> bool:
    """Keep B3 instructions visible across prompt-only callback routes."""

    if _direct_requires_inline_system(backend):
        return True
    if not isinstance(backend, ModelRouter):
        return False
    if backend.mode == "api":
        return _direct_requires_inline_system(backend.api)
    if backend.mode == "host":
        return _direct_requires_inline_system(backend.host)

    selected = backend.host if backend.host is not None else backend.api
    return _direct_requires_inline_system(selected)


__all__ = [
    "requires_inline_single_pass_system",
    "supports_single_pass_protocol",
]
