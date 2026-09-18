"""Opt-in, one-plus-one model dispatch on the shared incremental commit bridge.

Whole-response retry and explicit partial recovery share the same allowance.
Only partial recovery with actual changed context can replan unresolved sources;
accepted decisions are immutable. No legacy planner fallback exists.
"""
from __future__ import annotations

import json
from dataclasses import replace
import os
import uuid
from typing import Any

from .extraction_work_state import (
    ExtractionWorkStateError, extraction_work_id, reserve_model_request,
    complete_turn_budget, _read_budget_state_unlocked,
)
from .incremental_commit import equivalent_arguments
from .incremental_commit import _arguments, _window, apply_incremental, resume_incremental
from .incremental_journal import digest, load_work, public_result as commit_result
from .incremental_preview import _prepare_incremental_unlocked
from .incremental_prompts import INCREMENTAL_SYSTEM
from .incremental_protocol import compile_incremental, MAX_BYTES, PROTOCOL_VERSION, SEMANTIC_PROTOCOL
from .incremental_run_state import (
    VERSION, OWNER, TERMINAL, load_run, save_run, strip_payload, owner_live, public_result,
    register_owner, unregister_owner,
)
from .index import turn_key
from .inbox import captured_turn_selector
from .llm.base import ModelError
from .locking import atomic_write_json
from .models import utc_now
from .process_common import _read_processed
from .process_journal import ProcessJournal
from .recording_policy import recording_allowed
from .turn_plan import input_digest, turn_identity_key

RETRY_SYSTEM = "\n上次请求未获得可用 JSON 决议。按同一输入和原协议返回结果，不补造缺失事实。\n"
TRANSIENT = frozenset({"model_timeout", "model_rate_limited", "model_network_error", "model_invalid_response"})


def _protocol_digest():
    """Bind in-flight model bytes to the semantic compiler contract.

    Existing commit journals are already frozen operations and remain resumable.
    A pre-commit response from an older prompt/protocol must never be silently
    reinterpreted after an upgrade.
    """
    return digest({"wire": PROTOCOL_VERSION, "semantic": SEMANTIC_PROTOCOL, "system": INCREMENTAL_SYSTEM})


class IncrementalRunError(RuntimeError):
    """A local durability failure; the original run ID remains the recovery key."""
    def __init__(self, result: dict[str, Any]):
        super().__init__("incremental execution interrupted; resume the original run_id")
        self.result = result
        self.recovery_required = True


def _read(service):
    return _read_processed(service.vault.processed_state_path)


def _guard_legacy(service, processed):
    journal = ProcessJournal(service)
    if any(isinstance(s, dict) and journal._processing_marker_live(s.get("processing"), utc_now())
           for s in processed.get("sessions", {}).values()):
        raise ValueError("legacy_processing_busy")


def _check_sources(service, processed, run):
    if run.get("partial_used"):
        from .incremental_partial import recheck_snapshot
        return recheck_snapshot(service, processed, run)
    if not recording_allowed(processed, run["source"], run["session_id"], run["turn_key"]):
        raise ValueError("source_recording_revoked")
    turn, window = _window(service, run["source"], run["session_id"], run["turn_key"])
    if window != run["source_window"] or input_digest(turn) != run["source_digest"]:
        raise ValueError("source_or_context_changed")
    snapshot = _prepare_incremental_unlocked(service, retention_request=run.get("retention_request"), **run["arguments"])
    if snapshot.snapshot_id != run["snapshot_id"]:
        raise ValueError("stale_planning_snapshot")
    return snapshot


def _budget_count(service, run):
    state = _read_budget_state_unlocked(service.vault)
    row = state["works"].get(run["budget_id"], {}).get("turns", {}).get(run["turn_budget_id"])
    return row["requests"] if row else 0


def _finish(service, processed, run, status, code=None):
    run.update(status=status, code=code)
    if status == "completed_with_unresolved" and "recovery_seed" in run and not run.get("partial_used"):
        run["partial_basis"] = run.pop("recovery_seed")
    if status in TERMINAL:
        strip_payload(run)
    save_run(service, processed, run)


