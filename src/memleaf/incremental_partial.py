"""Explicit partial continuation using the canonical runner and original budget.

Not a background retry loop. Preparation is under the existing Vault lock;
network dispatch and crash recovery remain in incremental_execution.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from .incremental_commit import _window
from .incremental_journal import digest, load_work, public_result as commit_result
from .incremental_preview import _prepare_incremental_unlocked
from .incremental_protocol import compile_incremental, MAX_BYTES
from .incremental_prompts import INCREMENTAL_SYSTEM
from .incremental_recovery import (RECOVERY_SYSTEM, restore_snapshot, recovery_snapshot,
                                  recovery_input, context_changed, normalized_rows,
                                  unresolved_refs, isolated_focus, cumulative_result)
from .incremental_run_state import (load_run, save_run, public_result, owner_live, TERMINAL)
from .process_common import _read_processed
from .recording_policy import recording_allowed
from .turn_plan import input_digest
from .vault import safe_component


def current_snapshot(service: Any, processed: dict[str, Any], run: dict[str, Any], *,
                     mode: str, context_memory_ids: list[str], focus: set[str] | None = None):
    basis = run.get("partial_basis")
    if basis is None:
        raise ValueError("partial_recovery_basis_unavailable")
    original = restore_snapshot(basis["snapshot"])
    parent_id = run.get("partial_recovery", {}).get("parent_work_id", run["commit_work_id"])
    parent = load_work(processed, parent_id)
    if parent is None or commit_result(parent)["execution_status"] != "completed_with_unresolved":
        raise ValueError("partial_parent_not_settled")
    if not recording_allowed(processed, run["source"], run["session_id"], run["turn_key"]):
        raise ValueError("source_recording_revoked")
    turn, window = _window(service, run["source"], run["session_id"], run["turn_key"])
    if input_digest(turn) != run["source_digest"]:
        raise ValueError("partial_source_revised")
    old_state = original.state()
    binding = {e["ref"]: (e["event_key"], e["use"]) for e in parent["evidence"]}
    if binding != {e["ref"]: (e["event_key"], e["use"]) for e in old_state["evidence"]}:
        raise ValueError("partial_evidence_binding_changed")
    args = deepcopy(run["arguments"])
    priority = list(args["priority_memory_ids"])
    # Unresolved known targets are necessary context, not new write authority.
    for row in basis["rows"]:
        if isinstance(row, dict) and isinstance(row.get("target"), str):
            target = old_state["targets"].get(row["target"].strip())
            if target is not None:
                priority.append(target["memory"]["memory_id"])
    args["priority_memory_ids"] = list(dict.fromkeys(priority + context_memory_ids))
    fresh = _prepare_incremental_unlocked(service, retention_request=old_state.get("retention_request"), **args)
    if focus is None:
        focus = unresolved_refs(parent, old_state)
        if mode == "replan":
            focus = isolated_focus(basis, parent)
    if not focus:
        raise ValueError("no_isolated_unresolved_evidence")
    guarded_parent = {**parent, "_repair_rows": normalized_rows(basis, parent)[0]} if mode == "repair" else parent
    result = recovery_snapshot(original, fresh, guarded_parent, focus, mode=mode)
    return result, parent, window, context_changed(original, fresh, parent), focus


def recheck_snapshot(service: Any, processed: dict[str, Any], run: dict[str, Any]):
    recovery = run["partial_recovery"]
    snapshot, _, window, _, _ = current_snapshot(
        service, processed, run, mode=recovery["mode"], context_memory_ids=recovery["context_memory_ids"],
        focus=set(recovery["focus"]))
    if snapshot.snapshot_id != run["snapshot_id"] or window != run["source_window"]:
        raise ValueError("stale_partial_snapshot")
    return snapshot


def recover_incremental_partial(service: Any, run_id: str, *, mode: str = "replan",
                                context_memory_ids=(), model: Any = None, router: Any = None) -> dict[str, Any]:
    """One explicit partial round: local container repair OR changed-context replan.

    No-context/no-repair calls leave the durable run unchanged. A second model
    call is never authorized by a mode switch, argument nonce or a new run ID.
    """
    from .incremental_execution import _guard_legacy, resume_incremental_run, _budget_count
    from .incremental_runtime import _resolve
    if not isinstance(mode, str) or mode not in {"repair", "replan"}:
        raise ValueError("invalid_partial_mode")
    if model is not None and router is not None:
        raise ValueError("ambiguous_model_route")
    if not isinstance(context_memory_ids, (list, tuple)):
        raise ValueError("invalid_context_memory_ids")
    extra = list(context_memory_ids)
    if len(extra) > 20 or any(not isinstance(mid, str) for mid in extra) or len(set(extra)) != len(extra):
        raise ValueError("invalid_context_memory_ids")
    extra.sort()
    for mid in extra:
        safe_component(mid, "memory id")
    if mode == "repair" and extra:
        raise ValueError("repair_cannot_add_context")
    with service.vault.lock():
        processed = _read_processed(service.vault.processed_state_path)
        run = load_run(processed, run_id)
        if run is None:
            raise ValueError("incremental_run_not_found")
        if run.get("partial_used"):
            if (run["partial_recovery"]["mode"] != mode
                    or run["partial_recovery"]["context_memory_ids"] != extra):
                raise ValueError("partial_recovery_already_bound")
            if run["status"] in TERMINAL:
                return public_result(run)
        elif run["status"] != "completed_with_unresolved":
            raise ValueError("partial_recovery_not_available")
        else:
            if owner_live(processed):
                raise ValueError("incremental_model_busy")
            _guard_legacy(service, processed)
            snapshot, parent, window, changed, focus = current_snapshot(
                service, processed, run, mode=mode, context_memory_ids=extra)
            if mode == "replan" and not changed:
                return {**public_result(run), "partial_recovery_code": "context_unchanged"}
            if mode == "repair":
                rows, repaired, repair_indices = normalized_rows(run["partial_basis"], parent)
                if not repaired:
                    return {**public_result(run), "partial_recovery_code": "no_safe_structural_repair"}
                response = json.dumps({"items": rows}, ensure_ascii=False, separators=(",", ":"))
                compiled = compile_incremental(response, snapshot)
                _, proposals, _ = cumulative_result(compiled, parent, snapshot, focus, repair_rows=repair_indices)
                if not any(op["action"] != "DEFERRED" for op in proposals):
                    return {**public_result(run), "partial_recovery_code": "no_safe_structural_repair"}
            else:
                response, repair_indices = None, []
                # Inspect the same durable counter before changing the receipt.
                # The canonical dispatcher still atomically reserves later.
                from .extraction_work_state import ExtractionWorkStateError
                try:
                    consumed = _budget_count(service, run)
                except ExtractionWorkStateError as error:
                    raise ValueError("partial_budget_state_lost") from error
                if consumed < run["reserved_requests"]:
                    raise ValueError("partial_budget_state_lost")
                if consumed >= 2:
                    return {**public_result(run), "partial_recovery_code": "request_budget_exhausted"}
            request = {"system": INCREMENTAL_SYSTEM + (RECOVERY_SYSTEM if mode == "replan" else ""),
                       "user": json.dumps(recovery_input(snapshot, parent, mode=mode), ensure_ascii=False, separators=(",", ":"))}
            if sum(len(v.encode("utf-8")) for v in request.values()) > MAX_BYTES:
                raise ValueError("blocked_context")
            parent_id = run["commit_work_id"]
            commit_intent = run["commit_intent"] + "/partial-v1"
            run.update(partial_used=True,
                       partial_recovery={"version": 1, "mode": mode, "parent_work_id": parent_id,
                                         "focus": sorted(focus), "context_memory_ids": extra, "repair_rows": repair_indices},
                       commit_intent=commit_intent,
                       commit_work_id="inc-" + digest([run["source"], run["session_id"], run["turn_key"], commit_intent]),
                       request=request, request_digest=digest(request), snapshot_id=snapshot.snapshot_id,
                       source_window=window, status="response_ready" if response is not None else "ready", code=None)
            if response is not None:
                run["response"] = response
            else:
                run.pop("response", None)
            if "retention_request" in run["partial_basis"]["snapshot"]:
                run["retention_request"] = run["partial_basis"]["snapshot"]["retention_request"]
            run["native_comparison"] = {
                "status": "available" if snapshot.state().get("native_guard") else "no_eligible_sources",
                "selected_fragments": sum("native" in t for t in snapshot.state()["targets"].values()),
                "selection": "bounded_candidates", "read_only": True,
            }
            # New dependencies must also participate in Forget and cleanup.
            run["source_keys"] = sorted(set(run["source_keys"]) | {e["event_key"] for e in snapshot.state()["evidence"]})
            run["target_ids"] = sorted(set(run["target_ids"]) | {t["memory"]["memory_id"] for t in snapshot.state()["targets"].values()})
            save_run(service, processed, run)
        stored_commit = load_work(processed, run["commit_work_id"])
        needs_model = (mode == "replan" and run["status"] not in {"response_ready", "committing"}
                       and stored_commit is None)
    backend = _resolve(service, model, router) if needs_model else None
    return resume_incremental_run(service, run_id, backend=backend)
