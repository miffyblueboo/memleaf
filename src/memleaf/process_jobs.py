"""Durable, local background jobs for model-backed inbox processing.

The MCP process request must return before the model route completes.  Jobs are
kept in the vault's runtime state so a disconnected host can be retried and
inspected later.  A detached worker invokes the same public ``Memleaf.process``
operation used by the synchronous path; this module does not implement a
second processing pipeline.
"""

from __future__ import annotations

import argparse
import errno
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from .locking import atomic_write_json, read_json
from .service import Memleaf
from .vault import Vault, safe_component


_VERSION = 1
_MAX_QUEUE = 32
_MAX_RETAINED = 128
_MAX_ATTEMPTS_RETAINED = 32
_TERMINAL = {"succeeded", "failed", "deferred"}
_ACTIVE = {"starting", "running"}
_MODEL_METRIC_FIELDS = (
    "call_count",
    "retry_count",
    "failed_calls",
    "invalid_output_count",
    "request_duration_ms",
    "wall_clock_ms",
    "input_chars",
    "input_bytes",
    "output_chars",
    "output_bytes",
    "max_in_flight",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "reasoning_tokens",
    "cache_hit_calls",
)
_MODEL_METRIC_STAGES = frozenset({
    "gate",
    "summarize",
    "semantic_review",
    "coordination",
    "target_reconciliation",
    "single_pass",
    "other",
})
_MODEL_METRIC_OPERATIONS = frozenset(
    {f"{stage}_{suffix}" for stage in _MODEL_METRIC_STAGES for suffix in ("primary", "format_repair")}
    | {"gate_coverage_repair", "gate_semantic_retry"}
)
_MODEL_CALL_INT_FIELDS = frozenset({
    "call_index", "request_duration_ms", "input_chars", "input_bytes",
    "output_chars", "output_bytes", "prompt_tokens", "completion_tokens",
    "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
    "reasoning_tokens",
})
_MAX_MODEL_CALL_ROWS = 256
_THINKING_EFFECTIVE_MODES = frozenset({"provider_default", "unsupported", "disabled", "minimal", "low", "high", "max"})
_THINKING_CONTROLS = frozenset({
    "provider_default", "unsupported", "openai_reasoning_effort",
    "deepseek_thinking_effort", "anthropic_effort", "anthropic_adaptive_effort",
    "gemini_thinking_level", "gemini_thinking_budget",
})


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _state_path(vault: Vault) -> Path:
    return vault.state_path / "process_jobs.json"


def _empty_state() -> dict[str, Any]:
    return {"version": _VERSION, "jobs": {}, "order": [], "active_job_id": None}


def _valid_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != _VERSION:
        return _empty_state()
    jobs = value.get("jobs")
    order = value.get("order")
    if not isinstance(jobs, dict) or not isinstance(order, list):
        return _empty_state()
    order = [item for item in order if isinstance(item, str)]
    return {"version": _VERSION, "jobs": jobs, "order": order,
            "active_job_id": value.get("active_job_id")}


def _read_state(vault: Vault) -> dict[str, Any]:
    try:
        return _valid_state(read_json(_state_path(vault)))
    except (OSError, UnicodeError, TypeError, ValueError):
        return _empty_state()


def _write_state(vault: Vault, state: Mapping[str, Any]) -> None:
    atomic_write_json(_state_path(vault), dict(state), mode=0o600)


def _pid_alive(pid: Any) -> bool | None:
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        try:
            from .process_owner import windows_pid_status

            return windows_pid_status(pid)
        except (ImportError, OSError, ValueError):
            return None
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        if error.errno == errno.EPERM:
            return True
        return None
    return True


def _recover_dead_active(state: dict[str, Any]) -> bool:
    changed = False
    active_id = state.get("active_job_id")
    if not isinstance(active_id, str):
        for candidate_id, candidate in state.get("jobs", {}).items():
            if isinstance(candidate, dict) and candidate.get("status") in _ACTIVE:
                active_id = candidate_id
                state["active_job_id"] = candidate_id
                changed = True
                break
    job = state.get("jobs", {}).get(active_id) if isinstance(active_id, str) else None
    owner_alive = _pid_alive(job.get("owner_pid")) if isinstance(job, dict) else False
    if isinstance(job, dict) and job.get("status") in _ACTIVE and owner_alive is not False:
        return False
    if isinstance(job, dict) and job.get("status") in _ACTIVE:
        job.update({"status": "pending", "owner_pid": None, "started_at": None,
                    "recovered_at": _now()})
        changed = True
    if active_id is not None:
        state["active_job_id"] = None
        changed = True
    return changed


