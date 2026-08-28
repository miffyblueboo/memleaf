"""Rebuildable JSON indexes derived from Markdown source files."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .models import Memory


# Markers carry only a stable digest, never the caller's event ID.  User text
# is escaped before a marker is appended, so a body line cannot become an
# event merely by looking like a comment.
EVENT_START = "<!-- memleaf:event:v2 -->"
EVENT_CONTENT = "<!-- memleaf:content -->"
EVENT_END = "<!-- memleaf:event-end -->"
EVENT_MARKER = re.compile(r"<!--\s*memleaf:event-key:v1:([0-9a-fA-F]{64})\s*-->")
EVENT_V2_BLOCK = re.compile(
    r"(?ms)^<!-- memleaf:event:v2 -->\r?\n"
    r"(?P<meta>\{[^\r\n]*\})\r?\n"
    r"<!-- memleaf:content -->\r?\n"
    r"(?P<content>.*?)\r?\n"
    r"<!-- memleaf:event-end -->[ \t]*(?:\r?\n|$)"
)
_MARKER_LIKE = re.compile(
    r"<!--\s*(?:memleaf:event-key:v1:[^<\r\n]*|"
    r"memleaf:event:v2|memleaf:content|memleaf:event-end|"
    r"event_id\s*:[^<\r\n]*)\s*-->"
)
_WIKILINK = re.compile(
    r"\[\[([^\[\]\r\n\x00|]*)(?:\|([^\[\]\r\n\x00|]*))?\]\]"
)
_MAX_WIKILINK_TERM_LENGTH = 256
_UNSAFE_WIKILINK_SCHEMES = frozenset(("data", "file", "http", "https", "javascript"))


def normalize_term(value: str) -> str:
    return " ".join(str(value).casefold().strip().split())


def extract_wikilinks(body: str) -> list[str]:
    """Return bounded, normalized Obsidian link targets and display terms.

    This is deliberately an index helper, not a graph parser.  Only the
    target and optional display text are indexed; the Markdown remains the
    source of truth and is never rewritten.
    """

    if not isinstance(body, str):
        return []
    terms: list[str] = []
    seen: set[str] = set()
    for match in _WIKILINK.finditer(body):
        target = normalize_term(match.group(1) or "")
        if not target:
            continue
        link_terms = [target]
        display = normalize_term(match.group(2) or "")
        if display:
            link_terms.append(display)
        link_is_safe = True
        for term in link_terms:
            if not term or len(term) > _MAX_WIKILINK_TERM_LENGTH:
                link_is_safe = False
                break
            if any(char in term for char in ("\x00", "\n", "\r")):
                link_is_safe = False
                break
            # Avoid treating path/URI-like or traversal-looking input as a
            # local search term while retaining normal note names such as
            # ``project:Orion``.
            if (
                term.startswith(("/", "\\"))
                or ".." in term
                or "\\" in term
                or "://" in term
                or term.partition(":")[0] in _UNSAFE_WIKILINK_SCHEMES
                or any(ord(char) < 0x20 for char in term)
            ):
                link_is_safe = False
                break
        if not link_is_safe:
            continue
        for term in link_terms:
            if term not in seen:
                seen.add(term)
                terms.append(term)
    return terms


def event_key(event_id: str) -> str:
    return hashlib.sha256(event_id.encode("utf-8")).hexdigest()


def turn_key(turn_id: str) -> str:
    """Return the stable grouping key for a raw turn id."""

    return event_key(turn_id)


def extract_event_keys(text: str) -> list[str]:
    keys: list[str] = []
    for metadata in extract_event_metadata(text):
        key = metadata.get("event_key")
        if isinstance(key, str) and re.fullmatch(r"[0-9a-fA-F]{64}", key):
            keys.append(key.casefold())
    v2_keys = set(keys)
    # v1 is intentionally read for compatibility with already captured
    # inbox files.  Do not duplicate the compatibility marker emitted beside
    # a v2 block.
    keys.extend(
        match.group(1).casefold()
        for match in EVENT_MARKER.finditer(text)
        if match.group(1).casefold() not in v2_keys
    )
    return list(dict.fromkeys(keys))


def extract_event_metadata(text: str) -> list[dict]:
    """Read structured v2 event metadata and legacy v1 digest markers.

    Only a v2 marker immediately followed by one JSON metadata line and the
    content/end markers is accepted.  This keeps ordinary user text from
    becoming an event during index rebuild.
    """

    result: list[dict] = []
    for match in EVENT_V2_BLOCK.finditer(text):
        try:
            metadata = json.loads(match.group("meta"))
        except (TypeError, ValueError):
            continue
        if not isinstance(metadata, dict):
            continue
        key = metadata.get("event_key")
        role = metadata.get("role")
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", key):
            continue
        if role not in ("user", "assistant"):
            continue
        metadata = dict(metadata)
        metadata["event_key"] = key.casefold()
        metadata["content"] = match.group("content")
        result.append(metadata)
    v2_keys = {item.get("event_key") for item in result}
    for match in EVENT_MARKER.finditer(text):
        key = match.group(1).casefold()
        if key in v2_keys:
            continue
        result.append(
            {
                "event_key": key,
                "role": None,
                "turn_id": None,
                "turn_key": None,
                "turn_index": None,
                "content": "",
                "legacy": True,
            }
        )
    return result


def extract_event_ids(text: str) -> list[str]:
    """Compatibility name; new markers expose only stable event keys."""

    return extract_event_keys(text)


def escape_event_markers(text: str) -> str:
    """Prevent user-authored marker-looking comments from being indexed."""

    def replace(match: re.Match) -> str:
        return match.group(0).replace("<!--", "&lt;!--").replace("-->", "--&gt;")

    return _MARKER_LIKE.sub(replace, text)


def _term_map(memories: Sequence[Memory], attribute: str) -> dict[str, list[str]]:
    values: dict[str, set[str]] = defaultdict(set)
    for memory in memories:
        for raw_term in getattr(memory, attribute):
            term = normalize_term(raw_term)
            if term:
                values[term].add(memory.memory_id)
    return {term: sorted(memory_ids) for term, memory_ids in sorted(values.items())}


def _area_index(memories: Sequence[Memory]) -> dict[str, dict[str, list[str]]]:
    wikilinks: dict[str, set[str]] = defaultdict(set)
    for memory in memories:
        for term in extract_wikilinks(memory.body):
            wikilinks[term].add(memory.memory_id)
    return {
        "tags": _term_map(memories, "tags"),
        "aliases": _term_map(memories, "aliases"),
        "keywords": _term_map(memories, "keywords"),
        "wikilinks": {
            term: sorted(memory_ids)
            for term, memory_ids in sorted(wikilinks.items())
        },
    }


def build_tags_index(knowledge: Sequence[Memory], history: Sequence[Memory]) -> dict:
    active = _area_index(knowledge)
    historical = _area_index(history)
    return {
        "version": 1,
        "tags": active["tags"],
        "aliases": active["aliases"],
        "keywords": active["keywords"],
        "wikilinks": active["wikilinks"],
        "history": historical,
    }


def build_processed_index(
    event_keys: Iterable[str],
    *,
    existing: Mapping | None = None,
    sessions: Mapping | None = None,
) -> dict:
    unique = sorted(
        {
            key.casefold()
            for key in event_keys
            if isinstance(key, str) and re.fullmatch(r"[0-9a-fA-F]{64}", key)
        }
    )
    events: dict[str, dict] = {}
    previous = existing.get("events", {}) if isinstance(existing, Mapping) else {}
    if isinstance(previous, Mapping):
        for key, value in previous.items():
            if (
                isinstance(key, str)
                and re.fullmatch(r"[0-9a-fA-F]{64}", key)
                and key.casefold() in unique
                and isinstance(value, Mapping)
            ):
                # Preserve only safe, structural metadata.  In particular,
                # never carry forward a legacy raw event_id field.
                safe = {"event_key": key.casefold()}
                for name in (
                    "source",
                    "session_id",
                    "turn_id",
                    "turn_key",
                    "role",
                    "turn_index",
                    "captured_at",
                ):
                    item = value.get(name)
                    if isinstance(item, (str, int)) and not isinstance(item, bool):
                        safe[name] = item
                events[key.casefold()] = safe
    for key in unique:
        events.setdefault(key, {"event_key": key})
    value = {
        "version": 1,
        "event_keys": unique,
        "events": events,
    }
    if isinstance(sessions, Mapping):
        value["sessions"] = deepcopy(dict(sessions))
    elif isinstance(existing, Mapping) and isinstance(existing.get("sessions"), Mapping):
        value["sessions"] = deepcopy(dict(existing["sessions"]))
    else:
        value["sessions"] = {}
    return value
