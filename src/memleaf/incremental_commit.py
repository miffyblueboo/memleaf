"""Explicit G3b apply/recovery bridge, not the default model pipeline.

Source, authorization and target groups are frozen before using the existing
MemoryWriter. Each target is independently recoverable; no model is called.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any
import uuid

from .incremental_journal import (VERSION, TERMINAL, digest, load_work, save_work, public_result, strip_payload)
from .incremental_preview import _prepare_incremental_unlocked
from .incremental_protocol import compile_incremental
from .incremental_native import guard_current, validate_binding
from .inbox import parse_inbox_file, source_ordered_turns, captured_turn_selector
from .index import turn_key
from .memory_writer import MemoryWriter
from .models import Memory, MemoryVersionError, utc_now
from .process_common import _read_processed, _add_hours, _as_int
from .process_journal import ProcessJournal
from .recording_policy import recording_allowed
from .source_policy import merge_sources
from .turn_plan import input_digest, revision_digest, dedup_digest, turn_identity_key
from .validation import parse_strict_json
from .vault import safe_component


class IncrementalCommitError(OSError):
    def __init__(self, result: dict[str, Any]):
        super().__init__("incremental commit interrupted; resume the original work_id")
        self.result = result
        self.recovery_required = True


def _window(service: Any, source: str, session_id: str, selected_key: str):
    path = service.vault._inside("inbox", source, f"{session_id}.md")
    turns = source_ordered_turns(parse_inbox_file(path))
    selected = next((t for t in turns if t.turn_key == selected_key), None)
    if selected is None or not selected.complete:
        raise ValueError("source_not_complete")
    relevant = turns[max(0, turns.index(selected) - 1):]
    return selected, digest([(t.turn_key, input_digest(t)) for t in relevant])


def _arguments(source, session_id, turn_id, scope, priority_memory_ids, candidate_limit, allow_new_scopes, selection=None, captured_turn_key=None):
    from .scope_state import normalize_scopes
    from .incremental_selection import validate_selection
    selection = validate_selection(selection)
    captured_turn_selector(turn_id, captured_turn_key)
    source, session_id = safe_component(source, "source"), safe_component(session_id, "session id")
    if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 800:
        raise ValueError("invalid_turn_id")
    if type(candidate_limit) is not int or not 1 <= candidate_limit <= 20:
        raise ValueError("invalid_candidate_limit")
    if type(allow_new_scopes) is not bool:
        raise ValueError("invalid_context_flags")
    if isinstance(priority_memory_ids, (str, bytes)):
        raise ValueError("invalid_priority_ids")
    priority = list(dict.fromkeys(priority_memory_ids))
    for identity in priority:
        safe_component(identity, "memory id")
    return {"source": source, "session_id": session_id, "turn_id": turn_id,
            "scope": normalize_scopes(scope) if scope is not None else None,
            "priority_memory_ids": priority, "candidate_limit": candidate_limit, "allow_new_scopes": allow_new_scopes,
            **({"selection": selection} if selection else {}),
            **({"captured_turn_key": captured_turn_key} if captured_turn_key is not None else {})}



def equivalent_arguments(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare raw-ID and explicitly typed-key addressing of the same source.

    All authorization and context options remain exact. Never rewrite a stored
    request or treat an arbitrary 64-character raw ID as a captured key.
    """
    def normalize(args):
        value = dict(args)
        value["turn_id"] = captured_turn_selector(value["turn_id"], value.pop("captured_turn_key", None))
        return value
    return normalize(left) == normalize(right)


