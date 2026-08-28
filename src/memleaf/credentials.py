"""Credential validation helpers shared by discovery and runtime routing."""

from __future__ import annotations

import re
from typing import Any


_REDACTED_LITERALS = frozenset(
    {
        "***",
        "redacted",
        "masked",
        "<redacted>",
        "[redacted]",
        "<masked>",
        "[masked]",
        "(redacted)",
        "(masked)",
        "not shown",
        "hidden",
    }
)
_FULL_MASK_RE = re.compile(r"^[*#_\u2022\u25cfxX]{3,}$")
_TRUNCATED_MASK_RE = re.compile(r"^\S{1,16}(?:\.\.\.|\u2026)\S{1,16}$")


def credential_text(value: Any) -> str | None:
    """Return a usable credential string, rejecting obvious display redaction."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    lowered = text.casefold()
    if lowered in _REDACTED_LITERALS:
        return None
    if _FULL_MASK_RE.fullmatch(text):
        return None
    # Hermes canonical masking keeps a short head/tail and inserts ... in the
    # middle (for example sk-p...7890). Treat that display form as missing.
    if _TRUNCATED_MASK_RE.fullmatch(text):
        return None
    if ("redact" in lowered or "mask" in lowered) and (
        (text.startswith("<") and text.endswith(">"))
        or (text.startswith("[") and text.endswith("]"))
        or (text.startswith("(") and text.endswith(")"))
    ):
        return None
    return text


def is_redacted_credential(value: Any) -> bool:
    """Return True only for non-empty strings that look explicitly redacted."""

    return isinstance(value, str) and bool(value.strip()) and credential_text(value) is None
