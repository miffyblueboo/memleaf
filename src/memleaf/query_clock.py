"""Freeze a Todo query's calendar day across pagination without a timer."""
from __future__ import annotations
import base64
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .retrieval import RetrievalError
from .validation import parse_strict_json


def query_clock(cursor, as_of, timezone_name):
    bound = None
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError()
            obj = parse_strict_json(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii"))
            if obj.get("kind") != "active_todos" or not isinstance(obj.get("clock"), dict):
                raise ValueError()
            bound = obj["clock"]
            if set(bound) != {"as_of", "timezone"}:
                raise ValueError()
        except (ValueError, TypeError, AttributeError, UnicodeError):
            raise RetrievalError("invalid_cursor", "todo cursor has no valid query clock") from None
    name = timezone_name if timezone_name is not None else bound["timezone"] if bound else "UTC"
    if not isinstance(name, str) or not name or len(name) > 100:
        raise ValueError("invalid_query_timezone")
    try:
        zone = timezone.utc if name == "UTC" else ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("invalid_query_timezone") from None
    day = as_of if as_of is not None else bound["as_of"] if bound else datetime.now(zone).date().isoformat()
    if not isinstance(day, str) or len(day) != 10:
        raise ValueError("invalid_query_as_of")
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError()
    except ValueError:
        raise ValueError("invalid_query_as_of") from None
    result = {"as_of": day, "timezone": name}
    if bound is not None and result != bound:
        raise RetrievalError("stale_cursor", "todo query clock changed; restart the query")
    return result
