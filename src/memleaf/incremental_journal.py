"""Bounded, versioned receipts for explicit incremental commits.

These are control state in the existing processed ledger, not a memory database.
Reading receipts never replays a knowledge write. Terminal receipts omit bodies.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .locking import atomic_write_json
from .process_common import _read_processed
from .validation import parse_strict_json
from .receipt_codec import decode_receipt, encode_receipt, is_compact, ledger_usage

KEY = "incremental_commits"
VERSION = 2
COMPACT_VERSION = 3
SUPPORTED_VERSIONS = {1, VERSION}
MAX_WORK_BYTES = 8 * 1024 * 1024
MAX_LEDGER_BYTES = 16 * 1024 * 1024
TERMINAL = frozenset({"settled", "blocked", "cancelled", "deferred"})


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def load_work(processed: dict[str, Any], work_id: str) -> dict[str, Any] | None:
    works = processed.get(KEY, {})
    if not isinstance(works, dict):
        raise ValueError("invalid_incremental_ledger")
    ledger_usage(works, compact_version=COMPACT_VERSION)
    wrapper = works.get(work_id)
    if wrapper is None:
        return None
    compact = is_compact(wrapper, COMPACT_VERSION)
    if compact:
        wrapper = decode_receipt(wrapper, version=COMPACT_VERSION, maximum=MAX_WORK_BYTES, payload_versions=SUPPORTED_VERSIONS)
    if (not isinstance(wrapper, dict) or type(wrapper.get("version")) is not int
            or wrapper["version"] not in SUPPORTED_VERSIONS or not isinstance(wrapper.get("payload"), str)):
        raise ValueError("unsupported_incremental_journal")
    payload = wrapper["payload"]
    if (len(payload.encode("utf-8")) > MAX_WORK_BYTES
            or hashlib.sha256(payload.encode("utf-8")).hexdigest() != wrapper.get("checksum")):
        raise ValueError("invalid_incremental_checksum")
    work = parse_strict_json(payload)
    if (not isinstance(work, dict) or work.get("work_id") != work_id
            or type(work.get("version")) is not int or work["version"] != wrapper["version"]
            or not isinstance(work.get("operations"), list) or not isinstance(work.get("issues"), list)
            or not isinstance(work.get("binding"), dict) or not isinstance(work.get("evidence"), list)):
        raise ValueError("invalid_incremental_work")
    if (any(not isinstance(work.get(k), str) or not work[k] for k in
            ("source", "session_id", "turn_id", "turn_key", "intent_id", "source_digest", "source_window"))
            or type(work.get("turn_index")) is not int or work["turn_index"] < 1
            or type(work.get("receipt_settled")) is not bool
            or not isinstance(work.get("index_status"), str) or work["index_status"] not in {"dirty", "current"}):
        raise ValueError("invalid_incremental_work")
    from .incremental_selection import validate_selection
    selection = validate_selection(work["binding"].get("arguments", {}).get("selection"))
    kind = work.get("request_kind", "automatic")
    if not isinstance(kind, str) or kind not in {"automatic", "explicit_remember"} or (kind == "explicit_remember") != bool(selection):
        raise ValueError("invalid_incremental_intent")
    if "recovery_parent" in work and (not isinstance(work["recovery_parent"], str)
            or not work["recovery_parent"].startswith("inc-") or len(work["recovery_parent"]) != 68
            or work["recovery_parent"] == work_id):
        raise ValueError("invalid_partial_commit_parent")
    if work.get("native_guard") is not None:
        from .incremental_native import validate_guard
        validate_guard(work["native_guard"])
    if work["version"] == VERSION and work.get("scope_guard") is None:
        raise ValueError("missing_scope_guard")
    if work.get("scope_guard") is not None:
        from .incremental_scopes import validate_guard
        validate_guard(work["scope_guard"])
    refs, ids = set(), set()
    for e in work["evidence"]:
        if (not isinstance(e, dict) or not isinstance(e.get("ref"), str) or not e["ref"]
                or not isinstance(e.get("event_key"), str) or not e["event_key"]
                or not isinstance(e.get("use"), str) or e["use"] not in {"new", "context"}
                or e["ref"] in refs):
            raise ValueError("invalid_incremental_evidence")
        if e.get("explicit_input") is not None:
            from .explicit_text_source import validate_origin
            origin = validate_origin(e["explicit_input"])
            if not selection or origin["intent_hash"] != digest(selection["intent_id"]):
                raise ValueError("invalid_explicit_text_intent")
        refs.add(e["ref"])
    for op in work["operations"]:
        if (not isinstance(op, dict) or not isinstance(op.get("operation_id"), str)
                or not op["operation_id"] or op["operation_id"] in ids
                or not isinstance(op.get("action"), str)
                or op["action"] not in {"CREATE", "UPDATE", "NO_CHANGE", "NO_MEMORY", "DEFERRED"}
                or not isinstance(op.get("state"), str) or op["state"] not in TERMINAL | {"prepared", "applied"}
                or not isinstance(op.get("evidence"), list) or not op["evidence"]
                or any(not isinstance(ref, str) or ref not in refs for ref in op["evidence"])):
            raise ValueError("invalid_incremental_operation")
        if (op["action"] in {"CREATE", "UPDATE", "NO_CHANGE"}
                and (not isinstance(op.get("memory_id"), str) or not op["memory_id"])):
            raise ValueError("invalid_incremental_operation")
        if (op["action"] in {"CREATE", "UPDATE"} and op["state"] in {"prepared", "applied"}
                and (not isinstance(op.get("after"), str) or not op["after"]
                     or not isinstance(op.get("replacement_revision"), str))):
            raise ValueError("invalid_incremental_operation")
        if "native" in op:
            from .incremental_native import validate_binding
            if op["action"] != "NO_CHANGE" or any(k in op for k in ("before", "after")):
                raise ValueError("native_target_must_be_read_only")
            validate_binding(op["native"], work.get("native_guard"), op["memory_id"])
        if work["version"] == 1 and "scope_registration" in op:
            raise ValueError("unsupported_scope_registration")
        from .incremental_scopes import validate_registration
        validate_registration(op, work.get("scope_guard"))
        ids.add(op["operation_id"])
    if compact and (not work["receipt_settled"] or work["index_status"] != "current"
            or any(op["state"] not in TERMINAL or "before" in op or "after" in op for op in work["operations"])):
        raise ValueError("compact_work_not_sealed")
    return work


def save_work(service: Any, processed: dict[str, Any], work: dict[str, Any]) -> None:
    """Caller holds the Vault lock. Never evict a receipt to reopen old work."""
    payload = canonical(work)
    if len(payload.encode("utf-8")) > MAX_WORK_BYTES:
        raise ValueError("incremental_work_too_large")
    works = processed.setdefault(KEY, {})
    if not isinstance(works, dict):
        raise ValueError("invalid_incremental_ledger")
    previous = works.get(work["work_id"])
    works[work["work_id"]] = {"version": work["version"], "payload": payload,
                              "checksum": hashlib.sha256(payload.encode("utf-8")).hexdigest()}
    if is_compact(previous, COMPACT_VERSION):
        works[work["work_id"]] = encode_receipt(works[work["work_id"]], version=COMPACT_VERSION)
    ledger_usage(works, compact_version=COMPACT_VERSION)
    if len(canonical(works).encode("utf-8")) > MAX_LEDGER_BYTES:
        raise ValueError("incremental_ledger_full")
    load_work(processed, work["work_id"])
    atomic_write_json(service.vault.processed_state_path, processed)


def strip_payload(op: dict[str, Any]) -> None:
    op.pop("before", None)
    op.pop("after", None)
    if op.get("state") in {"blocked", "cancelled"} and "scope_registration" in op:
        op["scope_registration"]["state"] = "cancelled"


def public_result(work: dict[str, Any]) -> dict[str, Any]:
    counts = {"committed": 0, "applied": 0, "no_change": 0, "no_memory": 0,
              "unresolved": 0, "issues": len(work["issues"])}
    operations = []
    for op in work["operations"]:
        operations.append({k: op[k] for k in ("operation_id", "action", "memory_id", "evidence", "state", "code") if k in op})
        if "native" in op:
            operations[-1]["native"] = True
        if "scope_registration" in op:
            operations[-1]["scope_registration"] = dict(op["scope_registration"])
        if "scope_error" in op:
            operations[-1]["scope_error"] = op["scope_error"]
        if op["state"] in {"applied", "settled"} and op["action"] in {"CREATE", "UPDATE"}:
            counts["applied"] += 1
        if op["state"] == "settled":
            key = "committed" if op["action"] in {"CREATE", "UPDATE"} else op["action"].lower()
            if key in counts:
                counts[key] += 1
        else:
            counts["unresolved"] += 1
    active = any(op["state"] in {"prepared", "applied"} for op in work["operations"])
    unresolved = bool(work["issues"] or counts["unresolved"])
    status = "recovery_required" if active or work["index_status"] != "current" or not work["receipt_settled"] else (
        "completed_with_unresolved" if unresolved else "completed")
    settled_actions = [op["action"] for op in work["operations"] if op["state"] == "settled"]
    disposition = ("deferred" if unresolved else
                   "memory" if any(action in {"CREATE", "UPDATE"} for action in settled_actions) else
                   "no_change" if "NO_CHANGE" in settled_actions else
                   "no_memory" if "NO_MEMORY" in settled_actions else "unknown")
    return {"work_id": work["work_id"], "intent_id": work["intent_id"], "execution_status": status,
            "request_kind": work.get("request_kind", "automatic"), "turn_disposition": disposition,
            "coverage_status": "partial" if unresolved else "complete", "operations": operations,
            "counts": counts, "issues": work["issues"], "index_status": work["index_status"], "model_calls": 0,
            "native_comparison": work.get("native_comparison", {"status": "not_evaluated"}),
            "limitations": ["native_conflict_coordination_not_automatic", "explicit_staged_commit", "semantic_quality_not_verified"]}


def owned_turn_keys(processed: dict[str, Any], source: str, session_id: str) -> set[str]:
    works = processed.get(KEY, {})
    if not isinstance(works, dict):
        raise ValueError("invalid_incremental_ledger")
    from .incremental_run_state import owned_turns
    return owned_turns(processed, source, session_id) | {w["turn_key"] for key in works if (w := load_work(processed, key))["source"] == source
            and w["session_id"] == session_id
            and (w.get("request_kind", "automatic") == "automatic"
                 or public_result(w)["execution_status"] == "recovery_required")}


def referenced_turn_keys(processed: dict[str, Any], source: str, session_id: str,
                         event_keys: set[str]) -> set[str]:
    """Protect actual context dependencies without claiming their ownership.

    Old receipts have event keys but not per-event turn keys. Resolve them from
    durable capture metadata; if a retained dependency has lost that mapping,
    conservatively protect the known session instead of deleting its context.
    """
    events = processed.get("events", {})
    if not isinstance(events, dict):
        raise ValueError("invalid_capture_receipts")
    result, unknown = set(), False
    for key in event_keys:
        event = events.get(key)
        if (isinstance(event, dict) and event.get("source") == source
                and event.get("session_id") == session_id and isinstance(event.get("turn_key"), str)):
            result.add(event["turn_key"])
        else:
            unknown = True
    if unknown:
        state = processed.get("sessions", {}).get(f"{source}/{session_id}", {})
        result.update(key for key in state.get("turns", {}) if isinstance(key, str))
    return result


def resolved_parent_ids(processed: dict[str, Any]) -> set[str]:
    """A cumulative successful child releases only its own immutable parent.

    No parent decision or identity is rewritten. The successful child receipt
    is the durable resolution link, so a crash cannot leave a separate GC flag
    ahead of business settlement.
    """
    works = {key: load_work(processed, key) for key in processed.get(KEY, {})}
    resolved = set()
    for child in works.values():
        parent_id = child.get("recovery_parent")
        if parent_id is None:
            continue
        parent = works.get(parent_id)
        if (parent is None or parent_id == child["work_id"] or "recovery_parent" in parent
                or any(parent[k] != child[k] for k in ("source", "session_id", "turn_key", "source_digest"))
                or parent.get("request_kind", "automatic") != child.get("request_kind", "automatic")
                or parent["binding"]["arguments"] != child["binding"]["arguments"]
                or {e["event_key"] for e in parent["evidence"] if e["use"] == "new"} !=
                   {e["event_key"] for e in child["evidence"] if e["use"] == "new"}):
            raise ValueError("invalid_partial_commit_parent")
        accepted = {op["operation_id"]: op for op in parent["operations"] if op["state"] == "settled"}
        copied = {op["operation_id"]: op for op in child["operations"]}
        if any(copied.get(key) != op for key, op in accepted.items()):
            raise ValueError("partial_settled_operation_changed")
        if public_result(child)["execution_status"] == "completed":
            resolved.add(parent_id)
    return resolved


def protected_turn_keys(processed: dict[str, Any], source: str, session_id: str) -> set[str]:
    works = processed.get(KEY, {})
    if not isinstance(works, dict):
        raise ValueError("invalid_incremental_ledger")
    from .incremental_run_state import owned_turns
    result = owned_turns(processed, source, session_id, protect=True)
    resolved = resolved_parent_ids(processed)
    for key in works:
        if key in resolved:
            continue
        work = load_work(processed, key)
        if work["source"] == source and work["session_id"] == session_id and public_result(work)["execution_status"] != "completed":
            result.add(work["turn_key"])
            result.update(referenced_turn_keys(processed, source, session_id,
                                              {e["event_key"] for e in work["evidence"]}))
    return result


def reconcile_applied_unlocked(service: Any) -> None:
    """Acknowledge exact written heads before another supported mutation.

    Never replay pending head writes here: Forget must be able to cancel first.
    """
    from .memory_writer import MemoryWriter
    processed = _read_processed(service.vault.processed_state_path)
    works = processed.get(KEY, {})
    if not isinstance(works, dict):
        raise ValueError("invalid_incremental_ledger")
    writer = MemoryWriter(service)
    for key in list(works):
        work = load_work(processed, key)
        changed = False
        for op in work["operations"]:
            if op["state"] == "prepared" and op["action"] in {"CREATE", "UPDATE"} and writer.frozen_state_applied_unlocked(op):
                op["state"] = "applied"
                changed = True
        if changed:
            save_work(service, processed, work)


def cancel_forgotten_unlocked(service: Any, records: list[Any]) -> None:
    """Remove target/shared-source pending plaintext before physical deletion."""
    ids, sources = set(), set()
    for record in records:
        memory = record.memory
        ids.add(memory.memory_id)
        ids.update(v for k in ("active_memory_id", "original_memory_id")
                   if isinstance((v := memory.extra.get(k)), str))
        sources.update(s["event_key"] for s in memory.sources if isinstance(s.get("event_key"), str))
    ids = {identity.casefold() for identity in ids}
    processed = _read_processed(service.vault.processed_state_path)
    works = processed.get(KEY, {})
    if not isinstance(works, dict):
        raise ValueError("invalid_incremental_ledger")
    for key in list(works):
        work = load_work(processed, key)
        shared = {e["ref"] for e in work["evidence"] if e["event_key"] in sources}
        changed = False
        for op in work["operations"]:
            target_id = op.get("memory_id")
            target = isinstance(target_id, str) and target_id.casefold() in ids
            if target or (bool(shared.intersection(op["evidence"])) and op["state"] not in TERMINAL):
                op.update(state="cancelled", code="explicit_forget" if target else "shared_source_forgotten")
                strip_payload(op)
                op.pop("need", None)
                changed = True
        if changed:
            work["issues"] = [{"code": "explicit_forget", "evidence": sorted(shared)}]
            save_work(service, processed, work)

    from .incremental_run_state import cancel_forgotten_unlocked as cancel_runs
    cancel_runs(service, _read_processed(service.vault.processed_state_path), ids, sources)