def run_incremental(service: Any, *, source: str, session_id: str, turn_id: str,
                    backend: Any = None, scope: Any = None, priority_memory_ids=(),
                    candidate_limit: int = 12, selection=None,
                    retention_request: str | None = None,
                    allow_new_scopes: bool = False, captured_turn_key: str | None = None) -> dict[str, Any]:
    """Explicitly process one captured turn; default host routes are unchanged."""
    from .incremental_selection import explicit_run_id, validate_request
    args = _arguments(source, session_id, turn_id, scope, priority_memory_ids, candidate_limit, allow_new_scopes, selection, captured_turn_key)
    selection = args.get("selection")
    if selection:
        retention_request = validate_request(retention_request, selection)
    with service.vault.lock():
        processed = _read(service)
        if owner_live(processed):
            raise ValueError("incremental_model_busy")
        _guard_legacy(service, processed)
        run = load_run(processed, explicit_run_id(source, session_id, selection)) if selection else None
        if run is not None and not equivalent_arguments(args, run["arguments"]):
            raise ValueError("incremental_run_arguments_changed")
        if run is None:
            turn, window = _window(service, source, session_id, captured_turn_selector(turn_id, captured_turn_key))
            budget_turn = (replace(turn, events=tuple(e for e in turn.events if e.event_key in selection["source_refs"]))
                           if selection else turn)
            budget_id = extraction_work_id(budget_turn, request_kind="explicit_remember" if selection else "automatic",
                                           intent_id=selection["intent_id"] if selection else "automatic")
            identity = explicit_run_id(source, session_id, selection) if selection else "inc-run-" + budget_id.removeprefix("work-")
            run = load_run(processed, identity)
        if run is not None:
            if not equivalent_arguments(args, run["arguments"]):
                raise ValueError("incremental_run_arguments_changed")
        else:
            if not recording_allowed(processed, source, session_id, turn.turn_key):
                raise ValueError("source_recording_revoked")
            if turn_identity_key(source, session_id, turn.turn_key) in processed.get("pending_turn_plans", {}):
                raise ValueError("legacy_pending_plan")
            # Do not recapture an already consumed decision under a new pipeline.
            state = processed.get("sessions", {}).get(f"{source}/{session_id}", {})
            for entry in state.get("processed_turns", []):
                if (not selection and entry.get("turn_key") == turn.turn_key
                        and set(entry.get("event_keys", [])) == {e.event_key for e in turn.events}):
                    raise ValueError("source_already_processed")
            from .incremental_journal import KEY as COMMIT_KEY
            from .incremental_run_state import KEY as RUN_KEY
            # Previous source revisions can be coordinated after their owner is
            # terminal; a partial decision for this SAME revision is not retried.
            for key in processed.get(COMMIT_KEY, {}):
                prior = load_work(processed, key)
                if (prior["source"] == source and prior["session_id"] == session_id
                        and prior["turn_key"] == turn.turn_key
                        and ((not selection and prior.get("request_kind", "automatic") == "automatic"
                              and prior["source_digest"] == input_digest(turn))
                             or commit_result(prior)["execution_status"] == "recovery_required")):
                    raise ValueError("incremental_source_already_owned")
            for key in processed.get(RUN_KEY, {}):
                prior = load_run(processed, key)
                if (prior["source"] == source and prior["session_id"] == session_id
                        and prior["turn_key"] == turn.turn_key and prior["status"] not in TERMINAL):
                    raise ValueError("incremental_source_already_owned")
            snapshot = _prepare_incremental_unlocked(service, retention_request=retention_request, **args)
            request = {"system": INCREMENTAL_SYSTEM,
                       "user": json.dumps(snapshot.model_input(), ensure_ascii=False, separators=(",", ":"))}
            if sum(len(s.encode("utf-8")) for s in request.values()) + len(RETRY_SYSTEM.encode("utf-8")) > MAX_BYTES:
                raise ValueError("blocked_context")
            commit_intent = "dispatch-" + budget_id
            fields = ("message_id", "message_revision", "previous_message_revision", "source_sequence",
                      "previous_message_id", "source_time", "final")
            run = {"version": VERSION, "run_id": identity, "status": "ready", "arguments": args,
                   "source": source, "session_id": session_id, "turn_key": turn.turn_key,
                   "budget_id": budget_id, "turn_budget_id": f"{source}/{session_id}/{turn.turn_key}",
                   "legacy_source_unchanged": all(getattr(e, k, None) is None for e in turn.events for k in fields),
                   "source_digest": input_digest(turn), "source_window": window, "snapshot_id": snapshot.snapshot_id,
                   "request": request, "request_digest": digest(request), "protocol_digest": _protocol_digest(),
                   "source_keys": [e["event_key"] for e in snapshot.state()["evidence"]],
                   "target_ids": [t["memory"]["memory_id"] for t in snapshot.state()["targets"].values()],
                   "native_comparison": {"status": "available" if snapshot.state().get("native_guard") else "no_eligible_sources",
                                         "selected_fragments": sum("native" in t for t in snapshot.state()["targets"].values()),
                                         "selection": "bounded_candidates", "read_only": True},
                   "commit_intent": commit_intent,
                   "commit_work_id": "inc-" + digest([source, session_id, turn.turn_key, commit_intent]),
                   "attempts": [], "reserved_requests": 0, "budget_finalized": False}
            if selection:
                run.update(request_kind="explicit_remember", retention_request=retention_request,
                           authorization_intent=selection["intent_id"])
            save_run(service, processed, run)
    return resume_incremental_run(service, run["run_id"], backend=backend)


