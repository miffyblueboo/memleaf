"""Provider identity and capability resolution without credential coupling."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


PROVIDER_FAMILIES = frozenset({"generic", "openai", "deepseek", "anthropic", "gemini"})
CAPABILITY_SOURCES = frozenset({"explicit", "legacy_provider", "official_host", "unknown"})

_LEGACY_PROVIDER_FAMILY = {
    "openai": "openai",
    "deepseek": "deepseek",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "gemini": "gemini",
    "google": "gemini",
}

# Compatibility inference is deliberately exact-host only.  Subdomains,
# suffixes and model names never authorize provider-specific parameters.
_OFFICIAL_HOST_FAMILY = {
    "api.openai.com": "openai",
    "api.deepseek.com": "deepseek",
    "api.anthropic.com": "anthropic",
    "generativelanguage.googleapis.com": "gemini",
}

_LEGACY_PROVIDER_PROTOCOL = {
    "openai": "openai",
    "deepseek": "openai",
    "anthropic": "claude",
    "claude": "claude",
    "gemini": "gemini",
    "google": "gemini",
}

_PROTOCOL_ALIASES = {
    "openai": "openai",
    "openai-compatible": "openai",
    "openai_compatible": "openai",
    "claude": "claude",
    "anthropic": "claude",
    "gemini": "gemini",
}


@dataclass(frozen=True)
class ProviderCapabilities:
    """Resolved protocol-adjacent capabilities for one configured API route.

    ``provider_family`` is never used to locate credentials.  ``source`` says
    why that capability identity was selected; callers may expose it as a safe
    diagnostic because all values are program-defined.
    """

    provider_family: str
    source: str
    json_object: bool
    json_schema: bool
    output_token_field: str | None
    thinking_control: str
    b3_protocol: bool


_PROFILE_TABLE = {
    "generic": ProviderCapabilities(
        "generic", "unknown", False, False, None, "unsupported", False
    ),
    "openai": ProviderCapabilities(
        "openai", "unknown", True, False, "max_completion_tokens", "openai", True
    ),
    "deepseek": ProviderCapabilities(
        "deepseek", "unknown", True, False, "max_tokens", "deepseek", True
    ),
    # Current built-in Anthropic/Gemini adapters do not provide a verified
    # B3 structured-output boundary, so capability remains fail-closed.
    "anthropic": ProviderCapabilities(
        "anthropic", "unknown", False, False, "max_tokens", "anthropic", False
    ),
    "gemini": ProviderCapabilities(
        "gemini", "unknown", False, False, "maxOutputTokens", "gemini", False
    ),
}


def normalize_provider_family(value: Any, *, allow_empty: bool = True) -> str:
    """Validate a user-supplied capability family without guessing."""

    if value is None or value == "":
        if allow_empty:
            return ""
        return "generic"
    if not isinstance(value, str):
        raise ValueError("llm.provider_family must be a supported provider family")
    normalized = value.casefold().strip()
    if normalized not in PROVIDER_FAMILIES:
        raise ValueError("llm.provider_family must be one of: anthropic, deepseek, gemini, generic, openai")
    return normalized


def normalize_protocol(value: Any, *, provider: Any = None) -> str:
    """Honor an explicit protocol; use exact legacy provider IDs only if absent."""

    if isinstance(value, str) and value.strip():
        key = value.casefold().strip()
        normalized = _PROTOCOL_ALIASES.get(key)
        if normalized is None:
            raise ValueError("llm.protocol is not supported")
        return normalized
    provider_key = provider.casefold().strip() if isinstance(provider, str) else ""
    return _LEGACY_PROVIDER_PROTOCOL.get(provider_key, "openai")


def _official_host_family(base_url: Any) -> str | None:
    if not isinstance(base_url, str) or not base_url.strip():
        return None
    try:
        parsed = urlsplit(base_url.strip())
    except ValueError:
        return None
    if parsed.scheme.casefold() != "https" or not parsed.netloc:
        return None
    # User-info makes the URL unsuitable for security-sensitive capability
    # inference even if the hostname itself looks official.
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        hostname = parsed.hostname.casefold() if parsed.hostname else ""
        port = parsed.port
    except ValueError:
        return None
    if port not in (None, 443):
        return None
    return _OFFICIAL_HOST_FAMILY.get(hostname)


def resolve_provider_capabilities(
    *,
    provider: Any = None,
    provider_family: Any = None,
    base_url: Any = None,
) -> ProviderCapabilities:
    """Resolve capability identity by explicit > legacy ID > exact host > unknown."""

    explicit = normalize_provider_family(provider_family)
    if explicit:
        family, source = explicit, "explicit"
    else:
        provider_key = provider.casefold().strip() if isinstance(provider, str) else ""
        legacy = _LEGACY_PROVIDER_FAMILY.get(provider_key)
        if legacy is not None:
            family, source = legacy, "legacy_provider"
        else:
            host_family = _official_host_family(base_url)
            if host_family is not None:
                family, source = host_family, "official_host"
            else:
                family, source = "generic", "unknown"
    profile = _PROFILE_TABLE[family]
    return ProviderCapabilities(
        provider_family=profile.provider_family,
        source=source,
        json_object=profile.json_object,
        json_schema=profile.json_schema,
        output_token_field=profile.output_token_field,
        thinking_control=profile.thinking_control,
        b3_protocol=profile.b3_protocol,
    )


def legacy_protocol_for_provider(provider: Any) -> str | None:
    """Return exact legacy protocol mapping without substring matching."""

    if not isinstance(provider, str):
        return None
    return _LEGACY_PROVIDER_PROTOCOL.get(provider.casefold().strip())


__all__ = [
    "CAPABILITY_SOURCES",
    "PROVIDER_FAMILIES",
    "ProviderCapabilities",
    "legacy_protocol_for_provider",
    "normalize_protocol",
    "normalize_provider_family",
    "resolve_provider_capabilities",
]
