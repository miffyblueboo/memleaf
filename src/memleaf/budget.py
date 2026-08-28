"""Small, deterministic budgets for automatic memory context."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .models import DirectoryEntry, Memory


# Automatic recall is deliberately a directory, not an implicit full-memory
# read.  Keep these values importable by host adapters so they cannot drift.
MAX_CONTEXT_ITEMS = 3
MAX_CONTEXT_CHARS = 600
MAX_DIRECTORY_TITLE_CHARS = 96
MAX_DIRECTORY_SCOPE_CHARS = 64
MAX_DIRECTORY_SCOPES = 4
# Protocol responses use wider budgets than automatic context injection.  The
# limits bound metadata only; memory bodies are available through read_page.
MAX_SCOPE_CATALOG_ITEMS = 20
MAX_SCOPE_CATALOG_CHARS = 2000
MAX_SEARCH_CANDIDATE_ITEMS = 20
MAX_SEARCH_CANDIDATE_CHARS = 4000


def context_limit(limit: int | None) -> int:
    """Return the requested context limit clamped to the automatic safety cap."""

    if limit is None:
        return MAX_CONTEXT_ITEMS
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    return min(limit, MAX_CONTEXT_ITEMS)


def payload_chars(value: Any) -> int:
    """Measure the compact JSON payload size without changing its contents."""

    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    )


def _display_text(value: str, maximum: int) -> str:
    """Make a bounded, single-line display value without changing storage."""

    value = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    value = value.replace("\t", " ")
    if len(value) <= maximum:
        return value
    if maximum <= 1:
        return value[:maximum]
    return value[: maximum - 1] + "…"


def directory_entry(memory: Memory | DirectoryEntry) -> DirectoryEntry:
    """Build the bounded, body-free representation used for recall."""

    if not isinstance(memory, (Memory, DirectoryEntry)):
        raise TypeError("directory item must be a Memory or DirectoryEntry")
    scopes = memory.scopes
    display_scopes = [
        _display_text(scope, MAX_DIRECTORY_SCOPE_CHARS)
        for scope in list(scopes)[:MAX_DIRECTORY_SCOPES]
    ]
    if len(scopes) > MAX_DIRECTORY_SCOPES:
        display_scopes.append("…")
    return DirectoryEntry(
        memory_id=memory.memory_id,
        title=_display_text(memory.title, MAX_DIRECTORY_TITLE_CHARS),
        scopes=display_scopes,
    )


def fit_directory_items(
    items: Iterable[DirectoryEntry | Memory],
    *,
    limit: int | None = None,
) -> list[DirectoryEntry]:
    """Select directory entries under the count and 600-character budgets."""

    max_items = context_limit(limit)
    if max_items == 0:
        return []
    selected: list[DirectoryEntry] = []
    used = 2  # The surrounding JSON array brackets.
    for item in items:
        if len(selected) >= max_items:
            break
        try:
            entry = directory_entry(item)
            size = payload_chars(entry.to_dict())
        except (TypeError, ValueError, OverflowError):
            continue
        additional = size + (1 if selected else 0)
        if used + additional > MAX_CONTEXT_CHARS:
            continue
        selected.append(entry)
        used += additional
    return selected


__all__ = [
    "MAX_CONTEXT_CHARS",
    "MAX_CONTEXT_ITEMS",
    "MAX_DIRECTORY_SCOPE_CHARS",
    "MAX_DIRECTORY_SCOPES",
    "MAX_DIRECTORY_TITLE_CHARS",
    "MAX_SCOPE_CATALOG_CHARS",
    "MAX_SCOPE_CATALOG_ITEMS",
    "MAX_SEARCH_CANDIDATE_CHARS",
    "MAX_SEARCH_CANDIDATE_ITEMS",
    "context_limit",
    "directory_entry",
    "fit_directory_items",
    "payload_chars",
]
