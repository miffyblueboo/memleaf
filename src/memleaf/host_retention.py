"""Durable retention intent bound to a host turn, never model-written facts."""
from __future__ import annotations

from .incremental_journal import digest
from .incremental_selection import explicit_run_id, bind_selection
from .incremental_run_state import load_run, public_result
from .process_common import _read_processed
from .locking import atomic_write_json
from .retrieval_gate import validate_turn, validate_current_turn

KEY = "host_retention_intents"
MAX_PENDING = 128
INTENT_PREFIX = "host-retain-"


def pending_intents(processed):
    pending = processed.get(KEY, {})
    if not isinstance(pending, dict) or len(pending) > MAX_PENDING:
        raise ValueError("invalid_host_retention_intents")
    for identity, row in pending.items():
        if (not isinstance(row, dict) or set(row) != {"source", "session_id", "turn_id", "intent_id"}
                or row["source"] != "hermes"
                or any(not isinstance(row[k], str) or not row[k] for k in ("session_id", "turn_id"))
                or identity != digest([row["source"], row["session_id"], row["turn_id"]])
                or row["intent_id"] != "host-retain-" + identity):
            raise ValueError("invalid_host_retention_intents")
    return pending


def _bound_intent(processed, source, session_id, *, turn_id=None, captured_key=None):
    """Follow only declared ancestry for the SAME turn, never a latest session."""
    from .index import turn_key
    from .host_turn_identity import ancestors, continuation_path
    lineage = ancestors(processed, source, session_id)
    key = captured_key if captured_key is not None else turn_key(turn_id)
    matches = [(identity, row) for identity, row in pending_intents(processed).items()
               if row["source"] == source and row["session_id"] in lineage
               and turn_key(row["turn_id"]) == key]
    if len(matches) > 1:
        raise ValueError("ambiguous_host_retention_intent")
    if not matches:
        return None
    identity, row = matches[0]
    # A fork is not proof that this branch owns the original queued request.
    continuation_path(processed, source, session_id, row["session_id"])
    return identity, row


def check_run_authority(processed, run):
    """Recheck the whole frozen authority path before dispatch and commit."""
    origin = run.get("host_retention_origin")
    if origin is None:
        return
    from .host_turn_identity import continuation_path
    from .recording_policy import recording_allowed
    path = continuation_path(processed, run["source"], run["session_id"], origin["session_id"])
    if path is None or any(not recording_allowed(processed, run["source"], session, run["turn_key"])
                           for session in path):
        raise ValueError("source_recording_revoked")


def validate_origin(origin, *, source, turn_key, intent_id):
    if origin is None:
        return
    from .index import turn_key as key
    if not isinstance(origin, dict):
        raise ValueError("invalid_host_retention_origin")
    pending_intents({KEY: {digest([origin.get(k) for k in ("source", "session_id", "turn_id")]): origin}})
    if (origin["source"] != source or origin["intent_id"] != intent_id or key(origin["turn_id"]) != turn_key):
        raise ValueError("invalid_host_retention_origin")


def admit_unlocked(processed, *, source, session_id, turn_key, intent_id, run_id):
    """Bind a reserved host authorization in the SAME lock as run creation."""
    if not intent_id.startswith(INTENT_PREFIX):
        return None  # Existing caller-defined explicit intent namespaces remain scoped.
    from .incremental_run_state import KEY as RUNS
    for identity in processed.get(RUNS, {}):
        prior = load_run(processed, identity)
        if prior.get("authorization_intent") == intent_id and identity != run_id:
            raise ValueError("host_retention_source_binding_changed")
    bound = _bound_intent(processed, source, session_id, captured_key=turn_key)
    if bound is None or bound[1]["intent_id"] != intent_id:
        raise ValueError("host_retention_intent_not_pending")
    origin = dict(bound[1])
    check_run_authority(processed, {"host_retention_origin": origin, "source": source,
                                   "session_id": session_id, "turn_key": turn_key})
    return origin


