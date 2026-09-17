"""Typed standalone user input for the existing selected-retention runner.

The marker contains only control identity; body lives in the existing inbox.
Normal conversations and their source-digest formats remain unchanged.
"""
from __future__ import annotations

import re
from typing import Any, Mapping


def validate_origin(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    from .vault import safe_component
    fields = {"version", "session_id", "turn_key", "intent_hash", "request_hash", "scope_hash"}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value["version"]) is not int or value["version"] != 1):
        raise ValueError("invalid_explicit_text_origin")
    safe_component(value["session_id"], "origin session id")
    if any(not isinstance(value[k], str) or not re.fullmatch(r"[0-9a-f]{64}", value[k])
           for k in ("turn_key", "intent_hash", "request_hash", "scope_hash")):
        raise ValueError("invalid_explicit_text_origin")
    return dict(value)


def explicit_turn(turn: Any) -> bool:
    """Only a single, typed user input is independently complete for retention."""
    marked = [e for e in turn.events if getattr(e, "explicit_input", None) is not None]
    if not marked:
        return False
    if len(turn.events) != 1 or turn.events[0].role != "user" or not turn.processable:
        raise ValueError("invalid_explicit_text_turn")
    validate_origin(marked[0].explicit_input)
    return True


def check_origin(service: Any, turn: Any, processed: Mapping[str, Any] | None = None) -> None:
    if not explicit_turn(turn):
        return
    from .process_common import _read_processed
    from .recording_policy import recording_allowed
    origin = turn.events[0].explicit_input
    if processed is None:
        processed = _read_processed(service.vault.processed_state_path)
    if not recording_allowed(processed, turn.source, origin["session_id"], origin["turn_key"]):
        raise ValueError("source_recording_revoked")
