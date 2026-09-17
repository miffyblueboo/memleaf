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

KEY = "incremental_commits"
VERSION = 1
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
    wrapper = works.get(work_id)
    if wrapper is None:
        return None
    if (not isinstance(wrapper, dict) or type(wrapper.get("version")) is not int
            or wrapper["version"] != VERSION or not isinstance(wrapper.get("payload"), str)):
        raise ValueError("unsupported_incremental_journal")
    payload = wrapper["payload"]
    if (len(payload.encode("utf-8")) > MAX_WORK_BYTES
            or hashlib.sha256(payload.encode("utf-8")).hexdigest() != wrapper.get("checksum")):
        raise ValueError("invalid_incremental_checksum")
    work = parse_strict_json(payload)
    if (not isinstance(work, dict) or work.get("work_id") != work_id
            or type(work.get("version")) is not int or work["version"] != VERSION
            or not isinstance(work.get("operations"), list) or not isinstance(work.get("issues"), list)
            or not isinstance(work.get("binding"), dict) or not isinstance(work.get("evidence"), list)):
        raise ValueError("invalid_incremental_work")
    if (any(not isinstance(work.get(k), str) or not work[k] for k in
            ("source", "session_id", "turn_id", "turn_key", "intent_id", "source_digest", "source_window"))
            or type(work.get("turn_index")) is not int or work["turn_index"] < 1
            or type(work.get("receipt_settled")) is not bool
            or not isinstance(work.get("index_status"), str) or work["index_status"] not in {"dirty", "current"}):
        raise ValueError("invalid_incremental_work")
    refs, ids = set(), set()
    for e in work["evidence"]:
        if (not isinstance(e, dict) or not isinstance(e.get("ref"), str) or not e["ref"]
                or not isinstance(e.get("event_key"), str) or not e["event_key"]
                or not isinstance(e.get("use"), str) or e["use"] not in {"new", "context"}
                or e["ref"] in refs):
            raise ValueError("invalid_incremental_evidence")
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
        ids.add(op["operation_id"])
    return work


def save_work(service: Any, processed: dict[str, Any], work: dict[str, Any]) -> None:
    """Caller holds the Vault lock. Never evict a receipt to reopen old work."""
    payload = canonical(work)
    if len(payload.encode("utf-8")) > MAX_WORK_BYTES:
        raise ValueError("incremental_work_too_large")
    works = processed.setdefault(KEY, {})
    if not isinstance(works, dict):
        raise ValueError("invalid_incremental_ledger")
    works[work["work_id"]] = {"version": VERSION, "payload": payload,
                              "checksum": hashlib.sha256(payload.encode("utf-8")).hexdigest()}
    if len(canonical(works).encode("utf-8")) > MAX_LEDGER_BYTES:
        raise ValueError("incremental_ledger_full")
    load_work(processed, work["work_id"])
    atomic_write_json(service.vault.processed_state_path, processed)


def strip_payload(op: dict[str, Any]) -> None:
    op.pop("before", None)
    op.pop("after", None)


def public_result(work: dict[str, Any]) -> dict[str, Any]:
    counts = {"committed": 0, "applied": 0, "no_change": 0, "no_memory": 0,
              "unresolved": 0, "issues": len(work["issues"])}
    operations = []
    for op in work["operations"]:
        operations.append({k: op[k] for k in ("operation_id", "action", "memory_id", "evidence", "state", "code") if k in op})
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
    return {"work_id": work["work_id"], "intent_id": work["intent_id"], "execution_status": status,
            "coverage_status": "partial" if unresolved else "complete", "operations": operations,
            "counts": counts, "issues": work["issues"], "index_status": work["index_status"], "model_calls": 0,
            "limitations": ["native_comparison_not_integrated", "explicit_staged_commit", "semantic_quality_not_verified"]}


def owned_turn_keys(processed: dict[str, Any], source: str, session_id: str) -> set[str]:
    works = processed.get(KEY, {})
    if not isinstance(works, dict):
        raise ValueError("invalid_incremental_ledger")
    from .incremental_run_state import owned_turns
    return owned_turns(processed, source, session_id) | {w["turn_key"] for key in works if (w := load_work(processed, key))["source"] == source
            and w["session_id"] == session_id}


def protected_turn_keys(processed: dict[str, Any], source: str, session_id: str) -> set[str]:
    works = processed.get(KEY, {})
    if not isinstance(works, dict):
        raise ValueError("invalid_incremental_ledger")
    from .incremental_run_state import owned_turns
    return owned_turns(processed, source, session_id, protect=True) | {w["turn_key"] for key in works if (w := load_work(processed, key))["source"] == source
            and w["session_id"] == session_id and public_result(w)["execution_status"] != "completed"}


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
    processed = _read_processed(service.vault.processed_state_path)
    works = processed.get(KEY, {})
    if not isinstance(works, dict):
        raise ValueError("invalid_incremental_ledger")
    for key in list(works):
        work = load_work(processed, key)
        shared = {e["ref"] for e in work["evidence"] if e["event_key"] in sources}
        changed = False
        for op in work["operations"]:
            target = op.get("memory_id") in ids
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
