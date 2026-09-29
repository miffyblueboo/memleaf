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
_PROVENANCE_PREFIX = re.compile(r"^[ \t]*(?:来源|source)[：:][ \t]*", re.IGNORECASE)


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


def remove_source_time_provenance_date(body: str, events: list[Mapping[str, Any]]) -> str:
    """Drop only a redundant source-day stamp at the start of a provenance line.

    The source chain is stored by Core. A message timestamp is not evidence for
    a date in the memory's facts or deadline, even when a model copies that day
    into a decorative ``来源：`` line. Leave every other date unchanged; provenance matching does not
    authorize or reject business dates.
    """
    source_days = {
        anchor.date().isoformat()
        for event in events
        if (anchor := parse_source_time(event.get("source_time"))) is not None
    }
    if not source_days or not isinstance(body, str):
        return body
    original_lines = body.splitlines(keepends=True)
    footer_index = max((index for index, line in enumerate(original_lines) if line.strip()), default=-1)
    lines = []
    for index, line in enumerate(original_lines):
        # Only a footer is presentation metadata. A date in the middle of
        # task content must still be treated as a possible business fact.
        if index != footer_index:
            lines.append(line)
            continue
        prefix = _PROVENANCE_PREFIX.match(line)
        if prefix:
            token = next((token for token in calendar_tokens(line)
                          if token.start == prefix.end() and token.has_year), None)
            if token is not None and token.canonical in source_days:
                # Retain any text after the stamp; only source metadata is
                # normalized, never a task statement or a different date.
                line = line[:token.start] + line[token.end:].lstrip(" \t")
        lines.append(line)
    return "".join(lines)


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
    # A model-selected expression can already spell out its absolute date,
    # e.g. "明天（2026-09-30）". Resolve that local annotation even when an
    # explicit remember call carries no source timestamp. Do not borrow an
    # unrelated date elsewhere in the expression or infer a missing year.
    if local_day is None:
        def explicit_annotation(match: re.Match[str]) -> str:
            tail = view[match.end():]
            note = re.match(r"\s*(?:（([^（）()]*)）|\(([^（）()]*)\))", tail)
            if note is None:
                return match.group()
            literal = (note.group(1) if note.group(1) is not None else note.group(2)).strip()
            dates = calendar_tokens(literal)
            if (len(dates) == 1 and dates[0].has_year and dates[0].canonical
                    and dates[0].raw == literal):
                return dates[0].canonical
            return match.group()

        view = _RELATIVE_CALENDAR_EXPRESSION.sub(explicit_annotation, view)
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
