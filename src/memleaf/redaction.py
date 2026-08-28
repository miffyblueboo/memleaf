"""Best-effort local redaction for common credentials before persistence."""

from __future__ import annotations

import re


_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_COOKIE_HEADER = re.compile(r"(?im)^(\s*(?:cookie|set-cookie)\s*:\s*).+$")
_BEARER = re.compile(r"(?i)(\b(?:authorization\s*:\s*|bearer\s+))([A-Za-z0-9._~+/=-]{16,})")
_KNOWN_TOKEN = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9]{16,}|sk-proj-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{16,}|AKIA[0-9A-Z]{16})\b"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
_QUOTED_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?key|secret(?:[_-]?key)?|password|passwd|token|cookie|private[_-]?key)\b\s*[:=]\s*)([\"'])(.*?)(\2)"
)
_PLAIN_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?key|secret(?:[_-]?key)?|password|passwd|token|cookie|private[_-]?key)\b\s*[:=]\s*)([^\s,;]+)"
)


def redact_text(text: str) -> str:
    """Redact common secret shapes while preserving surrounding labels."""

    if not isinstance(text, str):
        raise TypeError("captured content must be text")
    redacted = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", text)
    redacted = _COOKIE_HEADER.sub(r"\1[REDACTED_COOKIE]", redacted)
    redacted = _BEARER.sub(r"\1[REDACTED_TOKEN]", redacted)
    redacted = _QUOTED_ASSIGNMENT.sub(r"\1\2[REDACTED_SECRET]\4", redacted)
    redacted = _PLAIN_ASSIGNMENT.sub(r"\1[REDACTED_SECRET]", redacted)
    redacted = _KNOWN_TOKEN.sub("[REDACTED_TOKEN]", redacted)
    redacted = _JWT.sub("[REDACTED_TOKEN]", redacted)
    return redacted


redact_secrets = redact_text
