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

_CLOCK_AFTER_DATE = re.compile(r"\s*(?:(?:日|号|上午|下午|晚上|中午|凌晨|约|在|at|T)\s*){0,2}(\d{1,2}:[0-5]\d)(?!\d)", re.IGNORECASE)


def _dated_clocks(text: str) -> set[tuple[str, str]]:
    pairs = set()
    for token in calendar_tokens(text):
        clock = _CLOCK_AFTER_DATE.match(text, token.end)
        if token.canonical and clock:
            pairs.add((token.canonical, clock.group(1)))
    return pairs


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
    value = {key: evidence[key] for key in (
        "source", "session_id", "event_key", "message_id", "message_revision",
        "source_time", "source_sequence",
    ) if key in evidence and evidence[key] is not None}
    if evidence.get("explicit_input") is not None:
        from .explicit_text_source import validate_origin
        origin = validate_origin(evidence["explicit_input"])
        value.update(input_kind="explicit_text", origin_session_id=origin["session_id"],
                     origin_turn_key=origin["turn_key"])
    return value


def calendar_hints(evidence: Mapping[str, Any]) -> list[dict[str, str]]:
    """Small, source-local calendar facts for the same model call (no processing clock)."""
    anchor = parse_source_time(evidence.get("source_time"))
    if anchor is None:
        return []
    text = reading_text(str(evidence["text"]))
    result: list[dict[str, str]] = []
    for match in _RELATIVE_CALENDAR_EXPRESSION.finditer(text):
        resolved = normalize_relative_calendar_text(match.group(), anchor.date())
        if resolved and resolved != match.group():
            hint = {"text": match.group(), "date": resolved}
            if hint not in result:
                result.append(hint)
    return result[:20]


def invalid_content_dates(fields: Mapping[str, Any], events: list[Mapping[str, Any]],
                          preserved: Mapping[str, Any] | None = None) -> bool:
    """Reject unsupported generated dates, including misdated relative clock pairs.

    Only cited sources and the selected update target may ground new wording.
    Existing memory wording is preserved without a new source claim.
    """
    from .process_common import _summary_date_grounding_violations

    source_texts = [reading_text(str(event["text"])) for event in events]
    grounded: set[str] = set()
    clock_dates: dict[str, set[str]] = {}
    for event, source in zip(events, source_texts):
        anchor = parse_source_time(event.get("source_time"))
        local_day = anchor.date() if anchor is not None else None
        normalized = normalize_relative_calendar_text(source, local_day) if local_day else None
        grounded.update(token.canonical for token in calendar_tokens(source, local_day) if token.canonical)
        if normalized is not None:
            grounded.update(token.canonical for token in calendar_tokens(normalized, local_day) if token.canonical)
            for match in _RELATIVE_CALENDAR_EXPRESSION.finditer(source):
                clock = _CLOCK_AFTER_DATE.match(source, match.end())
                if clock:
                    resolved = normalize_relative_calendar_text(match.group(), local_day)
                    if resolved:
                        clock_dates.setdefault(clock.group(1), set()).add(resolved)
        # An explicit source date at the same clock makes a relative binding
        # ambiguous; in that case the guard must not claim which event it is.
        for date_value, clock_value in _dated_clocks(source):
            clock_dates.setdefault(clock_value, set()).add(date_value)
    prior = preserved or {}
    texts = {key: fields[key] for key in ("title", "body") if key in fields}
    if _summary_date_grounding_violations(texts, grounded_dates=grounded, source_texts=source_texts,
                                          preserved_texts=[prior.get("title"), prior.get("body")]):
        return True
    preserved_pairs = set().union(*(_dated_clocks(text) for text in (prior.get("title"), prior.get("body"))
                                    if isinstance(text, str)))
    for value in texts.values():
        for date_value, clock_value in _dated_clocks(value):
            expected = clock_dates.get(clock_value, set())
            if len(expected) == 1 and date_value not in expected and (date_value, clock_value) not in preserved_pairs:
                return True
    return False


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