def _freeze_operation(proposal: dict[str, Any], snapshot: Any, now: str, active: list[Memory]) -> dict[str, Any]:
    op = deepcopy(proposal)
    op.pop("warnings", None)
    op.update(operation_id="inc-op-" + uuid.uuid4().hex, prepared_at=now,
              state="deferred" if op["action"] == "DEFERRED" else "prepared")
    if "target" in op:
        op["memory_id"] = op.pop("target")
    if op["action"] not in {"CREATE", "UPDATE"}:
        return op
    state = snapshot.state()
    before = next((Memory.from_mapping(t["memory"]) for t in state["targets"].values()
                   if t["memory"]["memory_id"] == op.get("memory_id")), None)
    value = op.pop("memory")
    if before is None:
        op["memory_id"] = "mem-" + uuid.uuid4().hex
        value.update(memory_id=op["memory_id"], created=now, updated=now)
    else:
        value.update(memory_id=before.memory_id, created=before.created, updated=now)
    after = Memory.from_mapping(value)
    if before is not None and dedup_digest(before.to_dict()) == dedup_digest(after.to_dict()):
        op.update(action="NO_CHANGE", expected_revision=revision_digest(before))
        return op
    if before is None:
        same = [m for m in active if m.validity == "valid" and dedup_digest(m.to_dict()) == dedup_digest(after.to_dict())]
        if len(same) > 1:
            op.update(state="blocked", code="ambiguous_exact_duplicate")
            return op
        if same:
            op.update(action="NO_CHANGE", memory_id=same[0].memory_id, expected_revision=revision_digest(same[0]))
            return op
    provenance = [{k: e[k] for k in ("source", "session_id", "event_key", "message_id", "message_revision", "source_time", "source_sequence")
                   if e.get(k) is not None} for e in state["evidence"] if e["ref"] in op["evidence"]]
    after.sources, source_meta = merge_sources(before.sources if before else [], provenance,
                                               extra=before.extra if before else {})
    after.extra.update(source_meta)
    after.extra["incremental_operation_id"] = op["operation_id"]
    if after.validity == "retracted":
        after.extra["retracted_at"] = now
    elif before is not None and before.validity == "retracted":
        after.extra.pop("retracted_at", None)
        after.extra.pop("retraction_reason", None)
    op.update(before=before.to_markdown() if before else None, after=after.to_markdown(),
              replacement_revision=revision_digest(after))
    from .incremental_scopes import freeze_registration
    try:
        freeze_registration(op, state)
    except ValueError as error:
        op.update(state="blocked", code=str(error))
        strip_payload(op)
        return op
    if before is not None:
        op["expected_revision"] = revision_digest(before)
    else:
        active.append(after)
    return op


def _source_valid(service, processed, work):
    if not recording_allowed(processed, work["source"], work["session_id"], work["turn_key"]):
        return False
    try:
        turn, window = _window(service, work["source"], work["session_id"], work["turn_key"])
    except (OSError, ValueError):
        return False
    return window == work["source_window"] and input_digest(turn) == work["source_digest"]


def _settle_source(service, processed, work, *, source_valid):
    if work.get("request_kind", "automatic") == "explicit_remember":
        # This authorization only selected some material. Its own work receipt
        # settles it; it never consumes the automatic turn or changes cleanup.
        return
    now = utc_now()
    state = processed.setdefault("sessions", {}).setdefault(f'{work["source"]}/{work["session_id"]}', {})
    entries = state.setdefault("processed_turns", [])
    entry = next((e for e in entries if e.get("turn_key") == work["turn_key"]), None)
    if entry is None:
        entry = {"turn_key": work["turn_key"], "turn_index": work["turn_index"], "memory_ids": []}
        entries.append(entry)
    result = public_result(work)
    entry.update(pipeline="incremental-items-v1", processed_at=now)
    entry["incremental_work_ids"] = sorted(set(entry.get("incremental_work_ids", [])) | {work["work_id"]})
    entry["memory_ids"] = sorted(set(entry.get("memory_ids", [])) | {
        op["memory_id"] for op in work["operations"] if op.get("memory_id") and "native" not in op and op["state"] in {"applied", "settled"}})
    keys = [e["event_key"] for e in work["evidence"] if e["use"] == "new"]
    if source_valid:
        entry["cleanup_event_keys"] = sorted(set(entry.get("cleanup_event_keys", [])) | set(entry.get("event_keys", [])) | set(keys))
        entry["event_keys"] = keys
    entry["incremental_coverage"] = result["coverage_status"]
    if source_valid and result["execution_status"] == "completed":
        hours = service.vault.config().get("process", {}).get("inbox_cleanup_hours", 24)
        entry["eligible_cleanup_at"] = _add_hours(now, hours)
        entry.pop("deferred_evidence", None); entry.pop("deferred_candidates", None)
        state["revised_turns"] = [e for e in state.get("revised_turns", []) if e.get("turn_key") != work["turn_key"]]
    else:
        entry["eligible_cleanup_at"] = None
        entry["deferred_evidence"] = [{"decision": "DEFERRED", "reason": "incremental_unresolved"}]
    entry.pop("cleanup_done_at", None)
    indices = {_as_int(e.get("turn_index"), -1) for e in entries}
    watermark = max(_as_int(state.get("watermark"), 0), _as_int(state.get("processed_watermark"), 0))
    while watermark + 1 in indices:
        watermark += 1
    state.update(watermark=watermark, processed_watermark=watermark)


