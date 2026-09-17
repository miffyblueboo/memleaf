"""Adapt the existing text remember API to the single incremental runner.

One standalone user input is stored in the existing inbox, never as an invented
user/assistant conversation. The immutable marker and event receipt are the
binding; model work, budget, operations and recovery remain canonical.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .capture import capture_event, _source_timestamp
from .incremental_journal import digest, load_work
from .incremental_run_state import load_run
from .incremental_selection import bind_selection, explicit_run_id
from .index import event_key, turn_key
from .process_common import _read_processed
from .recording_policy import recording_allowed
from .redaction import redact_text
from .scope_state import normalize_scopes
from .turn_plan import turn_identity_key
from .vault import safe_component

_REQUEST = "Remember the submitted content."
MAX_TEXT_BYTES = 64 * 1024


def _identifier(value: Any, name: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not value or len(value) > 800
                              or any(c in value for c in "\0\r\n")):
        raise ValueError("invalid_remember_" + name)
    return value


def _identity(source, session_id, turn_id, event_id, intent_id):
    """Preserve legacy event/intent identity to detect cross-pipeline retries."""
    turn_id = _identifier(turn_id, "turn_id")
    event_id = _identifier(event_id, "event_id")
    intent_id = _identifier(intent_id, "intent_id")
    if intent_id is None:
        receipt = event_id or turn_id
        intent_id = ("receipt-" + hashlib.sha256(receipt.encode()).hexdigest()
                     if receipt else "intent-" + uuid.uuid4().hex)
    if not intent_id.strip():
        raise ValueError("invalid_remember_intent_id")
    raw_turn = turn_id or "remember-" + hashlib.sha256(intent_id.encode()).hexdigest()[:16]
    raw_event = event_id or f"remember/{source}/{session_id}/{intent_id}"
    event_preimage = json.dumps([source, session_id, raw_event, intent_id]) if event_id is not None else raw_event
    legacy_turn = ("remember-" + hashlib.sha256(json.dumps([raw_turn, intent_id]).encode()).hexdigest()
                   if turn_id is not None else raw_turn)
    return intent_id, raw_turn, legacy_turn, event_preimage


def _applied(processed, run):
    if run is None:
        return set()
    work = load_work(processed, run["commit_work_id"])
    return {op["operation_id"] for op in work["operations"]
            if op["action"] in {"CREATE", "UPDATE"} and op["state"] in {"applied", "settled"}} if work else set()


def remember_text(service: Any, *, content: str | None = None, text: str | None = None,
                  source: str = "memleaf", session_id: str = "remember", turn_id: str | None = None,
                  event_id: str | None = None, intent_id: str | None = None, scopes: Any = None,
                  model: Any = None, router: Any = None, recover: bool = False,
                  source_time: str | None = None) -> dict[str, Any]:
    if content is not None and text is not None and content != text:
        raise ValueError("ambiguous_remember_content")
    value = content if content is not None else text
    if (not isinstance(value, str) or not value.strip() or "\0" in value
            or len(value.encode("utf-8")) > MAX_TEXT_BYTES):
        raise ValueError("invalid_remember_content")
    if type(recover) is not bool or (model is not None and router is not None):
        raise ValueError("invalid_remember_options")
    if recover and intent_id is None and event_id is None and turn_id is None:
        raise ValueError("remember_recovery_requires_identity")
    source, session_id = safe_component(source, "source"), safe_component(session_id, "session id")
    source_time = _source_timestamp(source_time)  # Missing original time stays unknown.
    scope = normalize_scopes(scopes) if scopes is not None else None
    intent, raw_turn, legacy_turn, event_preimage = _identity(source, session_id, turn_id, event_id, intent_id)
    key = event_key(event_preimage)
    # Source identity remains the originating Agent; isolation is per explicit
    # request, not a permanent-memory visibility namespace or fake host session.
    submitted_session = "remember-text-" + digest([source, session_id, intent])
    submitted_turn = "submitted-text"
    selection, request = bind_selection(intent, [key], _REQUEST)
    rid = explicit_run_id(source, submitted_session, selection)
    safe_value = redact_text(value).rstrip("\n")
    origin = {"version": 1, "session_id": session_id, "turn_key": turn_key(raw_turn),
              "intent_hash": digest(intent), "scope_hash": digest(scope),
              "request_hash": digest([safe_value, source_time, scope, raw_turn, event_id])}
    with service.vault.lock():
        processed = _read_processed(service.vault.processed_state_path)
        run = load_run(processed, rid)
        receipt = processed.get("events", {}).get(key)
        if receipt is not None and (not isinstance(receipt, dict) or receipt.get("explicit_input") != origin):
            raise ValueError("remember_binding_or_pipeline_changed")
        if run is not None and receipt is None:
            raise ValueError("remember_source_binding_unavailable")
        if turn_identity_key(source, session_id, turn_key(legacy_turn)) in processed.get("pending_turn_plans", {}):
            raise ValueError("legacy_remember_requires_resume")
        if not recording_allowed(processed, source, session_id, origin["turn_key"]):
            raise ValueError("source_recording_revoked")
        before = _applied(processed, run)
        was_completed = run is not None and run["status"] == "completed"
    if run is None:
        # All marker fields are present in the first atomic inbox write, so
        # a crash before receipt saving cannot expose this to automatic work.
        result = capture_event(service.vault, source=source, session_id=submitted_session,
                               turn_id=submitted_turn, role="user", content=value,
                               event_id=event_preimage, source_time=source_time,
                               _explicit_input=origin)
        if result.suppressed:
            return {"pipeline": "incremental", "execution_status": "suppressed",
                    "code": "source_recording_revoked", "intent_id": intent,
                    "memory_ids": [], "memories_written": 0, "processed_turns": 0,
                    "model_calls_this_invocation": 0, "retry_available": False}
    from .incremental_runtime import process_incremental
    result = process_incremental(service, source=source, session_id=submitted_session,
                                 turn_id=submitted_turn, scope=scope, model=model, router=router,
                                 recover=recover, selection=selection, retention_request=request)
    with service.vault.lock():
        processed = _read_processed(service.vault.processed_state_path)
        run = load_run(processed, rid)
        after = _applied(processed, run)
    commit = result.get("commit") or {}
    ids = sorted({op["memory_id"] for op in commit.get("operations", [])
                  if op.get("memory_id") and not op.get("native") and op["state"] in {"applied", "settled"}})
    return {**result, "pipeline": "incremental", "intent_id": intent, "memory_ids": ids,
            "memories_written": len(after - before), "metadata_merged": 0,
            "processed_turns": int(result["execution_status"] == "completed" and not was_completed),
            "cleaned_turns": 0,
            "compaction": {"status": "not_run", "reason": "outside_extraction_critical_path"}}