def remember_turn(service, *, phase, retrieval_id=None, source=None, session_id=None, turn_id=None, model=None, router=None):
    if phase == "queue":
        state = validate_turn(service.vault, retrieval_id)
        source, session_id, turn_id = (state.get(k) for k in ("source", "session_id", "turn_id"))
        if source != "hermes":
            raise ValueError("host_retention_identity_required")
        validate_current_turn(service.vault, retrieval_id, source)
    elif phase != "complete" or source != "hermes" or not all(isinstance(v, str) and v for v in (session_id, turn_id)):
        raise ValueError("invalid_host_retention_phase")
    identity = digest([source, session_id, turn_id])
    intent = "host-retain-" + identity
    with service.vault.lock():
        processed = _read_processed(service.vault.processed_state_path)
        pending = pending_intents(processed)
        bound = _bound_intent(processed, source, session_id, turn_id=turn_id)
        if bound is not None:
            identity, expected = bound
            intent = expected["intent_id"]
        from .index import turn_key
        from .recording_policy import recording_allowed
        if (not recording_allowed(processed, source, session_id, turn_key(turn_id))
                or bound is not None and not recording_allowed(processed, source, expected["session_id"], turn_key(turn_id))):
            raise ValueError("source_recording_revoked")
        if bound is not None:
            check_run_authority(processed, {"host_retention_origin": expected, "source": source,
                                           "session_id": session_id, "turn_key": turn_key(turn_id)})
        if phase == "queue":
            if identity not in pending and len(pending) >= MAX_PENDING:
                raise ValueError("host_retention_intents_full")
            if bound is None:
                pending[identity] = {"source": source, "session_id": session_id, "turn_id": turn_id, "intent_id": intent}
            processed[KEY] = pending
            atomic_write_json(service.vault.processed_state_path, processed)
            return {"execution_status": "queued", "saved": False,
                    "guidance": "The actual user messages will be retained after this turn finishes. Do not claim permanent memory was saved yet."}
        if bound is None:
            return {"execution_status": "not_requested", "saved": False, "model_calls": 0}
        # A started intent already owns its source and allowance. Compression
        # cannot turn it into a fresh authorization with a fresh budget.
        from .incremental_run_state import KEY as RUNS
        for run_id in processed.get(RUNS, {}):
            prior = load_run(processed, run_id)
            if (prior.get("authorization_intent") == intent
                    and (prior["source"], prior["session_id"]) != (source, session_id)):
                raise ValueError("host_retention_source_binding_changed")
        from .incremental_commit import _window
        from .index import turn_key
        turn, _ = _window(service, source, session_id, turn_key(turn_id))
        users = [event for event in turn.events if event.role == "user"]
        request = "\n\n".join(event.content for event in users)
        refs = [event.event_key for event in users]
        selection, request = bind_selection(intent, refs, request)
        run = load_run(processed, explicit_run_id(source, session_id, selection))
    if run is not None and run["status"] in {"completed", "completed_with_unresolved", "blocked", "failed", "cancelled"}:
        result = public_result(run)
    else:
        result = service.remember_incremental(source=source, session_id=session_id, turn_id=turn_id,
            intent_id=intent, selected_source_refs=refs, retention_request=request, model=model, router=router)
    if result["execution_status"] == "completed":
        with service.vault.lock():
            processed = _read_processed(service.vault.processed_state_path)
            if processed.get(KEY, {}).get(identity) == expected:
                processed[KEY].pop(identity)
                atomic_write_json(service.vault.processed_state_path, processed)
    return result


def complete_for_captured_turn(service, turn, *, model=None, router=None):
    """Resume durable queued intent after host restart, without borrowing a newer turn."""
    with service.vault.lock():
        processed = _read_processed(service.vault.processed_state_path)
        bound = _bound_intent(processed, turn.source, turn.session_id, captured_key=turn.turn_key)
    if bound is None:
        return None
    _, row = bound
    return remember_turn(service, phase="complete", source=turn.source, session_id=turn.session_id,
                         turn_id=row["turn_id"], model=model, router=router)