def _resume_unlocked(service, processed, work):
    from .incremental_scopes import registry_view, guard_matches, applied_additions, finish_registration
    writer = MemoryWriter(service)
    source_valid = _source_valid(service, processed, work)
    native_valid = True
    if any(op["state"] not in TERMINAL for op in work["operations"]):
        try:
            native_valid = guard_current(service, work["source"], work.get("native_guard"))
        except ValueError:
            native_valid = False
    try:
        for op in work["operations"]:
            if op["state"] in TERMINAL:
                continue
            if op["state"] == "prepared" and op["action"] in {"CREATE", "UPDATE"} and writer.frozen_state_applied_unlocked(op):
                op["state"] = "applied"
            if op["state"] == "applied":
                finish_registration(service, op)
                op["state"] = "settled"
                strip_payload(op); save_work(service, processed, work)
                continue
            if not source_valid or not native_valid:
                op.update(state="blocked", code="source_or_context_changed" if not source_valid else "native_context_changed")
                strip_payload(op); save_work(service, processed, work)
                continue
            try:
                if work.get("scope_guard") is not None and op["action"] in {"CREATE", "UPDATE", "NO_CHANGE"}:
                    _, current_guard, _ = registry_view(service.vault.config())
                    if not guard_matches(current_guard, work["scope_guard"], applied_additions(work)):
                        raise ValueError("scope_registry_changed")
                if op["action"] in {"CREATE", "UPDATE"}:
                    writer.write_frozen_unlocked(op)
                    op["state"] = "applied"
                    save_work(service, processed, work)
                    finish_registration(service, op)
                    op["state"] = "settled"
                    strip_payload(op)
                elif op["action"] == "NO_CHANGE":
                    if "native" in op:
                        # Full-file and configuration guard was revalidated above.
                        # Native fragments are never passed to MemoryWriter.
                        validate_binding(op["native"], work.get("native_guard"), op["memory_id"])
                    else:
                        found = [r for r in service._read_memories_unlocked("knowledge")
                                 if r.memory.memory_id.casefold() == op["memory_id"].casefold()]
                        if len(found) != 1 or revision_digest(found[0].memory) != op["expected_revision"]:
                            raise MemoryVersionError("stale_incremental_target")
                    op["state"] = "settled"
                else:
                    op["state"] = "settled"
            except (ValueError, MemoryVersionError) as error:
                op.update(state="blocked", code=str(error)); strip_payload(op)
            save_work(service, processed, work)
        if work["index_status"] != "current":
            service._rebuild_index_unlocked()
            work["index_status"] = "current"
        # These two changes share the final atomic ledger replacement.
        work["receipt_settled"] = True
        _settle_source(service, processed, work, source_valid=source_valid)
        save_work(service, processed, work)
    except OSError as error:
        for op in work["operations"]:
            if op["state"] == "prepared" and op["action"] in {"CREATE", "UPDATE"}:
                try:
                    if writer.frozen_state_applied_unlocked(op): op["state"] = "applied"
                except (OSError, ValueError):
                    pass
        # A registration error is an applied business write with pending local
        # projection, not a blocked/unapplied candidate. Preserve its diagnosis.
        if any(op.get("scope_error") for op in work["operations"]):
            save_work(service, processed, work)
        raise IncrementalCommitError(public_result(work)) from error
    return public_result(work)


def _check_run_guard(processed, guard):
    if guard is None:
        return
    from .incremental_run_state import load_run, OWNER, TERMINAL as RUN_TERMINAL
    run_id, token = guard
    run = load_run(processed, run_id)
    if (run is None or run["status"] in RUN_TERMINAL
            or processed.get(OWNER, {}).get("token") != token):
        raise ValueError("incremental_run_revoked")