def _prune_terminal(state: dict[str, Any]) -> bool:
    """Bound the durable history while retaining all pending/active jobs."""
    jobs = state.get("jobs", {})
    order = state.get("order", [])
    terminal = [job_id for job_id in order if isinstance(jobs.get(job_id), dict) and jobs[job_id].get("status") in _TERMINAL]
    if len(terminal) <= _MAX_RETAINED:
        return False
    remove = set(terminal[: len(terminal) - _MAX_RETAINED])
    state["order"] = [job_id for job_id in order if job_id not in remove]
    for job_id in remove:
        jobs.pop(job_id, None)
    return True


def _safe_metric_bucket(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, int] = {}
    for key in _MODEL_METRIC_FIELDS:
        item = value.get(key)
        if type(item) is int and item >= 0:
            result[key] = item
    return result


def _safe_model_metrics(value: Any) -> dict[str, Any]:
    """Project structural-only telemetry; never retain prompt/response content."""

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    total = _safe_metric_bucket(value.get("total"))
    if total:
        result["total"] = total
    stages = value.get("stages")
    if isinstance(stages, Mapping):
        bounded: dict[str, dict[str, int]] = {}
        for stage, bucket in stages.items():
            if not isinstance(stage, str) or stage not in _MODEL_METRIC_STAGES:
                continue
            projected = _safe_metric_bucket(bucket)
            if projected:
                bounded[stage] = projected
        if bounded:
            result["stages"] = bounded
    operations = value.get("operations")
    if isinstance(operations, Mapping):
        bounded_operations: dict[str, dict[str, int]] = {}
        for operation, bucket in operations.items():
            if not isinstance(operation, str) or operation not in _MODEL_METRIC_OPERATIONS:
                continue
            projected = _safe_metric_bucket(bucket)
            if projected:
                bounded_operations[operation] = projected
        if bounded_operations:
            result["operations"] = bounded_operations
    calls = value.get("calls")
    if isinstance(calls, list):
        bounded_calls: list[dict[str, Any]] = []
        for raw in calls[:_MAX_MODEL_CALL_ROWS]:
            if not isinstance(raw, Mapping):
                continue
            stage = raw.get("stage")
            operation = raw.get("operation")
            if stage not in _MODEL_METRIC_STAGES or operation not in _MODEL_METRIC_OPERATIONS:
                continue
            row: dict[str, Any] = {"stage": stage, "operation": operation}
            for key in ("retry", "failed", "invalid_output"):
                if isinstance(raw.get(key), bool):
                    row[key] = raw[key]
            for key in _MODEL_CALL_INT_FIELDS:
                item = raw.get(key)
                if type(item) is int and item >= 0:
                    row[key] = item
            mode = raw.get("thinking_mode")
            if mode in {"default", "disabled", "low", "high", "max"}:
                row["thinking_mode"] = mode
            effective = raw.get("thinking_effective")
            if effective in _THINKING_EFFECTIVE_MODES:
                row["thinking_effective"] = effective
            control = raw.get("thinking_control")
            if control in _THINKING_CONTROLS:
                row["thinking_control"] = control
            bounded_calls.append(row)
        if bounded_calls:
            result["calls"] = bounded_calls
    return result


