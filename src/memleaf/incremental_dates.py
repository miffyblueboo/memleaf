"""Calendar conversion for *model-selected* expressions, never deadline inference.

The incremental compiler supplies an exact supported expression. This module
retains it even outside the bounded calendar grammar; it never scans the whole
message to guess which date is an action deadline.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping

from .validation import calendar_tokens, normalize_relative_calendar_text, _RELATIVE_CALENDAR_EXPRESSION


def parse_source_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid_source_time")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid_source_time") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("invalid_source_time")
    return result


def reading_text(value: str) -> str:
    # Presentation-only normalization. Keep parentheses, numbers and qualifiers.
    return re.sub(r"\*\*|__|`", "", value)


def source_basis(evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {key: evidence[key] for key in (
        "source", "session_id", "event_key", "message_id", "message_revision",
        "source_time", "source_sequence",
    ) if key in evidence and evidence[key] is not None}


def selected_calendar(text: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Return a lossless text/anchor projection plus an optional calendar date.

    Pass a local *date* to the legacy calendar grammar, which otherwise converts
    datetime anchors to UTC. A midnight-offset message must keep its source day.
    No configured processing clock or capture time is used as a substitute.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 512:
        raise ValueError("invalid_time_expression")
    if reading_text(text) not in reading_text(str(evidence.get("text", ""))):
        raise ValueError("unsupported_time_quote")
    anchor = parse_source_time(evidence.get("source_time"))
    local_day = anchor.date() if anchor is not None else None
    view = reading_text(text)
    converted = normalize_relative_calendar_text(view, local_day)
    if local_day is None and not _RELATIVE_CALENDAR_EXPRESSION.search(view):
        converted = view
    tokens = calendar_tokens(converted if converted is not None else view, local_day)
    dates = {token.canonical for token in tokens}
    # Relative expressions which cannot be resolved must not borrow another date.
    resolved = (converted is not None and bool(tokens)
                and None not in dates and len(dates) == 1)
    return {
        "date": next(iter(dates)) if resolved else None,
        "text": text,
        "anchor": source_basis(evidence),
        "status": "resolved" if resolved else "unresolved",
    }