def apply_incremental(service: Any, *, response: str, expected_snapshot: str, intent_id: str,
                      source: str, session_id: str, turn_id: str, scope: Any = None,
                      priority_memory_ids=(), candidate_limit: int = 12, allow_new_scopes: bool = False,
                      _run_guard: tuple[str, str] | None = None, selection=None,
                      retention_request: str | None = None, captured_turn_key: str | None = None) -> dict[str, Any]:
    if not isinstance(intent_id, str) or not intent_id.strip() or len(intent_id) > 800 or any(c in intent_id for c in "\0\r\n"):
        raise ValueError("invalid_intent_id")
    if not isinstance(expected_snapshot, str) or len(expected_snapshot) != 64:
        raise ValueError("invalid_planning_snapshot")
    args = _arguments(source, session_id, turn_id, scope, priority_memory_ids, candidate_limit, allow_new_scopes, selection, captured_turn_key)
    if not isinstance(response, str) or len(response.encode("utf-8")) > 128 * 1024:
        raise ValueError("invalid_response_size")
    binding = {"arguments": args, "snapshot_id": expected_snapshot, "response_digest": digest(parse_strict_json(response))}
    identity = "inc-" + digest([source, session_id, captured_turn_selector(turn_id, captured_turn_key), intent_id])
    with service._mutation_boundary():
        processed = _read_processed(service.vault.processed_state_path)
        _check_run_guard(processed, _run_guard)
        stored = load_work(processed, identity)
        if stored is not None:
            if stored["binding"] != binding:
                raise ValueError("incremental_intent_payload_conflict")
            return (_resume_unlocked(service, processed, stored) if public_result(stored)["execution_status"] == "recovery_required"
                    else public_result(stored))
        selected, window = _window(service, source, session_id, captured_turn_selector(turn_id, captured_turn_key))
        if not recording_allowed(processed, source, session_id, selected.turn_key):
            raise ValueError("source_recording_revoked")
        state = processed.get("sessions", {}).get(f"{source}/{session_id}", {})
        if ProcessJournal(service)._processing_marker_live(state.get("processing"), utc_now()):
            raise ValueError("legacy_processing_busy")
        if turn_identity_key(source, session_id, selected.turn_key) in processed.get("pending_turn_plans", {}):
            raise ValueError("legacy_pending_plan")
        recovery_run = None
        if _run_guard is not None:
            from .incremental_run_state import load_run
            candidate_run = load_run(processed, _run_guard[0])
            if candidate_run.get("partial_used"):
                if candidate_run["arguments"] != args or candidate_run["commit_intent"] != intent_id:
                    raise ValueError("partial_commit_binding_changed")
                recovery_run = candidate_run
        if recovery_run is not None:
            from .incremental_partial import recheck_snapshot
            snapshot = recheck_snapshot(service, processed, recovery_run)
        else:
            snapshot = _prepare_incremental_unlocked(service, retention_request=retention_request, **args)
        if snapshot.snapshot_id != expected_snapshot:
            raise ValueError("stale_planning_snapshot")
        compiled = compile_incremental(response, snapshot)
        now = utc_now()
        active = [r.memory for r in service._read_memories_unlocked("knowledge")]
        inherited, proposals, issues = [], compiled["operations"], compiled["issues"]
        if recovery_run is not None:
            from .incremental_recovery import cumulative_result
            parent = load_work(processed, recovery_run["partial_recovery"]["parent_work_id"])
            inherited, proposals, issues = cumulative_result(
                compiled, parent, snapshot, set(recovery_run["partial_recovery"]["focus"]),
                repair_rows=(recovery_run["partial_recovery"]["repair_rows"]
                             if recovery_run["partial_recovery"]["mode"] == "repair" else None))
        operations = inherited + [_freeze_operation(op, snapshot, now, active) for op in proposals]
        evidence = [{k: e[k] for k in ("ref", "event_key", "use")} for e in snapshot.state()["evidence"]]
        if recovery_run is not None:
            # The compile view authorizes only unresolved new blocks. The source
            # receipt still accounts for the original complete selected set.
            original_new = {e["event_key"] for e in parent["evidence"] if e["use"] == "new"}
            for e in evidence:
                e["use"] = "new" if e["event_key"] in original_new else "context"
        work = {"version": VERSION, "work_id": identity, "intent_id": intent_id, "binding": binding,
                "request_kind": "explicit_remember" if selection else "automatic",
                "source": source, "session_id": session_id, "turn_id": turn_id, "turn_key": selected.turn_key,
                "turn_index": selected.turn_index, "source_digest": input_digest(selected), "source_window": window,
                "evidence": evidence,
                "operations": operations, "issues": issues, "receipt_settled": False,
                "native_guard": snapshot.state().get("native_guard"),
                "scope_guard": snapshot.state().get("scope_guard"),
                "native_comparison": {"status": "available" if snapshot.state().get("native_guard") else "no_eligible_sources",
                                      "selected_fragments": sum("native" in t for t in snapshot.state()["targets"].values()),
                                      "selection": "bounded_candidates", "read_only": True},
                "index_status": "dirty" if any(op["action"] in {"CREATE", "UPDATE"} and op["state"] == "prepared" for op in operations) else "current"}
        if recovery_run is not None:
            work["recovery_parent"] = parent["work_id"]
        save_work(service, processed, work)
        return _resume_unlocked(service, processed, work)


def resume_incremental(service: Any, work_id: str, *, _run_guard: tuple[str, str] | None = None) -> dict[str, Any]:
    if not isinstance(work_id, str) or not work_id.startswith("inc-") or len(work_id) != 68:
        raise ValueError("invalid_incremental_work_id")
    with service._mutation_boundary():
        processed = _read_processed(service.vault.processed_state_path)
        _check_run_guard(processed, _run_guard)
        work = load_work(processed, work_id)
        if work is None:
            raise ValueError("incremental_work_not_found")
        return (_resume_unlocked(service, processed, work) if public_result(work)["execution_status"] == "recovery_required"
                else public_result(work))
