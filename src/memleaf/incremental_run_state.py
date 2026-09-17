"""Bounded control receipts for the opt-in incremental model runner.

Runtime receipts are not a second memory store. They retain an in-flight request
and response only until commit recovery settles, or explicit cancellation erases
it. The existing source-work ledger remains the authority for request allowance.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any

from .incremental_journal import canonical, digest
from .locking import atomic_write_json
from .validation import parse_strict_json

KEY = "incremental_runs"
OWNER = "incremental_run_owner"
VERSION = 1
MAX_RUNS = 128
MAX_RUN_BYTES = 1024 * 1024
MAX_LEDGER_BYTES = 16 * 1024 * 1024
TERMINAL = frozenset({"completed", "completed_with_unresolved", "blocked", "failed", "cancelled"})
_ACTIVE_TOKENS: set[str] = set()
STATES = TERMINAL | {"ready", "dispatching", "response_ready", "committing", "retryable"}


def valid_run_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"inc-run-[0-9a-f]{64}", value) is not None


def load_run(processed: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    if not valid_run_id(run_id):
        raise ValueError("invalid_incremental_run_id")
    runs = processed.get(KEY, {})
    if not isinstance(runs, dict) or len(runs) > MAX_RUNS:
        raise ValueError("invalid_incremental_runs")
    wrapper = runs.get(run_id)
    if wrapper is None:
        return None
    if (not isinstance(wrapper, dict) or type(wrapper.get("version")) is not int
            or wrapper["version"] != VERSION or not isinstance(wrapper.get("payload"), str)):
        raise ValueError("unsupported_incremental_run")
    payload = wrapper["payload"]
    if (len(payload.encode("utf-8")) > MAX_RUN_BYTES
            or hashlib.sha256(payload.encode("utf-8")).hexdigest() != wrapper.get("checksum")):
        raise ValueError("invalid_incremental_run_checksum")
    run = parse_strict_json(payload)
    if (not isinstance(run, dict) or run.get("run_id") != run_id or type(run.get("version")) is not int
            or run["version"] != VERSION or not isinstance(run.get("status"), str) or run["status"] not in STATES
            or not isinstance(run.get("arguments"), dict)
            or any(not isinstance(run.get(k), str) or not run[k] for k in
                   ("budget_id", "turn_budget_id", "source", "session_id", "turn_key", "snapshot_id", "source_digest", "source_window", "commit_intent", "commit_work_id"))
            or not isinstance(run.get("source_keys"), list) or not isinstance(run.get("target_ids"), list)
            or any(not isinstance(v, str) for k in ("source_keys", "target_ids") for v in run[k])
            or type(run.get("legacy_source_unchanged")) is not bool
            or not isinstance(run.get("attempts"), list) or len(run["attempts"]) > 2
            or type(run.get("reserved_requests")) is not int or run["reserved_requests"] < 0):
        raise ValueError("invalid_incremental_run")
    ordinals = [a.get("ordinal") for a in run["attempts"] if isinstance(a, dict)]
    if (any(type(v) is not int for v in ordinals) or ordinals != sorted(set(ordinals))
            or type(run.get("budget_finalized", False)) is not bool):
        raise ValueError("invalid_incremental_attempts")
    for attempt in run["attempts"]:
        if (not isinstance(attempt, dict) or type(attempt.get("ordinal")) is not int
                or not 1 <= attempt["ordinal"] <= 2 or attempt.get("outcome") not in
                ("unknown", "response", "invalid_response", "model_timeout", "model_rate_limited", "model_network_error", "model_failed", "model_auth_failed", "model_unavailable", "model_http_error", "model_invalid_response")):
            raise ValueError("invalid_incremental_attempt")
    if run["status"] not in TERMINAL:
        request = run.get("request")
        if (not isinstance(request, dict) or set(request) != {"system", "user"}
                or any(not isinstance(v, str) for v in request.values())
                or digest(request) != run.get("request_digest")):
            raise ValueError("invalid_incremental_request")
    if "response" in run and not isinstance(run["response"], str):
        raise ValueError("invalid_incremental_response")
    return run


def save_run(service: Any, processed: dict[str, Any], run: dict[str, Any]) -> None:
    payload = canonical(run)
    if len(payload.encode("utf-8")) > MAX_RUN_BYTES:
        raise ValueError("incremental_run_too_large")
    runs = processed.setdefault(KEY, {})
    if not isinstance(runs, dict):
        raise ValueError("invalid_incremental_runs")
    if run["run_id"] not in runs and len(runs) >= MAX_RUNS:
        raise ValueError("incremental_runs_full")
    runs[run["run_id"]] = {"version": VERSION, "payload": payload,
                           "checksum": hashlib.sha256(payload.encode("utf-8")).hexdigest()}
    if len(canonical(runs).encode("utf-8")) > MAX_LEDGER_BYTES:
        raise ValueError("incremental_runs_full")
    load_run(processed, run["run_id"])
    atomic_write_json(service.vault.processed_state_path, processed)


def strip_payload(run: dict[str, Any]) -> None:
    for key in ("request", "response"):
        run.pop(key, None)


def owner_live(processed: dict[str, Any]) -> bool:
    from .process_journal import ProcessJournal
    owner = processed.get(OWNER)
    if owner is None:
        return False
    if (not isinstance(owner, dict) or not valid_run_id(owner.get("run_id"))
            or not isinstance(owner.get("token"), str) or not owner["token"]
            or type(owner.get("pid")) is not int or owner["pid"] <= 0):
        raise ValueError("invalid_incremental_run_owner")
    # Never time-expire a known live process during an in-flight network call.
    # Unknown liveness is conservative; local PID reuse may need operator review.
    if owner["pid"] == os.getpid():
        return owner["token"] in _ACTIVE_TOKENS
    return ProcessJournal._owner_pid_status(owner["pid"]) is not False


def register_owner(token: str) -> None:
    _ACTIVE_TOKENS.add(token)


def unregister_owner(token: str) -> None:
    _ACTIVE_TOKENS.discard(token)


def assert_no_runner(processed: dict[str, Any]) -> None:
    if owner_live(processed):
        raise ValueError("incremental_model_busy")


def owned_turns(processed: dict[str, Any], source: str, session_id: str, *, protect: bool = False) -> set[str]:
    runs = processed.get(KEY, {})
    if not isinstance(runs, dict):
        raise ValueError("invalid_incremental_runs")
    result = set()
    for key in runs:
        run = load_run(processed, key)
        if run["source"] == source and run["session_id"] == session_id and (not protect or run["status"] != "completed"):
            result.add(run["turn_key"])
    return result


def public_result(run: dict[str, Any], *, calls: int = 0) -> dict[str, Any]:
    return {
        "run_id": run["run_id"], "execution_status": run["status"],
        "code": run.get("code"), "commit_work_id": run["commit_work_id"],
        "model_calls_this_invocation": calls,
        "reserved_requests": run["reserved_requests"], "request_limit": 2,
        "budget_finalized": run.get("budget_finalized", False),
        "unattributed_reservations": max(0, run["reserved_requests"] - len(run["attempts"])),
        "responses_observed": sum(a["outcome"] in {"response", "invalid_response"} for a in run["attempts"]),
        "uncertain_attempts": sum(a["outcome"] == "unknown" for a in run["attempts"]),
        "commit": run.get("commit_result"),
        "limitations": ["opt_in_captured_turn_only", "native_comparison_not_integrated",
                        "partial_replanning_not_enabled", "semantic_quality_not_verified"],
    }


def cancel_forgotten_unlocked(service: Any, processed: dict[str, Any], ids: set[str], source_keys: set[str]) -> None:
    runs = processed.get(KEY, {})
    if not isinstance(runs, dict):
        raise ValueError("invalid_incremental_runs")
    for key in list(runs):
        run = load_run(processed, key)
        committed_ids = {op.get("memory_id") for op in (run.get("commit_result") or {}).get("operations", [])}
        if (ids.intersection(run["target_ids"]) or ids.intersection(committed_ids)
                or source_keys.intersection(run["source_keys"])):
            run.update(status="cancelled", code="explicit_forget")
            strip_payload(run)
            run.pop("commit_result", None)
            # Keep an in-flight owner until its callback exits; a new request
            # must not overlap the still-running old request after cancellation.
            save_run(service, processed, run)
