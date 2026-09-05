"""Append-only visible-event capture with event-level idempotency."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from .index import (
    EVENT_CONTENT,
    EVENT_END,
    EVENT_START,
    event_key,
    escape_event_markers,
    extract_event_keys,
    extract_event_metadata,
    turn_key,
)
from .locking import atomic_write_json, atomic_write_text, read_json
from .models import CaptureResult
from .redaction import redact_text
from .vault import Vault, safe_component


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")



_MAIL_EVIDENCE_FIELDS = frozenset({"message_id", "subject", "sender", "domain"})
_MAIL_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.IGNORECASE)
_MAX_MAIL_EVIDENCE_ITEMS = 8
_MAX_MAIL_EVIDENCE_TEXT = 320


def _normalize_tool_evidence(value: Any) -> list[dict[str, str]]:
    from .provenance import normalize_tool_evidence
    return normalize_tool_evidence(value)

def _event_id(source: str, session_id: str, turn_id: str, role: str, event_id: Optional[str]) -> str:
    if event_id is not None:
        if not isinstance(event_id, str) or not event_id or "\x00" in event_id or "\n" in event_id or "\r" in event_id:
            raise ValueError("invalid event id")
        return event_id
    return f"{source}/{session_id}/{turn_id}/{role}"


def _empty_processed() -> dict:
    return {"version": 1, "event_keys": [], "events": {}, "sessions": {}}


def _read_processed(path: Path) -> dict:
    if not path.exists() or path.is_symlink():
        return _empty_processed()
    try:
        value = read_json(path)
    except (OSError, ValueError, TypeError):
        return _empty_processed()
    if not isinstance(value, dict):
        return _empty_processed()
    value.setdefault("version", 1)
    value.setdefault("event_keys", [])
    value.setdefault("events", {})
    value.setdefault("sessions", {})
    return value


def _known_event_keys(vault: Vault, processed: dict) -> set[str]:
    known: set[str] = set()
    event_keys = processed.get("event_keys", [])
    if isinstance(event_keys, list):
        known.update(
            item.casefold()
            for item in event_keys
            if isinstance(item, str) and len(item) == 64
        )
    events = processed.get("events", {})
    if isinstance(events, dict):
        known.update(key.casefold() for key in events if isinstance(key, str) and len(key) == 64)
    for path in vault.list_markdown("inbox"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        known.update(extract_event_keys(text))
    return known


def _new_session_text(source: str, session_id: str, timestamp: str) -> str:
    return (
        f"# Session {source}/{session_id}\n"
        f"- source: {source}\n"
        f"- session_id: {session_id}\n"
        f"- started: {timestamp}\n"
        f"- updated: {timestamp}\n\n"
    )


def _session_key(source: str, session_id: str) -> str:
    return f"{source}/{session_id}"


def _safe_turn_id(turn_id: str) -> str:
    # A turn id is useful to the parser but can contain a password/token label
    # when supplied by a host.  Keep its stable redacted representation only.
    return redact_text(turn_id)


def _turn_index(
    existing: str,
    processed: dict,
    session_key: str,
    raw_turn_id: str,
    persisted_turn_id: str,
) -> tuple[int, dict]:
    stable_turn_key = turn_key(raw_turn_id)
    sessions = processed.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    state = sessions.get(session_key)
    if not isinstance(state, dict):
        state = {}
    turns = state.get("turns")
    if not isinstance(turns, dict):
        turns = {}
    for candidate_key in (stable_turn_key, persisted_turn_id):
        if candidate_key in turns and isinstance(turns[candidate_key], int) and not isinstance(turns[candidate_key], bool):
            return turns[candidate_key], state

    maximum = 0
    for metadata in extract_event_metadata(existing):
        index = metadata.get("turn_index")
        if isinstance(index, int) and not isinstance(index, bool):
            maximum = max(maximum, index)
        if (
            metadata.get("turn_key") == stable_turn_key
            or (not metadata.get("turn_key") and metadata.get("turn_id") == persisted_turn_id)
        ) and isinstance(index, int):
            turns[stable_turn_key] = index
            state["turns"] = turns
            return index, state
    next_index = state.get("next_turn_index", maximum + 1)
    if not isinstance(next_index, int) or isinstance(next_index, bool) or next_index < 1:
        next_index = maximum + 1
    turns[stable_turn_key] = next_index
    state["turns"] = turns
    state["next_turn_index"] = next_index + 1
    return next_index, state


def _append_event(
    existing: str,
    source: str,
    session_id: str,
    turn_id: str,
    role: str,
    content: str,
    event_digest: str,
    event_turn_key: str,
    turn_index: int,
    tool_evidence: list[dict[str, str]] | None = None,
) -> str:
    timestamp = _timestamp()
    if not existing:
        existing = _new_session_text(source, session_id, timestamp)
    updated = re.sub(r"(?m)^- updated:.*$", f"- updated: {timestamp}", existing, count=1)
    if updated == existing and not existing.endswith("\n"):
        updated += "\n"
    if not updated.endswith("\n\n"):
        updated = updated.rstrip("\n") + "\n\n"
    metadata = {
        "event_key": event_digest,
        "role": role,
        "session_id": session_id,
        "source": source,
        "turn_id": turn_id,
        "turn_key": event_turn_key,
        "turn_index": turn_index,
        "timestamp": timestamp,
    }
    if tool_evidence:
        metadata["tool_evidence"] = [dict(item) for item in tool_evidence]
    metadata_line = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (
        f"{updated}{EVENT_START}\n"
        f"{metadata_line}\n"
        f"{EVENT_CONTENT}\n"
        f"{content.rstrip(chr(10))}\n"
        f"{EVENT_END}\n"
        # Keep the digest-only v1 marker for older local readers.  The body
        # is escaped before this point, so only this writer can emit a live
        # legacy marker.
        f"<!-- memleaf:event-key:v1:{event_digest} -->\n"
    )


def _safe_event_entry(
    *, source: str, session_id: str, turn_id: str, role: str, turn_index: int, captured_at: str
) -> dict:
    return {
        "event_key": "",
        "source": source,
        "session_id": session_id,
        "turn_id": turn_id,
        "role": role,
        "turn_index": turn_index,
        "captured_at": captured_at,
    }


def capture_event(
    vault: Vault,
    *,
    source: str,
    session_id: str,
    turn_id: str,
    role: str,
    content: str,
    event_id: Optional[str] = None,
    record: bool = True,
    visible: bool = True,
    tool_evidence: Any = None,
) -> CaptureResult:
    """Capture one visible event; all persisted text is redacted first."""

    source = safe_component(source, "source")
    session_id = safe_component(session_id, "session id")
    if not isinstance(turn_id, str) or not turn_id or "\x00" in turn_id or "\n" in turn_id or "\r" in turn_id:
        raise ValueError("invalid turn id")
    role = safe_component(str(role), "role")
    if not isinstance(content, str):
        raise TypeError("captured content must be text")
    resolved_event_id = _event_id(source, session_id, turn_id, role, event_id)
    resolved_event_key = event_key(resolved_event_id)
    resolved_turn_key = turn_key(turn_id)
    persisted_turn_id = _safe_turn_id(turn_id)
    safe_content = escape_event_markers(redact_text(content))
    safe_tool_evidence = _normalize_tool_evidence(tool_evidence)
    if not record or not visible or role not in ("user", "assistant"):
        return CaptureResult(resolved_event_id, stored=False, duplicate=False, content=safe_content)

    with vault.lock():
        processed = _read_processed(vault.processed_index_path)
        known_keys = _known_event_keys(vault, processed)
        if resolved_event_key in known_keys:
            path = vault.session_path(source, session_id)
            return CaptureResult(
                resolved_event_id,
                stored=False,
                duplicate=True,
                path=path if path.exists() else None,
                content=safe_content,
            )

        path = vault.session_path(source, session_id)
        if path.is_symlink():
            raise ValueError("unsafe inbox session path")
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
        except (OSError, UnicodeError) as error:
            raise ValueError("cannot read inbox session") from error
        if resolved_event_key in set(extract_event_keys(existing)):
            return CaptureResult(resolved_event_id, stored=False, duplicate=True, path=path, content=safe_content)

        session_key = _session_key(source, session_id)
        turn_index, state = _turn_index(
            existing,
            processed,
            session_key,
            turn_id,
            persisted_turn_id,
        )
        updated = _append_event(
            existing,
            source,
            session_id,
            persisted_turn_id,
            role,
            safe_content,
            resolved_event_key,
            resolved_turn_key,
            turn_index,
            safe_tool_evidence,
        )
        atomic_write_text(path, updated)

        event_keys = _known_event_keys(vault, processed)
        event_keys.add(resolved_event_key)
        existing_events = processed.get("events", {})
        event_entries = {
            key: dict(value)
            for key, value in existing_events.items()
            if isinstance(key, str) and len(key) == 64 and isinstance(value, dict)
        } if isinstance(existing_events, dict) else {}
        captured_at = _timestamp()
        entry = _safe_event_entry(
            source=source,
            session_id=session_id,
            turn_id=persisted_turn_id,
            role=role,
            turn_index=turn_index,
            captured_at=captured_at,
        )
        entry["turn_key"] = resolved_turn_key
        entry["event_key"] = resolved_event_key
        event_entries[resolved_event_key] = entry
        sessions = processed.get("sessions", {})
        if not isinstance(sessions, dict):
            sessions = {}
        # Merge only capture-owned fields.  In particular, do not replace a
        # processer's in-flight ``processing`` state during concurrent capture.
        new_state = dict(state)
        old_state = sessions.get(session_key)
        if isinstance(old_state, dict) and "processing" in old_state:
            new_state["processing"] = old_state["processing"]
        sessions[session_key] = new_state
        atomic_write_json(
            vault.processed_index_path,
            {
                "version": 1,
                "event_keys": sorted(event_keys),
                "events": event_entries,
                "sessions": sessions,
            },
        )
        return CaptureResult(resolved_event_id, stored=True, duplicate=False, path=path, content=safe_content)
