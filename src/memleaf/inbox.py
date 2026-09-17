"""Read-only parser for structured inbox events and complete turns.

The parser deliberately treats legacy digest-only v1 markers as
``legacy=True`` and never pairs them.  A v2 block is processable only when it
has an explicit turn key and index; a redacted display turn id is not used as
the grouping identity.  A turn may contain several visible user events
followed by one assistant event.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .index import EVENT_V2_BLOCK, extract_event_metadata


_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def captured_turn_selector(turn_id: str, captured_turn_key: str | None = None) -> str:
    """Resolve either a raw host ID or an explicit, persisted capture key.

    No numeric/name fallback is allowed. For keyed calls the display argument
    must equal the key, so two conflicting identities cannot enter a snapshot.
    This is an internal location selector, not write authorization.
    """
    from .index import turn_key
    if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 800:
        raise ValueError("invalid_turn_id")
    if captured_turn_key is None:
        return turn_key(turn_id)
    if (not isinstance(captured_turn_key, str)
            or not re.fullmatch(r"[0-9a-f]{64}", captured_turn_key)
            or turn_id != captured_turn_key):
        raise ValueError("invalid_captured_turn_key")
    return captured_turn_key


@dataclass(frozen=True)
class InboxEvent:
    source: str
    session_id: str
    turn_key: Optional[str]
    turn_index: Optional[int]
    role: Optional[str]
    event_key: str
    content: str
    turn_id: Optional[str] = None
    message_id: Optional[str] = None
    message_revision: Optional[str] = None
    previous_message_revision: Optional[str] = None
    source_sequence: Optional[int] = None
    previous_message_id: Optional[str] = None
    source_time: Optional[str] = None
    captured_at: Optional[str] = None
    final: Optional[bool] = None
    timestamp: Optional[str] = None
    tool_evidence: tuple[dict[str, str], ...] = ()
    legacy: bool = False
    explicit_input: dict[str, Any] | None = None

    @property
    def processable(self) -> bool:
        return (
            not self.legacy
            and isinstance(self.turn_key, str)
            and bool(_HEX64.fullmatch(self.turn_key))
            and isinstance(self.turn_index, int)
            and not isinstance(self.turn_index, bool)
            and self.turn_index > 0
            and self.role in ("user", "assistant")
        )


@dataclass(frozen=True)
class InboxTurn:
    source: str
    session_id: str
    turn_key: Optional[str]
    turn_index: Optional[int]
    events: tuple[InboxEvent, ...]
    legacy: bool = False

    @property
    def complete(self) -> bool:
        if not self.processable or len(self.events) < 2:
            return False
        roles = [event.role for event in self.events]
        if roles.count("user") < 1 or roles.count("assistant") < 1:
            return False
        assistants = [event for event in self.events if event.role == "assistant"]
        declared = [event for event in assistants if event.final is not None]
        if declared:
            finals = [event for event in assistants if event.final is True]
            # A trusted adapter may persist intermediate visible assistant
            # messages. Exactly one declared final, ordered after every other
            # event, is the processable turn boundary.
            return len(finals) == 1 and self.events[-1] is finals[0]
        # Legacy v2 inbox blocks did not carry a final signal. Preserve their
        # old one-assistant compatibility path without treating a capture-time
        # timestamp as proof of source time.
        return len(assistants) == 1

    @property
    def is_complete(self) -> bool:
        return self.complete

    @property
    def processable(self) -> bool:
        structurally_processable = (
            not self.legacy
            and bool(self.turn_key)
            and isinstance(self.turn_index, int)
            and all(event.processable for event in self.events)
        )
        if not structurally_processable:
            return False
        sequences = [event.source_sequence for event in self.events]
        if any(sequence is not None for sequence in sequences):
            return (
                all(type(sequence) is int for sequence in sequences)
                and len(sequences) == len(set(sequences))
            )
        return True

    @property
    def event_keys(self) -> tuple[str, ...]:
        return tuple(event.event_key for event in self.events)


def source_ordered_turns(turns: Iterable[InboxTurn]) -> list[InboxTurn]:
    """Order one stream only when all source positions prove a total order.

    Local turn indices remain capture identities. Unknown, overlapping or
    mixed-stream positions retain caller order; no wall clock or text inference
    is used to invent causality. This orders the currently available window,
    not messages which have not arrived yet.
    """
    values = list(turns)
    if not values or len({(turn.source, turn.session_id) for turn in values}) != 1:
        return values
    bounds = []
    for turn in values:
        positions = [event.source_sequence for event in turn.events]
        if not positions or any(type(position) is not int for position in positions):
            return values
        if len(set(positions)) != len(positions):
            return values
        bounds.append((min(positions), max(positions), turn))
    bounds.sort(key=lambda item: item[0])
    if any(left[1] >= right[0] for left, right in zip(bounds, bounds[1:])):
        return values
    return [item[2] for item in bounds]


def _fallback_component(value: Optional[str], default: str) -> str:
    return value if isinstance(value, str) and value else default


def _event_metadata(text: str) -> list[dict[str, Any]]:
    """Read every structurally valid v2 block, including non-processable roles."""

    result: list[dict[str, Any]] = []
    for match in EVENT_V2_BLOCK.finditer(text):
        try:
            metadata = json.loads(match.group("meta"))
        except (TypeError, ValueError):
            continue
        if not isinstance(metadata, dict):
            continue
        metadata = dict(metadata)
        metadata["content"] = match.group("content")
        result.append(metadata)
    result.extend(item for item in extract_event_metadata(text) if item.get("legacy"))
    return result



def _bounded_tool_evidence(value: Any) -> tuple[dict[str, str], ...]:
    from .provenance import read_tool_evidence
    return read_tool_evidence(value)

def parse_inbox_text(
    text: str,
    *,
    source: Optional[str] = None,
    session_id: Optional[str] = None,
) -> list[InboxTurn]:
    """Parse one inbox Markdown string into ordered turns."""

    if not isinstance(text, str):
        raise TypeError("inbox text must be text")
    parsed_events: list[InboxEvent] = []
    seen: set[tuple[str, str, str]] = set()
    fallback_source = _fallback_component(source, "unknown")
    fallback_session = _fallback_component(session_id, "unknown")
    for metadata in _event_metadata(text):
        key = metadata.get("event_key")
        if not isinstance(key, str) or not _HEX64.fullmatch(key):
            continue
        event_source = _fallback_component(metadata.get("source"), fallback_source)
        event_session = _fallback_component(metadata.get("session_id"), fallback_session)
        dedupe_key = (event_source, event_session, key.casefold())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        if metadata.get("legacy"):
            parsed_events.append(
                InboxEvent(
                    source=event_source,
                    session_id=event_session,
                    turn_key=None,
                    turn_index=None,
                    role=None,
                    event_key=key.casefold(),
                    content="",
                    legacy=True,
                )
            )
            continue

        role = metadata.get("role")
        display_turn_id = metadata.get("turn_id")
        stable_turn_key = metadata.get("turn_key")
        turn_index = metadata.get("turn_index")
        message_id = metadata.get("message_id")
        message_revision = metadata.get("message_revision")
        previous_message_revision = metadata.get("previous_message_revision")
        source_sequence = metadata.get("source_sequence")
        previous_message_id = metadata.get("previous_message_id")
        source_time = metadata.get("source_time")
        captured_at = metadata.get("captured_at")
        final = metadata.get("final")
        from .explicit_text_source import validate_origin
        explicit_input = validate_origin(metadata.get("explicit_input"))
        valid_v2 = (
            role in ("user", "assistant")
            and isinstance(display_turn_id, str)
            and isinstance(stable_turn_key, str)
            and bool(_HEX64.fullmatch(stable_turn_key))
            and isinstance(turn_index, int)
            and not isinstance(turn_index, bool)
            and turn_index > 0
        )
        groupable = (
            isinstance(stable_turn_key, str)
            and bool(_HEX64.fullmatch(stable_turn_key))
            and isinstance(turn_index, int)
            and not isinstance(turn_index, bool)
            and turn_index > 0
        )
        if not valid_v2:
            # A malformed/older v2-shaped block is visible for diagnostics but
            # cannot be made processable by guessing a grouping key.
            parsed_events.append(
                InboxEvent(
                    source=event_source,
                    session_id=event_session,
                    turn_key=stable_turn_key.casefold() if groupable else None,
                    turn_index=turn_index if groupable else None,
                    role=role if role in ("user", "assistant") else None,
                    event_key=key.casefold(),
                    content=str(metadata.get("content", "")),
                    turn_id=display_turn_id if isinstance(display_turn_id, str) else None,
                    message_id=message_id if isinstance(message_id, str) and message_id else None,
                    message_revision=message_revision if isinstance(message_revision, str) and message_revision else None,
                    previous_message_revision=previous_message_revision if isinstance(previous_message_revision, str) and previous_message_revision else None,
                    source_sequence=source_sequence if type(source_sequence) is int and source_sequence >= 0 else None,
                    previous_message_id=previous_message_id if isinstance(previous_message_id, str) and previous_message_id else None,
                    source_time=source_time if isinstance(source_time, str) and source_time else None,
                    captured_at=captured_at if isinstance(captured_at, str) and captured_at else (
                        metadata.get("timestamp") if isinstance(metadata.get("timestamp"), str) else None
                    ),
                    final=final if isinstance(final, bool) else None,
                    timestamp=source_time if isinstance(source_time, str) and source_time else None,
                    tool_evidence=_bounded_tool_evidence(metadata.get("tool_evidence")),
                    explicit_input=explicit_input,
                    legacy=not groupable,
                )
            )
            continue
        parsed_events.append(
            InboxEvent(
                source=event_source,
                session_id=event_session,
                turn_key=stable_turn_key.casefold(),
                turn_index=turn_index,
                role=role,
                event_key=key.casefold(),
                content=str(metadata.get("content", "")),
                turn_id=display_turn_id,
                message_id=message_id if isinstance(message_id, str) and message_id else None,
                message_revision=message_revision if isinstance(message_revision, str) and message_revision else None,
                previous_message_revision=previous_message_revision if isinstance(previous_message_revision, str) and previous_message_revision else None,
                source_sequence=source_sequence if type(source_sequence) is int and source_sequence >= 0 else None,
                previous_message_id=previous_message_id if isinstance(previous_message_id, str) and previous_message_id else None,
                source_time=source_time if isinstance(source_time, str) and source_time else None,
                captured_at=captured_at if isinstance(captured_at, str) and captured_at else (
                    metadata.get("timestamp") if isinstance(metadata.get("timestamp"), str) else None
                ),
                final=final if isinstance(final, bool) else None,
                timestamp=source_time if isinstance(source_time, str) and source_time else None,
                tool_evidence=_bounded_tool_evidence(metadata.get("tool_evidence")),
                explicit_input=explicit_input,
            )
        )

    # A host revision replaces the older visible version of the same stable
    # message. File order is the durable arrival order; revisions are opaque
    # host identifiers and therefore are never compared lexically.
    latest_message_index: dict[tuple[str, str, str], int] = {}
    first_message_index: dict[tuple[str, str, str], int] = {}
    for index, event in enumerate(parsed_events):
        if event.message_id:
            identity = (event.source, event.session_id, event.message_id)
            first_message_index.setdefault(identity, index)
            latest_message_index[identity] = index
    parsed_events = [
        event
        for index, event in enumerate(parsed_events)
        if not event.message_id
        or latest_message_index[(event.source, event.session_id, event.message_id)] == index
        # Invalidate a reply only if an already-observed predecessor is later
        # revised. The first arrival of a predecessor can follow its reply
        # during out-of-order delivery; it is not evidence of a revision.
        if not event.previous_message_id
        or latest_message_index.get(
            (event.source, event.session_id, event.previous_message_id), -1
        ) < index
        or first_message_index.get(
            (event.source, event.session_id, event.previous_message_id), -1
        ) > index
    ]

    groups: dict[tuple[str, str, str, int], list[InboxEvent]] = {}
    result: list[InboxTurn] = []
    for event in parsed_events:
        if not isinstance(event.turn_key, str) or not isinstance(event.turn_index, int):
            result.append(
                InboxTurn(
                    source=event.source,
                    session_id=event.session_id,
                    turn_key=None,
                    turn_index=None,
                    events=(event,),
                    legacy=True,
                )
            )
            continue
        group_key = (event.source, event.session_id, event.turn_key, event.turn_index)
        groups.setdefault(group_key, []).append(event)

    grouped = sorted(groups.items(), key=lambda item: (item[0][3], item[0][0], item[0][1], item[0][2]))
    for (group_source, group_session, group_key, group_index), events in grouped:
        if events and all(type(event.source_sequence) is int for event in events):
            events.sort(key=lambda event: event.source_sequence)  # type: ignore[arg-type]
        result.append(
            InboxTurn(
                source=group_source,
                session_id=group_session,
                turn_key=group_key,
                turn_index=group_index,
                events=tuple(events),
            )
        )
    return source_ordered_turns(result)


def parse_inbox_file(path: Path | str) -> list[InboxTurn]:
    file_path = Path(path)
    if file_path.is_symlink() or not file_path.is_file():
        return []
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    return parse_inbox_text(text, source=file_path.parent.name, session_id=file_path.stem)


def parse_inbox(vault_or_path: Any) -> list[InboxTurn]:
    """Parse all inbox Markdown files from a Vault or inbox directory."""

    if hasattr(vault_or_path, "list_markdown"):
        paths = vault_or_path.list_markdown("inbox")
    else:
        root = Path(vault_or_path)
        if root.is_file():
            return parse_inbox_file(root)
        inbox_root = root / "inbox" if (root / "inbox").is_dir() else root
        paths = sorted(inbox_root.rglob("*.md")) if inbox_root.exists() else []
    turns: list[InboxTurn] = []
    for path in paths:
        turns.extend(parse_inbox_file(path))
    return turns


def complete_turns(vault_or_path: Any) -> list[InboxTurn]:
    return [turn for turn in parse_inbox(vault_or_path) if turn.complete]


parse_turns = parse_inbox
parse_complete_turns = complete_turns


class InboxParser:
    """Small object wrapper for callers that prefer an injectable parser."""

    def parse_text(self, text: str, *, source: Optional[str] = None, session_id: Optional[str] = None) -> list[InboxTurn]:
        return parse_inbox_text(text, source=source, session_id=session_id)

    def parse_file(self, path: Path | str) -> list[InboxTurn]:
        return parse_inbox_file(path)

    def parse(self, vault_or_path: Any) -> list[InboxTurn]:
        return parse_inbox(vault_or_path)


__all__ = [
    "InboxEvent",
    "InboxParser",
    "InboxTurn",
    "complete_turns",
    "parse_complete_turns",
    "parse_inbox",
    "parse_inbox_file",
    "parse_inbox_text",
    "parse_turns",
]