def _owned(service, run_id, token):
    processed = _read(service)
    run = load_run(processed, run_id)
    if run is None:
        raise ValueError("incremental_run_not_found")
    if run["status"] not in TERMINAL and processed.get(OWNER, {}).get("token") != token:
        raise ValueError("incremental_owner_changed")
    return processed, run


def _drive(service, run_id, token, backend, calls):
    while True:
        with service.vault.lock():
            processed, run = _owned(service, run_id, token)
            if run["status"] in TERMINAL:
                return public_result(run, calls=calls[0])
            stored_commit = load_work(processed, run["commit_work_id"])
            phase = "resume_commit" if stored_commit is not None else "dispatch"
            if stored_commit is None and run.get("protocol_digest") != _protocol_digest():
                _finish(service, processed, run, "blocked", "protocol_upgrade_required")
                return public_result(run, calls=calls[0])
            if stored_commit is None:
                try:
                    snapshot = _check_sources(service, processed, run)
                except (OSError, ValueError):
                    _finish(service, processed, run, "blocked", "source_or_snapshot_changed")
                    return public_result(run, calls=calls[0])
                if run["status"] in {"response_ready", "committing"}:
                    try:
                        compile_incremental(run["response"], snapshot)
                        if not run.get("partial_used"):
                            from .incremental_recovery import seed
                            run["recovery_seed"] = seed(snapshot, run["response"])
                    except ValueError:
                        if run.get("partial_used"):
                            _finish(service, processed, run, "failed", "invalid_partial_response")
                            return public_result(run, calls=calls[0])
                        run["attempts"][-1]["outcome"] = "invalid_response"
                        run.pop("response", None)
                        _finish(service, processed, run, "retryable", "invalid_model_response")
                        continue
                    run["status"] = "committing"
                    save_run(service, processed, run)
                    phase = "apply_commit"
                elif backend is None or getattr(backend, "single_pass_safe", False) is not True or not callable(getattr(backend, "complete", None)):
                    code = "backend_required" if backend is None else "backend_not_single_dispatch"
                    _finish(service, processed, run, "retryable", code)
                    return public_result(run, calls=calls[0])
        # Public commit and budget functions own their locks. Never nest the
        # non-reentrant OS file lock. The persistent owner fences other runners;
        # commit's guard/CAS still rechecks concurrent manual edits and Forget.
        if phase in {"resume_commit", "apply_commit"}:
            try:
                if phase == "resume_commit":
                    result = resume_incremental(service, run["commit_work_id"], _run_guard=(run_id, token))
                else:
                    result = apply_incremental(service, response=run["response"], expected_snapshot=run["snapshot_id"],
                                               intent_id=run["commit_intent"], _run_guard=(run_id, token),
                                               retention_request=run.get("retention_request"), **run["arguments"])
            except ValueError:
                with service.vault.lock():
                    processed, run = _owned(service, run_id, token)
                    if run["status"] not in TERMINAL:
                        _finish(service, processed, run, "blocked", "commit_precondition_changed")
                    return public_result(run, calls=calls[0])
            with service.vault.lock():
                processed, run = _owned(service, run_id, token)
                if run["status"] not in TERMINAL:
                    run["commit_result"] = result
                    _finish(service, processed, run, result["execution_status"])
            return public_result(run, calls=calls[0])
        try:
            with service.vault.lock():
                # A missing/regressed budget must not reopen dispatch when this
                # independent receipt already proves consumed reservations.
                if _budget_count(service, run) < run["reserved_requests"]:
                    raise ExtractionWorkStateError("request budget lost committed consumption")
            ordinal = reserve_model_request(service.vault, work_id=run["budget_id"], turn_id=run["turn_budget_id"],
                                            request_limit=2,
                                            legacy_turn_id=(None if run.get("request_kind") == "explicit_remember" else run["turn_budget_id"]),
                                            legacy_source_unchanged=run["legacy_source_unchanged"])
        except ExtractionWorkStateError:
            with service.vault.lock():
                processed, run = _owned(service, run_id, token)
                if run["status"] not in TERMINAL:
                    _finish(service, processed, run, "blocked", "budget_state_or_migration_required")
                return public_result(run, calls=calls[0])
        with service.vault.lock():
            processed, run = _owned(service, run_id, token)
            if run["status"] in TERMINAL:
                return public_result(run, calls=calls[0])
            run["reserved_requests"] = _budget_count(service, run)
            if ordinal is None:
                _finish(service, processed, run, "failed", "request_budget_exhausted")
                return public_result(run, calls=calls[0])
            # A reserve may precede a crash or a cancellation. Conservatively
            # consume it, but never label it confirmed provider billing.
            run["attempts"].append({"ordinal": ordinal, "outcome": "unknown"})
            try:
                _check_sources(service, processed, run)
            except (OSError, ValueError):
                _finish(service, processed, run, "blocked", "source_or_snapshot_changed")
                return public_result(run, calls=calls[0])
            run["status"] = "dispatching"
            save_run(service, processed, run)
            request = dict(run["request"])
            if ordinal > 1 and not run.get("partial_used"):
                request["system"] += RETRY_SYSTEM
        error_code = None
        response = None
        try:
            calls[0] += 1
            response = backend.complete(request["user"], system=request["system"], purpose="single_pass")
        except ModelError as error:
            error_code = error.code
        except Exception:
            error_code = "model_failed"  # Never persist provider exception text.
        with service.vault.lock():
            processed, run = _owned(service, run_id, token)
            if run["status"] in TERMINAL:
                return public_result(run, calls=calls[0])
            if error_code is not None:
                run["attempts"][-1]["outcome"] = error_code
                retryable = error_code in TRANSIENT and run["reserved_requests"] < 2
                _finish(service, processed, run, "retryable" if retryable else "failed", error_code)
                return public_result(run, calls=calls[0])
            if not isinstance(response, str) or not response.strip() or len(response.encode("utf-8")) > MAX_BYTES:
                run["attempts"][-1]["outcome"] = "invalid_response"
                if run.get("partial_used"):
                    _finish(service, processed, run, "failed", "invalid_partial_response")
                    return public_result(run, calls=calls[0])
                _finish(service, processed, run, "retryable", "invalid_response_size")
                continue
            run["attempts"][-1]["outcome"] = "response"
            run.update(status="response_ready", response=response)
            save_run(service, processed, run)


