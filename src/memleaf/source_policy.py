"""Bounded provenance stored with active Markdown memories."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

MAX_MEMORY_SOURCES = 16


def _source_value(value: Mapping[str, Any]) -> dict[str, Any]:
    return dict(value)


def _fingerprint(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _initial_digest(sources: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for source in sources:
        digest.update(_fingerprint(source).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _bounded(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(values) <= MAX_MEMORY_SOURCES:
        return values
    # Keep the first provenance anchor and the most recent observations.
    return [values[0], *values[-(MAX_MEMORY_SOURCES - 1):]]


def source_state(
    sources: Iterable[Mapping[str, Any]], extra: Mapping[str, Any] | None = None
) -> tuple[int, str, list[dict[str, Any]]]:
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in sources:
        if not isinstance(item, Mapping):
            continue
        value = _source_value(item)
        fingerprint = _fingerprint(value)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        values.append(value)
    metadata = extra if isinstance(extra, Mapping) else {}
    count = metadata.get("source_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < len(values):
        count = len(values)
    digest = metadata.get("source_digest")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest.casefold())
    ):
        digest = _initial_digest(values)
    return count, digest, _bounded(values)


def merge_sources(
    old: Iterable[Mapping[str, Any]],
    current: Iterable[Mapping[str, Any]],
    *,
    extra: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    count, digest, retained = source_state(old, extra)
    seen = {_fingerprint(value) for value in retained}
    for item in current:
        if not isinstance(item, Mapping):
            continue
        value = _source_value(item)
        fingerprint = _fingerprint(value)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        count += 1
        digest = hashlib.sha256(f"{digest}\0{fingerprint}".encode("ascii")).hexdigest()
        retained.append(value)
    retained = _bounded(retained)
    metadata = {
        "source_count": count,
        "source_digest": digest,
        "sources_omitted": max(0, count - len(retained)),
    }
    return retained, metadata


def merge_memory_provenance(memories: Iterable[Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    values = list(memories)
    retained: list[dict[str, Any]] = []
    total_count = 0
    digest_parts: list[str] = []
    for memory in values:
        count, digest, sources = source_state(
            getattr(memory, "sources", ()), getattr(memory, "extra", {})
        )
        total_count += count
        digest_parts.append(digest)
        retained.extend(sources)
    # De-duplicate retained projections while keeping deterministic source order.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in retained:
        fingerprint = _fingerprint(source)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(source)
    unique = _bounded(unique)
    combined_digest = hashlib.sha256(
        "\0".join(digest_parts).encode("ascii")
    ).hexdigest()
    return unique, {
        "source_count": max(total_count, len(unique)),
        "source_digest": combined_digest,
        "sources_omitted": max(0, total_count - len(unique)),
    }


__all__ = ["MAX_MEMORY_SOURCES", "merge_sources", "merge_memory_provenance", "source_state"]
