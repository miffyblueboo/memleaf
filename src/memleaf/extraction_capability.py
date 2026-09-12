"""Capability checks for unified automatic extraction.

Protocol compatibility and transport guarantees are intentionally separate.
A backend may understand B3 without exposing or enforcing a request timeout.
Fixed ``single_pass_safe`` routes support durable outbound-request counting;
transport timeout remains backend-owned. No capability implies a ten-second
failure deadline.
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
    """Return B3 protocol capability without claiming transport cancellation."""

    if _direct_protocol_capable(backend):
        return True
    if not isinstance(backend, ModelRouter):
        return False

    if backend.mode == "api":
        return _direct_protocol_capable(backend.api)
    if backend.mode == "host":
        return _direct_protocol_capable(backend.host)

    # Auto mode can expose both routes over its lifetime. Even though the
    # single-pass call itself is pinned against hidden host->API fallback, B3
    # capability remains fail-closed unless every configured reachable route
    # speaks the same protocol. This prevents a later routing-policy change or
    # route selection from silently changing the output contract.
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

    # Keep the prompt self-contained whenever either configured auto route is
    # a prompt-only callback. Capability gating above ensures both routes speak
    # B3 before automatic extraction chooses this protocol.
    return (
        _direct_requires_inline_system(backend.host)
        or _direct_requires_inline_system(backend.api)
    )


__all__ = [
    "requires_inline_single_pass_system",
    "supports_single_pass_protocol",
]