def resume_incremental_run(service: Any, run_id: str, *, backend: Any = None) -> dict[str, Any]:
    """Continue a stored response/commit first. Transport retries are explicit."""
    calls = [0]
    token = uuid.uuid4().hex
    run = None
    register_owner(token)
    try:
        with service.vault.lock():
            processed = _read(service)
            run = load_run(processed, run_id)
            if run is None:
                raise ValueError("incremental_run_not_found")
            if run["status"] not in TERMINAL:
                if owner_live(processed):
                    raise ValueError("incremental_model_busy")
                _guard_legacy(service, processed)
                processed[OWNER] = {"run_id": run_id, "token": token, "pid": os.getpid()}
                save_run(service, processed, run)
        result = public_result(run) if run["status"] in TERMINAL else _drive(service, run_id, token, backend, calls)
    except OSError as error:
        with service.vault.lock():
            durable = load_run(_read(service), run_id)
        if durable is None:
            raise
        raise IncrementalRunError(public_result(durable, calls=calls[0])) from error
    finally:
        try:
            with service.vault.lock():
                processed = _read(service)
                if processed.get(OWNER, {}).get("token") == token:
                    processed.pop(OWNER, None)
                    try:
                        atomic_write_json(service.vault.processed_state_path, processed)
                    except OSError as error:
                        durable = load_run(_read(service), run_id)
                        if durable is None:
                            raise
                        raise IncrementalRunError(public_result(durable, calls=calls[0])) from error
        finally:
            unregister_owner(token)
    # Budget finalization is derived control work. Its failure does not
    # invalidate committed memory; cached replay attempts it again, never an LLM.
    if result["execution_status"] == "completed":
        finalized = complete_turn_budget(service.vault, work_id=run["budget_id"], turn_id=run["turn_budget_id"])
        with service.vault.lock():
            processed = _read(service)
            run = load_run(processed, run_id)
            if run["status"] == "completed" and finalized and not run.get("budget_finalized", False):
                run["budget_finalized"] = True
                try:
                    save_run(service, processed, run)
                except OSError as error:
                    raise IncrementalRunError(result) from error
            return public_result(run, calls=calls[0])
    return result