def _aggregate_metric_buckets(values: list[Mapping[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for field in _MODEL_METRIC_FIELDS:
        items = [value.get(field) for value in values if type(value.get(field)) is int and value.get(field) >= 0]
        if not items:
            continue
        result[field] = max(items) if field == "max_in_flight" else sum(items)
    return result


def _aggregate_model_metrics(values: list[Mapping[str, Any]]) -> dict[str, Any]:
    safe_values = [_safe_model_metrics(value) for value in values]
    safe_values = [value for value in safe_values if value]
    if not safe_values:
        return {}
    result: dict[str, Any] = {}
    totals = [value["total"] for value in safe_values if isinstance(value.get("total"), Mapping)]
    if totals:
        result["total"] = _aggregate_metric_buckets(totals)
    stage_names = sorted({
        stage
        for value in safe_values
        for stage in value.get("stages", {})
        if isinstance(value.get("stages"), Mapping) and stage in _MODEL_METRIC_STAGES
    })
    stages: dict[str, dict[str, int]] = {}
    for stage in stage_names:
        buckets = [
            value["stages"][stage]
            for value in safe_values
            if isinstance(value.get("stages"), Mapping)
            and isinstance(value["stages"].get(stage), Mapping)
        ]
        if buckets:
            stages[stage] = _aggregate_metric_buckets(buckets)
    if stages:
        result["stages"] = stages
    operation_names = sorted({
        operation
        for value in safe_values
        for operation in value.get("operations", {})
        if isinstance(value.get("operations"), Mapping) and operation in _MODEL_METRIC_OPERATIONS
    })
    operations: dict[str, dict[str, int]] = {}
    for operation in operation_names:
        buckets = [
            value["operations"][operation]
            for value in safe_values
            if isinstance(value.get("operations"), Mapping)
            and isinstance(value["operations"].get(operation), Mapping)
        ]
        if buckets:
            operations[operation] = _aggregate_metric_buckets(buckets)
    if operations:
        result["operations"] = operations
    calls: list[dict[str, Any]] = []
    for value in safe_values:
        rows = value.get("calls")
        if not isinstance(rows, list):
            continue
        for raw in rows:
            if isinstance(raw, Mapping) and len(calls) < _MAX_MODEL_CALL_ROWS:
                row = dict(raw)
                row["call_index"] = len(calls) + 1
                calls.append(row)
    if calls:
        result["calls"] = calls
    return result


def _safe_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in (
        "processed_turns", "memories_written", "metadata_merged", "cleaned_turns",
        "deferred_candidates", "deferred_inbox_turns", "pending_inbox_turns",
        "unresolved_evidence_count", "retryable_deferred_turns",
    ):
        item = value.get(key)
        if type(item) is int and item >= 0:
            result[key] = item
    ids = value.get("memory_ids")
    if isinstance(ids, list):
        result["memory_ids"] = [item[:200] for item in ids if isinstance(item, str)][:100]
    model_metrics = _safe_model_metrics(value.get("model_metrics"))
    if model_metrics:
        result["model_metrics"] = model_metrics
    for key in ("coverage_status", "external_evidence_status"):
        if isinstance(value.get(key), str):
            result[key] = value[key][:80]
    external = value.get("external_evidence")
    if isinstance(external, Mapping):
        detail: dict[str, Any] = {}
        if isinstance(external.get("status"), str):
            detail["status"] = external["status"][:80]
        for key in (
            "external_record_count", "retained_body_count", "retained_body_bytes",
            "metadata_only_record_count", "incomplete_record_count", "unusable_record_count",
        ):
            item = external.get(key)
            if type(item) is int and item >= 0:
                detail[key] = item
        policy = external.get("capture_policy")
        if isinstance(policy, Mapping):
            bounded_policy = {}
            for key in ("tool_evidence_mode", "include_attachments", "body_retention"):
                if isinstance(policy.get(key), (str, bool)):
                    bounded_policy[key] = policy[key]
            if bounded_policy:
                detail["capture_policy"] = bounded_policy
        if detail:
            result["external_evidence"] = detail
    coverage = value.get("coverage")
    if isinstance(coverage, Mapping):
        result["coverage"] = {
            key: coverage[key] for key in ("status", "reason", "source", "session_id")
            if isinstance(coverage.get(key), (str, int, bool))
        }
    return result


def _result_status(result: Mapping[str, Any]) -> str:
    deferred = any(
        type(result.get(key)) is int and result.get(key, 0) > 0
        for key in (
            "deferred_candidates", "deferred_inbox_turns", "pending_inbox_turns",
            "unresolved_evidence_count",
        )
    )
    if result.get("coverage_status") in {"partial", "deferred", "unavailable"}:
        deferred = True
    coverage = result.get("coverage")
    if isinstance(coverage, Mapping) and coverage.get("status") in {"partial", "deferred", "unavailable"}:
        deferred = True
    return "deferred" if deferred else "succeeded"


def _aggregate_attempt_results(attempts: list[Any]) -> dict[str, Any]:
    """Combine per-run counters while keeping each run in ``attempts``."""
    aggregate: dict[str, Any] = {}
    numeric = (
        "processed_turns", "memories_written", "metadata_merged", "cleaned_turns",
        "deferred_candidates", "deferred_inbox_turns", "pending_inbox_turns",
        "unresolved_evidence_count", "retryable_deferred_turns",
    )
    for key in numeric:
        total = 0
        seen = False
        for attempt in attempts:
            value = attempt.get("result", {}).get(key) if isinstance(attempt, Mapping) else None
            if type(value) is int and value >= 0:
                total += value
                seen = True
        if seen:
            aggregate[key] = total
    ids: list[str] = []
    for attempt in attempts:
        values = attempt.get("result", {}).get("memory_ids") if isinstance(attempt, Mapping) else None
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str) and value not in ids and len(ids) < 100:
                ids.append(value)
    if ids:
        aggregate["memory_ids"] = ids
    metric_values: list[Mapping[str, Any]] = []
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        for field in ("result", "error"):
            container = attempt.get(field)
            if not isinstance(container, Mapping):
                continue
            value = container.get("model_metrics")
            if isinstance(value, Mapping):
                metric_values.append(value)
    metrics = _aggregate_model_metrics(metric_values)
    if metrics:
        aggregate["model_metrics"] = metrics
    # The terminal result's coverage status is authoritative for the final
    # run; counters above intentionally retain the work done by earlier runs.
    for key in ("coverage_status", "external_evidence_status"):
        for attempt in reversed(attempts):
            value = attempt.get("result", {}).get(key) if isinstance(attempt, Mapping) else None
            if isinstance(value, str):
                aggregate[key] = value
                break
    return aggregate


def _safe_error(error: BaseException) -> dict[str, Any]:
    message = str(error).replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    result: dict[str, Any] = {"type": type(error).__name__[:80], "message": message[:500]}
    # Retain code locations even when the detached worker has no stderr. Never
    # serialize source lines, absolute paths, locals, or model response bodies.
    frames: list[str] = []
    trace = error.__traceback__
    package = Path(__file__).parent.resolve()
    while trace is not None:
        code = trace.tb_frame.f_code
        try:
            relative = Path(code.co_filename).resolve().relative_to(package)
        except ValueError:
            pass
        else:
            frames.append(f"{relative.as_posix()}:{trace.tb_lineno}:{code.co_name}")
        trace = trace.tb_next
    if frames:
        result["code_locations"] = " > ".join(frames[-12:])[:2000]
    model_metrics = _safe_model_metrics(getattr(error, "model_metrics", None))
    if model_metrics:
        result["model_metrics"] = model_metrics
    return result


def _launch(vault: Vault, job_id: str) -> subprocess.Popen[Any]:
    command = [sys.executable, "-m", "memleaf.process_jobs", "--vault", str(vault.root), "--job-id", job_id]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def _reap(process: subprocess.Popen[Any]) -> None:
    try:
        process.wait()
    except (OSError, ValueError):
        pass


def _dispatch(vault: Vault, state: dict[str, Any]) -> None:
    """Start the oldest pending job, recording the owner before returning."""
    if state.get("active_job_id"):
        return
    jobs = state.get("jobs", {})
    for job_id in list(state.get("order", [])):
        job = jobs.get(job_id)
        if not isinstance(job, dict) or job.get("status") != "pending":
            continue
        job["status"] = "starting"
        job["started_at"] = _now()
        job["owner_pid"] = None
        state["active_job_id"] = job_id
        _write_state(vault, state)
        try:
            process = _launch(vault, job_id)
        except Exception as error:
            job.update({"status": "failed", "finished_at": _now(), "owner_pid": None,
                        "error": _safe_error(error)})
            state["active_job_id"] = None
            _write_state(vault, state)
            continue
        # Detached workers have their own lifecycle, but the launching
        # process still owns a Popen handle. Reap it asynchronously so a host
        # shutdown does not emit a ResourceWarning or retain a zombie.
        threading.Thread(target=_reap, args=(process,), daemon=True).start()
        # The detached worker can finish before Popen returns (for an empty
        # inbox). Re-read before recording the PID so a terminal worker result
        # is never overwritten by the enqueueing process.
        current = _read_state(vault)
        current_job = current.get("jobs", {}).get(job_id)
        if isinstance(current_job, dict) and current.get("active_job_id") == job_id and current_job.get("status") == "starting":
            current_job.update({"status": "running", "owner_pid": process.pid})
            _write_state(vault, current)
        return


def enqueue(vault_path: Path | str, *, source: str, session_id: str, scope: Any = None) -> dict[str, Any]:
    """Accept one process request and return immediately with a job identity."""
    source = safe_component(str(source), "source")
    session_id = safe_component(str(session_id), "session id")
    vault = Vault(vault_path)
    with vault.lock():
        state = _read_state(vault)
        recovered = _recover_dead_active(state)
        pruned = _prune_terminal(state)
        if recovered or pruned:
            _write_state(vault, state)
        jobs = state["jobs"]
        for job_id in reversed(state["order"]):
            job = jobs.get(job_id)
            if not isinstance(job, dict) or job.get("source") != source or job.get("session_id") != session_id:
                continue
            if job.get("status") in _ACTIVE:
                job["rerun_requested"] = True
                job["updated_at"] = _now()
                _write_state(vault, state)
                _dispatch(vault, state)
                return {"accepted": True, "completed": False, "status": "pending", "job_id": job_id,
                        "rerun_requested": True}
            if job.get("status") == "pending":
                _dispatch(vault, state)
                return {"accepted": True, "completed": False, "status": "pending", "job_id": job_id}
        pending_count = sum(1 for value in jobs.values() if isinstance(value, dict) and value.get("status") == "pending")
        if pending_count >= _MAX_QUEUE:
            return {"accepted": False, "completed": False, "status": "deferred", "job_id": None,
                    "reason": "queue_full"}
        job_id = f"job-{uuid.uuid4().hex}"
        job = {
            "job_id": job_id, "source": source, "session_id": session_id,
            "scope": scope, "status": "pending", "accepted_at": _now(),
            "updated_at": _now(), "owner_pid": None, "rerun_requested": False,
        }
        jobs[job_id] = job
        state["order"].append(job_id)
        _write_state(vault, state)
        _dispatch(vault, state)
        return {"accepted": True, "completed": False, "status": "pending", "job_id": job_id}


def status(vault_path: Path | str, *, job_id: str) -> dict[str, Any]:
    vault = Vault(vault_path)
    with vault.lock():
        state = _read_state(vault)
        job = state.get("jobs", {}).get(job_id)
        if not isinstance(job, Mapping):
            raise ValueError("unknown process job")
        result = dict(job)
        # Status is deliberately a read-only projection. A dead owner is
        # shown as recoverable pending work; the next enqueue performs the
        # actual state transition and dispatches a worker.
        active_id = state.get("active_job_id")
        if active_id == job_id and result.get("status") in _ACTIVE and _pid_alive(result.get("owner_pid")) is False:
            result["status"] = "pending"
            result["owner_pid"] = None
            result["recovery_pending"] = True
        result["accepted"] = True
        result["completed"] = result.get("status") in _TERMINAL
        return result


def _finish(vault: Vault, job_id: str, *, status_value: str, result: Any = None, error: BaseException | None = None) -> bool:
    with vault.lock():
        state = _read_state(vault)
        job = state.get("jobs", {}).get(job_id)
        if not isinstance(job, dict):
            return False
        finished_at = _now()
        attempt: dict[str, Any] = {"status": status_value, "finished_at": finished_at}
        if result is not None:
            attempt["result"] = _safe_result(result)
        if error is not None:
            attempt["error"] = _safe_error(error)
        attempts = job.get("attempts")
        if not isinstance(attempts, list):
            attempts = []
        attempts.append(attempt)
        job["attempts"] = attempts[-_MAX_ATTEMPTS_RETAINED:]
        job["attempt_count"] = len(attempts)
        if job.get("rerun_requested"):
            job["rerun_requested"] = False
            job["status"] = "running"
            job["updated_at"] = _now()
            job["owner_pid"] = os.getpid()
            job["attempt"] = int(job.get("attempt", 0) or 0) + 1
            job["aggregate_result"] = _aggregate_attempt_results(job["attempts"])
            _write_state(vault, state)
            return True
        job.update({"status": status_value, "finished_at": _now(), "updated_at": _now(), "owner_pid": None})
        if result is not None:
            job["last_result"] = _safe_result(result)
            job["aggregate_result"] = _aggregate_attempt_results(job["attempts"])
            job["result"] = dict(job["aggregate_result"])
        if error is not None:
            job["error"] = _safe_error(error)
            job["aggregate_result"] = _aggregate_attempt_results(job["attempts"])
        state["active_job_id"] = None
        _prune_terminal(state)
        _write_state(vault, state)
        _dispatch(vault, state)
        return False


def run_worker(vault_path: Path | str, job_id: str) -> int:
    vault = Vault(vault_path)
    with vault.lock():
        state = _read_state(vault)
        job = state.get("jobs", {}).get(job_id)
        if not isinstance(job, dict) or job.get("status") not in _ACTIVE:
            return 0
        job.update({"status": "running", "owner_pid": os.getpid(), "updated_at": _now()})
        _write_state(vault, state)
    while True:
        try:
            service = Memleaf(vault)
            result = service.process(source=job["source"], session_id=job["session_id"], scope=job.get("scope"))
            terminal = _result_status(result if isinstance(result, Mapping) else {})
            rerun = _finish(vault, job_id, status_value=terminal, result=result)
        except Exception as error:
            rerun = _finish(vault, job_id, status_value="failed", error=error)
        if not rerun:
            return 0
        with vault.lock():
            state = _read_state(vault)
            job = state.get("jobs", {}).get(job_id)
            if not isinstance(job, dict):
                return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m memleaf.process_jobs")
    parser.add_argument("--vault", required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args(argv)
    return run_worker(args.vault, args.job_id)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
