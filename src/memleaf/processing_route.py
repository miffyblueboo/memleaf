"""Bounded automatic inbox routing on the sole incremental processing engine.

This module owns compatibility selection, scheduling and the public result
envelope.  Legacy configuration and retained legacy state can still be
recognized for migration, but no legacy execution path exists.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Mapping

from .capture import recover_capture_receipts_unlocked
from .extraction_work_state import extraction_work_id
from .incremental_commit import IncrementalCommitError
from .incremental_execution import IncrementalRunError
from .incremental_journal import KEY as COMMIT_KEY, load_work, public_result as commit_result
from .incremental_run_state import KEY as RUN_KEY, TERMINAL, load_run, owner_live, public_result
from .incremental_runtime import process_incremental, _resolve
from .inbox import source_ordered_turns
from .models import utc_now
from .process_common import _read_processed
from .process_journal import ProcessJournal
from .recording_policy import recording_allowed
from .scope_state import normalize_scopes
from .turn_plan import input_digest, turn_identity_key
from .vault import safe_component

PIPELINES = frozenset({"incremental"})
MAX_BATCH_TURNS = 4
MAX_RESULT_ROWS = 100


def _select_pipeline(config: Mapping[str, Any], key: str, pipeline: str | None = None) -> str:
    process = config.get("process", {})
    value = process.get(key, "incremental") if isinstance(process, Mapping) and pipeline is None else pipeline
    if value == "legacy":
        raise ValueError("legacy_pipeline_removed")
    if not isinstance(value, str) or value not in PIPELINES:
        raise ValueError("invalid_processing_pipeline")
    return "incremental"


def select_pipeline(config: Mapping[str, Any], pipeline: str | None = None) -> str:
    """Return the sole executable automatic route, rejecting legacy explicitly."""
    return _select_pipeline(config, "automatic_pipeline", pipeline)


def select_remember_pipeline(config: Mapping[str, Any], pipeline: str | None = None) -> str:
    """Return the sole executable explicit-remember route."""
    return _select_pipeline(config, "remember_pipeline", pipeline)


def _matches(source, session_id, obj):
    return ((source is None or obj["source"] == source)
            and (session_id is None or obj["session_id"] == session_id))


def _snapshot(service, source, session_id):
    """Caller holds the Vault lock. Validate controls; never claim a legacy job."""
    processed = _read_processed(service.vault.processed_state_path)
    sessions = processed.get("sessions", {})
    if not isinstance(sessions, dict):
        raise ValueError("invalid_processed_sessions")
    journal = ProcessJournal(service)
    if owner_live(processed) or any(journal._processing_marker_live(s.get("processing"), utc_now())
                                   for s in sessions.values() if isinstance(s, dict)):
        raise ValueError("processing_busy")
    runs = {k: load_run(processed, k) for k in processed.get(RUN_KEY, {})}
    works = {k: load_work(processed, k) for k in processed.get(COMMIT_KEY, {})}
    grouped = journal._turns_by_session()
    actions, skipped = [], []
    available_identities = set()
    for _, available in sorted(grouped.items()):
        if not available or not _matches(source, session_id, vars(available[0])):
            continue
        # This orders only provable source windows; no capture timestamp guessing.
        barrier = False
        for turn in source_ordered_turns(available):
            available_identities.add((turn.source, turn.session_id, turn.turn_key))
            base = {"source": turn.source, "session_id": turn.session_id,
                    "turn_key": turn.turn_key, "turn_index": turn.turn_index}
            if not turn.processable or not turn.complete:
                skipped.append({**base, "execution_status": "blocked", "code": "source_not_complete"})
                barrier = True
                continue
            rid = "inc-run-" + extraction_work_id(turn, request_kind="automatic", intent_id="automatic").removeprefix("work-")
            run = runs.get(rid)
            if run is not None and run["status"] in TERMINAL:
                if run["status"] == "completed" and run.get("budget_finalized"):
                    continue
                if run["status"] != "completed":
                    skipped.append(_row(turn, public_result(run)))
                    continue
            state = sessions.get(f"{turn.source}/{turn.session_id}", {})
            if not isinstance(state, dict):
                raise ValueError("invalid_processed_session")
            prior = [e for e in state.get("processed_turns", []) if isinstance(e, dict)
                     and e.get("turn_key") == turn.turn_key and set(e.get("event_keys", [])) == set(turn.event_keys)]
            if len(prior) > 1:
                raise ValueError("ambiguous_processed_turn")
            if run is None and prior and not prior[0].get("deferred_evidence") and not prior[0].get("deferred_candidates"):
                continue
            code = None
            if barrier:
                code = "earlier_source_incomplete"
            elif not recording_allowed(processed, turn.source, turn.session_id, turn.turn_key):
                code = "source_recording_revoked"
            elif turn_identity_key(turn.source, turn.session_id, turn.turn_key) in processed.get("pending_turn_plans", {}):
                code = "legacy_pending_plan"
            elif run is None and prior:
                code = "legacy_partial_requires_migration"
            same_works = [w for w in works.values() if w["source"] == turn.source
                          and w["session_id"] == turn.session_id and w["turn_key"] == turn.turn_key
                          and w.get("request_kind", "automatic") == "automatic"
                          and w["source_digest"] == input_digest(turn)]
            if code is None and run is None and same_works:
                code = "incremental_commit_requires_explicit_resume"
            if code:
                skipped.append({**base, "execution_status": "blocked", "code": code})
            else:
                actions.append((turn, run))
    for run in runs.values():
        if (_matches(source, session_id, run) and run.get("request_kind", "automatic") == "automatic"
                and run["status"] != "completed"
                and (run["source"], run["session_id"], run["turn_key"]) not in available_identities):
            skipped.append({k: run[k] for k in ("source", "session_id", "turn_key", "run_id")}
                           | {"execution_status": "blocked", "code": "source_not_available"})
    return actions, skipped


def _safe_code(error):
    # Never echo arbitrary exception text or credentials through jobs/Provider.
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in {"model_unavailable", "model_auth_failed", "model_timeout", "model_rate_limited"}:
        return code
    text = str(error)
    if isinstance(error, ValueError) and text.isascii() and text.replace("_", "").isalnum() and len(text) <= 90:
        return text
    return "model_unavailable" if type(error).__name__ == "ModelUnavailable" else "processing_error"


def _row(turn, result):
    commit = result.get("commit") or {}
    ops = commit.get("operations", [])
    ids = sorted({op["memory_id"] for op in ops if op.get("memory_id") and not op.get("native")
                  and op["state"] in {"applied", "settled"}})
    unresolved_refs = {ref for op in ops if op.get("state") not in {"applied", "settled"}
                       for ref in op.get("evidence", [])}
    unresolved_refs.update(ref for issue in commit.get("issues", []) for ref in issue.get("evidence", []))
    return {"source": turn.source, "session_id": turn.session_id, "turn_key": turn.turn_key,
            "turn_index": turn.turn_index, "run_id": result.get("run_id"),
            "work_id": result.get("commit_work_id"), "execution_status": result["execution_status"],
            "code": result.get("code"), "memory_ids": ids,
            "model_calls": result.get("model_calls_this_invocation", 0),
            "reservations": result.get("reserved_requests", 0),
            "unresolved_evidence_count": len(unresolved_refs),
            "unlocated_issue_count": sum(not issue.get("evidence") for issue in commit.get("issues", [])),
            "partial_recovery_available": result.get("partial_recovery_available", False)}


def _applied(result):
    commit = result.get("commit") or {}
    return {op["operation_id"]: op["memory_id"] for op in commit.get("operations", [])
            if op.get("action") in {"CREATE", "UPDATE"} and op.get("state") in {"applied", "settled"}}


def process_inbox(service: Any, *, source: str | None = None, session_id: str | None = None,
                  model: Any = None, router: Any = None, scope: Any = None,
                  recover: bool = False) -> dict[str, Any]:
    if type(recover) is not bool:
        raise ValueError("invalid_recover")
    if source is not None:
        source = safe_component(source, "source")
    if session_id is not None:
        session_id = safe_component(session_id, "session id")
    if model is not None and router is not None:
        raise ValueError("ambiguous_model_route")
    boundary = normalize_scopes(scope) if scope is not None else None
    # Capture receipt recovery precedes incremental selection.
    with service.vault.lock():
        processed = _read_processed(service.vault.processed_state_path)
        changed = False
        for path in service.vault.list_markdown("inbox"):
            changed = recover_capture_receipts_unlocked(service.vault, processed, path) or changed
        if changed:
            ProcessJournal(service)._write_processed_unlocked(processed)
        try:
            pending, residual = _snapshot(service, source, session_id)
        except ValueError as error:
            return _summary([], [{"execution_status": "blocked", "code": _safe_code(error)}], 0, {}, 0)
    rows, newly_applied, attempted = [], {}, 0
    initial_config = service.vault.config().get("process", {}).get("automatic_pipeline", "incremental")
    for turn, run in pending:
        if attempted >= MAX_BATCH_TURNS:
            break
        args = run["arguments"] if run else {
            "source": turn.source, "session_id": turn.session_id,
            "turn_id": turn.turn_key, "captured_turn_key": turn.turn_key,
            "scope": boundary, "priority_memory_ids": [], "candidate_limit": 12,
            "allow_new_scopes": False,
        }
        if scope is not None and args.get("scope") != boundary:
            rows.append(_row(turn, {"execution_status": "blocked", "code": "incremental_run_arguments_changed"}))
            continue
        with service.vault.lock():
            state = _read_processed(service.vault.processed_state_path)
            saved_work = load_work(state, run["commit_work_id"]) if run else None
        saved_commit = saved_work is not None
        # Polling/repeated enqueue must not implicitly spend a transport retry.
        can_resume_without_model = run is not None and (run["status"] in TERMINAL | {"response_ready", "committing"} or saved_commit)
        if run is not None and not can_resume_without_model and not recover:
            rows.append(_row(turn, {**public_result(run), "execution_status": "retryable", "code": "explicit_recovery_required"}))
            continue
        attempted += 1
        before = _applied({"commit": commit_result(saved_work)}) if saved_work else (_applied(public_result(run)) if run else {})
        try:
            if service.vault.config().get("process", {}).get("automatic_pipeline", "incremental") != initial_config:
                rows.append(_row(turn, {"execution_status": "blocked", "code": "processing_pipeline_changed"}))
                break
            if run is not None:
                from .incremental_execution import resume_incremental_run
                # A pre-existing frozen commit needs no model even if its run
                # receipt was not yet changed to response_ready.
                with service.vault.lock():
                    state = _read_processed(service.vault.processed_state_path)
                    saved_commit = run["commit_work_id"] in state.get(COMMIT_KEY, {})
                backend = None if can_resume_without_model or saved_commit else _resolve(service, model, router)
                result = resume_incremental_run(service, run["run_id"], backend=backend)
            else:
                result = process_incremental(service, model=model, router=router, recover=recover, **args)
        except (IncrementalRunError, IncrementalCommitError) as error:
            result = error.result
            if "run_id" not in result:
                result = {"execution_status": "recovery_required", "commit": result,
                          "commit_work_id": result.get("work_id"), "code": "commit_recovery_required"}
            else:
                result = {**result, "execution_status": "recovery_required", "code": "run_recovery_required"}
        except Exception as error:
            result = {"execution_status": "blocked", "code": _safe_code(error)}
        # A runtime exception may predate copying the commit receipt into the
        # run even though independent operations are already settled on disk.
        # Report proven writes from that same work, never discard them behind
        # a generic outer execution error or infer them from a content search.
        if result.get("commit_work_id"):
            try:
                with service.vault.lock():
                    state = _read_processed(service.vault.processed_state_path)
                    observed_work = load_work(state, result["commit_work_id"])
                if observed_work is not None:
                    result = {**result, "commit": commit_result(observed_work)}
            except (OSError, ValueError):
                result = {**result, "execution_status": "recovery_required", "code": "commit_state_unavailable"}
        rows.append(_row(turn, result))
        newly_applied.update({op: mid for op, mid in _applied(result).items() if op not in before})
        if result.get("code") in {"processing_busy", "incremental_model_busy", "legacy_processing_busy", "model_unavailable", "model_auth_failed"}:
            break
    # Re-read receipts after every batch; never derive remaining work from max
    # local turn_index or cumulative counters of a different source ordering.
    with service.vault.lock():
        try:
            remaining, unresolved = _snapshot(service, source, session_id)
        except ValueError as error:
            remaining, unresolved = [], [{"execution_status": "blocked", "code": _safe_code(error)}]
    seen = {(r.get("source"), r.get("session_id"), r.get("turn_key")) for r in rows}
    # Keep historical terminal failures visible without resubmitting them.
    outstanding = []
    for entry in unresolved:
        token = (entry.get("source"), entry.get("session_id"), entry.get("turn_key"))
        if token not in seen:
            outstanding.append(entry)
    pending_count = len(remaining)
    result = _summary(rows, outstanding, pending_count, newly_applied, attempted)
    # The host may continue fresh work or zero-call response/commit recovery.
    # Transport retries still require recover=True; do not mark them as an
    # automatic retry opportunity merely because they remain in the inbox.
    result["retryable_deferred_turns"] = sum(run is None or run["status"] in
        TERMINAL | {"response_ready", "committing"} for _, run in remaining)
    # Reuse the old deterministic cleanup contract after the bounded work.
    # Its dependency checks protect partial sources and frozen context windows.
    try:
        with service.vault.lock():
            state = _read_processed(service.vault.processed_state_path)
            if not owner_live(state):
                journal = ProcessJournal(service)
                result["cleaned_turns"] = journal._cleanup_due_unlocked(state, utc_now(), journal._cleanup_hours())
    except (OSError, ValueError, RuntimeError):
        result["cleanup_status"] = "recovery_required"
        result["execution_status"] = "partial"
        # Do not replace known per-source coverage or successful writes.
    return result


def _summary(rows, outstanding, pending_count, applied, attempted):
    all_rows = rows + outstanding
    errors = [r for r in all_rows if r["execution_status"] != "completed"]
    incomplete = bool(errors or pending_count)
    statuses = Counter(r["execution_status"] for r in errors)
    return {
        "pipeline": "incremental", "execution_status": "partial" if incomplete else "completed",
        "coverage_status": "partial" if incomplete else "complete", "processed_turns": sum(r["execution_status"] == "completed" for r in rows),
        "attempted_turns": attempted, "memories_written": len(applied), "memory_ids": sorted(set(applied.values())),
        "committed_operation_ids": sorted(applied), "metadata_merged": 0, "cleaned_turns": 0,
        "pending_inbox_turns": pending_count, "deferred_inbox_turns": len(errors),
        "deferred_candidates": statuses["completed_with_unresolved"],
        "unresolved_evidence_count": sum(r.get("unresolved_evidence_count", 0) for r in errors),
        "unlocated_issue_count": sum(r.get("unlocated_issue_count", 0) for r in errors),
        "model_calls": sum(r.get("model_calls", 0) for r in rows),
        "results": all_rows[:MAX_RESULT_ROWS], "results_truncated": len(all_rows) > MAX_RESULT_ROWS,
        "compaction": {"status": "not_run", "reason": "outside_extraction_critical_path"},
        "limitations": ["semantic_quality_not_verified", "native_os_acceptance_pending",
                        "explicit_partial_recovery_only", "cold_switch_requires_operator_quiescence"],
    }


def health_view(vault) -> dict[str, Any]:
    """Read-only status of retained incremental control work, not semantic QA.

    No lock file, job launch, model, cleanup or journal recovery is performed.
    Concurrent edits may change this observation; it is not a switch permit.
    """
    state = _read_processed(vault.processed_state_path)
    runs = [load_run(state, key) for key in state.get(RUN_KEY, {})]
    works = [load_work(state, key) for key in state.get(COMMIT_KEY, {})]
    statuses = Counter(run["status"] for run in runs)
    pending_commits = sum(commit_result(work)["execution_status"] == "recovery_required" for work in works)
    details = [{"run_id": run["run_id"], "source": run["source"], "session_id": run["session_id"],
                "execution_status": run["status"], "code": run.get("code"),
                "reserved_requests": run["reserved_requests"]}
               for run in runs if run["status"] != "completed"]
    live = owner_live(state)
    from .query_progress import retention_inventory
    config = vault.config()
    process = config.get("process", {}) if isinstance(config, Mapping) else {}
    configured = process.get("automatic_pipeline", "incremental") if isinstance(process, Mapping) else None
    return {"retention_inventory": retention_inventory(vault),
            "configured_pipeline": configured,
            "engine": "incremental",
            "pipeline_ready": configured in (None, "incremental"),
            "configuration_code": "legacy_pipeline_removed" if configured == "legacy" else None,
            "incremental": {"retained_runs": len(runs), "retained_by_status": dict(statuses),
                            "owner_live": live, "pending_commits": pending_commits,
                            "unresolved_runs": details[:MAX_RESULT_ROWS],
                            "results_truncated": len(details) > MAX_RESULT_ROWS},
            "legacy_pending_plans": len(state.get("pending_turn_plans", {})),
            "read_only": True,
            "switch_permission": False,
            "limitations": ["retained_receipts_not_current_semantic_state", "not_an_os_process_barrier"]}
