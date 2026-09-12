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


def _direct_protocol_capable(backend: Any) -> bool:
    """Return whether one concrete backend can speak the B3 output contract."""

    if backend is None:
        return False
    if getattr(backend, "single_pass_safe", False) is True:
        return True
    if getattr(backend, "single_pass_protocol", False) is True:
        return True
    # Raw Python callbacks are adapted by ModelExecutor into CallableBackend.
    # They can consume memleaf's B3 prompt, but they are not strict-deadline
    # safe because caller-owned code may ignore timeout/cancellation entirely.
    return isinstance(backend, CallableBackend)


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

    # Auto mode may fall through from host to API inside one logical complete
    # call. Select B3 only when every route that can actually be reached speaks
    # the same protocol. This remains weaker than ``single_pass_safe``.
    if backend.host is not None:
        if not _direct_protocol_capable(backend.host):
            return False
        return backend.api is None or _direct_protocol_capable(backend.api)
    return _direct_protocol_capable(backend.api)


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

    # Auto routing may execute host first and API second. If either reachable
    # route is a legacy callback, use one self-contained prompt that survives
    # both the prompt-only host call and any later fallback.
    return (
        _direct_requires_inline_system(backend.host)
        or _direct_requires_inline_system(backend.api)
    )


__all__ = [
    "requires_inline_single_pass_system",
    "supports_single_pass_protocol",
]
