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
from .turn_plan import turn_identity_key
from .vault import Vault, safe_component


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _optional_identifier(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 800 or any(char in value for char in "\x00\r\n"):
        raise ValueError(f"invalid {label}")
    return value


def _source_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 80:
        raise ValueError("invalid source time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid source time") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("source time must include a timezone")
    return value


def _event_identity(
    source: str,
    session_id: str,
    turn_id: str,
    role: str,
    event_id: Optional[str],
    message_id: Optional[str],
    message_revision: Optional[str],
) -> tuple[str, str, str]:
    resolved_event_id = _event_id(source, session_id, turn_id, role, event_id)
    resolved_message_id = message_id or resolved_event_id
    resolved_revision = message_revision or "1"
    if message_id is not None or message_revision is not None:
        identity = json.dumps(
            [source, session_id, resolved_message_id, resolved_revision],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return resolved_event_id, resolved_message_id, event_key(identity)
    return resolved_event_id, resolved_message_id, event_key(resolved_event_id)




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
    *,
    message_id: str,
    message_revision: str,
    previous_message_revision: str | None,
    source_sequence: int | None,
    previous_message_id: str | None,
    source_time: str | None,
    captured_at: str,
    final: bool | None,
    tool_evidence: list[dict[str, str]] | None = None,
) -> str:
    if not existing:
        existing = _new_session_text(source, session_id, captured_at)
    updated = re.sub(r"(?m)^- updated:.*$", f"- updated: {captured_at}", existing, count=1)
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
        "timestamp": captured_at,
        "captured_at": captured_at,
        "message_id": message_id,
        "message_revision": message_revision,
    }
    if source_sequence is not None:
        metadata["source_sequence"] = source_sequence
    if previous_message_revision is not None:
        metadata["previous_message_revision"] = previous_message_revision
    if previous_message_id is not None:
        metadata["previous_message_id"] = previous_message_id
    if source_time is not None:
        metadata["source_time"] = source_time
    if final is not None:
        metadata["final"] = final
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
    *, source: str, session_id: str, turn_id: str, role: str, turn_index: int,
    captured_at: str, message_id: str, message_revision: str,
    previous_message_revision: str | None,
    source_sequence: int | None, previous_message_id: str | None,
    source_time: str | None, final: bool | None,
) -> dict:
    result = {
        "event_key": "",
        "source": source,
        "session_id": session_id,
        "turn_id": turn_id,
        "role": role,
        "turn_index": turn_index,
        "captured_at": captured_at,
        "message_id": message_id,
        "message_revision": message_revision,
    }
    if source_sequence is not None:
        result["source_sequence"] = source_sequence
    if previous_message_revision is not None:
        result["previous_message_revision"] = previous_message_revision
    if previous_message_id is not None:
        result["previous_message_id"] = previous_message_id
    if source_time is not None:
        result["source_time"] = source_time
    if final is not None:
        result["final"] = final
    return result


def _invalidate_revised_message_unlocked(
    processed: dict,
    *,
    source: str,
    session_id: str,
    turn_key_value: str,
    turn_index: int,
    message_id: str,
    message_revision: str,
) -> None:
    """Fence old work and schedule committed targets for source coordination."""

    events = processed.get("events")
    if not isinstance(events, Mapping):
        return
    prior = [
        entry
        for entry in events.values()
        if isinstance(entry, Mapping)
        and entry.get("source") == source
        and entry.get("session_id") == session_id
        and entry.get("message_id") == message_id
        and entry.get("message_revision") != message_revision
    ]
    if not prior:
        return
    prior_turn_keys = {
        entry.get("turn_key")
        for entry in prior
        if isinstance(entry.get("turn_key"), str)
    }
    sessions = processed.setdefault("sessions", {})
    state_key = _session_key(source, session_id)
    state = sessions.get(state_key)
    if not isinstance(state, dict):
        state = {}
    target_ids: set[str] = set()
    entries = state.get("processed_turns")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, Mapping) or entry.get("turn_key") not in prior_turn_keys:
                continue
            values = entry.get("memory_ids")
            if isinstance(values, list):
                target_ids.update(value for value in values if isinstance(value, str) and value)
    revised = state.get("revised_turns")
    if not isinstance(revised, list):
        revised = []
    marker = next(
        (
            item
            for item in revised
            if isinstance(item, dict) and item.get("turn_key") == turn_key_value
        ),
        None,
    )
    if marker is None:
        marker = {"turn_key": turn_key_value, "turn_index": turn_index}
        revised.append(marker)
    marker["message_id"] = message_id
    marker["message_revision"] = message_revision
    marker["memory_ids"] = sorted(
        set(marker.get("memory_ids", [])) | target_ids
    )
    state["revised_turns"] = revised[-64:]

    affected_turn_keys = set(prior_turn_keys) | {turn_key_value}
    processing = state.get("processing")
    if isinstance(processing, Mapping) and affected_turn_keys.intersection(
        value for value in processing.get("turn_keys", []) if isinstance(value, str)
    ):
        # The commit boundary checks this token. Replacing the marker fences a
        # provider result produced from an older source revision.
        state["processing"] = {"status": "idle", "reason": "source_revision_changed"}
    sessions[state_key] = state

    plans = processed.get("pending_turn_plans")
    if isinstance(plans, dict):
        for key in affected_turn_keys:
            plans.pop(turn_identity_key(source, session_id, key), None)
    operations = processed.get("pending_operations")
    if isinstance(operations, dict):
        for operation_id, operation in list(operations.items()):
            if isinstance(operation, Mapping) and (
                operation.get("source") == source
                and operation.get("session_id") == session_id
                and operation.get("turn_key") in affected_turn_keys
            ):
                operations.pop(operation_id, None)


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
    source_time: Optional[str] = None,
    source_sequence: Optional[int] = None,
    message_id: Optional[str] = None,
    message_revision: Optional[str] = None,
    previous_message_revision: Optional[str] = None,
    previous_message_id: Optional[str] = None,
    final: Optional[bool] = None,
) -> CaptureResult:
    """Capture one visible event; all persisted text is redacted first."""

    source = safe_component(source, "source")
    session_id = safe_component(session_id, "session id")
    if not isinstance(turn_id, str) or not turn_id or "\x00" in turn_id or "\n" in turn_id or "\r" in turn_id:
        raise ValueError("invalid turn id")
    role = safe_component(str(role), "role")
    if not isinstance(content, str):
        raise TypeError("captured content must be text")
    message_id = _optional_identifier(message_id, "message id")
    message_revision = _optional_identifier(message_revision, "message revision")
    previous_message_revision = _optional_identifier(
        previous_message_revision, "previous message revision"
    )
    previous_message_id = _optional_identifier(previous_message_id, "previous message id")
    source_time = _source_timestamp(source_time)
    if source_sequence is not None and (type(source_sequence) is not int or source_sequence < 0):
        raise ValueError("invalid source sequence")
    if final is not None and not isinstance(final, bool):
        raise ValueError("invalid final flag")
    resolved_event_id, resolved_message_id, resolved_event_key = _event_identity(
        source, session_id, turn_id, role, event_id, message_id, message_revision
    )
    resolved_message_revision = message_revision or "1"
    resolved_turn_key = turn_key(turn_id)
    persisted_turn_id = _safe_turn_id(turn_id)
    safe_content = escape_event_markers(redact_text(content))
    if not visible or role not in ("user", "assistant"):
        return CaptureResult(resolved_event_id, stored=False, duplicate=False, content=safe_content)

    with vault.lock():
        processed = _read_processed(vault.processed_state_path)
        from .recording_policy import apply_control
        allowed, changed = apply_control(processed, source=source, session_id=session_id,
            turn_key=resolved_turn_key, event_key=resolved_event_key, role=role, content=content, record=record)
        if not allowed:
            if changed:
                atomic_write_json(vault.processed_state_path, processed)
            return CaptureResult(resolved_event_id, stored=False, duplicate=False, suppressed=True)
        # Only permitted data reaches normalization or any persistence path.
        from .evidence_policy import retain_tool_evidence
        safe_tool_evidence = retain_tool_evidence(tool_evidence, vault.config())
        if changed:
            atomic_write_json(vault.processed_state_path, processed)
        known_keys = _known_event_keys(vault, processed)
        known_event = resolved_event_key in known_keys

        path = vault.session_path(source, session_id)
        if path.is_symlink():
            raise ValueError("unsafe inbox session path")
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
        except (OSError, UnicodeError) as error:
            raise ValueError("cannot read inbox session") from error
        existing_event = next(
            (
                metadata
                for metadata in extract_event_metadata(existing)
                if metadata.get("event_key") == resolved_event_key
            ),
            None,
        )
        if existing_event is not None:
            if (
                existing_event.get("role") != role
                or existing_event.get("turn_key") != resolved_turn_key
                or existing_event.get("message_id") not in {None, resolved_message_id}
                or existing_event.get("message_revision") not in {None, resolved_message_revision}
                or existing_event.get("content") != safe_content.rstrip("\n")
            ):
                raise ValueError("event revision identity reused with different payload")
            return CaptureResult(resolved_event_id, stored=False, duplicate=True, path=path, content=safe_content)
        if known_event:
            # The inbox block may already have crossed its cleanup boundary;
            # the durable receipt still proves this delivery was accepted.
            return CaptureResult(
                resolved_event_id,
                stored=False,
                duplicate=True,
                path=path if path.exists() else None,
                content=safe_content,
            )

        prior_revisions = [
            metadata.get("message_revision")
            for metadata in extract_event_metadata(existing)
            if metadata.get("source") == source
            and metadata.get("session_id") == session_id
            and metadata.get("message_id") == resolved_message_id
            and isinstance(metadata.get("message_revision"), str)
        ]
        current_revision = prior_revisions[-1] if prior_revisions else None
        if current_revision is None and previous_message_revision is not None:
            raise ValueError("message revision predecessor does not exist")
        if current_revision is not None and current_revision != resolved_message_revision:
            if previous_message_revision != current_revision:
                raise ValueError("message revision predecessor mismatch")
        elif current_revision is not None and previous_message_revision not in {None, current_revision}:
            raise ValueError("message revision predecessor mismatch")

        session_key = _session_key(source, session_id)
        turn_index, state = _turn_index(
            existing,
            processed,
            session_key,
            turn_id,
            persisted_turn_id,
        )
        _invalidate_revised_message_unlocked(
            processed,
            source=source,
            session_id=session_id,
            turn_key_value=resolved_turn_key,
            turn_index=turn_index,
            message_id=resolved_message_id,
            message_revision=resolved_message_revision,
        )
        captured_at = _timestamp()
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
            message_id=resolved_message_id,
            message_revision=resolved_message_revision,
            previous_message_revision=previous_message_revision,
            source_sequence=source_sequence,
            previous_message_id=previous_message_id,
            source_time=source_time,
            captured_at=captured_at,
            final=final,
            tool_evidence=safe_tool_evidence,
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
        entry = _safe_event_entry(
            source=source,
            session_id=session_id,
            turn_id=persisted_turn_id,
            role=role,
            turn_index=turn_index,
            captured_at=captured_at,
            message_id=resolved_message_id,
            message_revision=resolved_message_revision,
            previous_message_revision=previous_message_revision,
            source_sequence=source_sequence,
            previous_message_id=previous_message_id,
            source_time=source_time,
            final=final,
        )
        entry["turn_key"] = resolved_turn_key
        entry["event_key"] = resolved_event_key
        event_entries[resolved_event_key] = entry
        sessions = processed.get("sessions", {})
        if not isinstance(sessions, dict):
            sessions = {}
        # Merge only capture-owned turn-index fields. In particular, preserve
        # revision coordination and a processor's in-flight ownership marker.
        old_state = sessions.get(session_key)
        new_state = dict(old_state) if isinstance(old_state, dict) else {}
        new_state["turns"] = dict(state.get("turns", {}))
        if "next_turn_index" in state:
            new_state["next_turn_index"] = state["next_turn_index"]
        sessions[session_key] = new_state
        atomic_write_json(
            vault.processed_state_path,
            {
                **processed,
                "version": 1,
                "event_keys": sorted(event_keys),
                "events": event_entries,
                "sessions": sessions,
            },
        )
        return CaptureResult(resolved_event_id, stored=True, duplicate=False, path=path, content=safe_content)
