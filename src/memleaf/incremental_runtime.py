"""Configured-model facade for the existing incremental runner.

There is one dispatcher, one run schema and one request allowance. This module
reconciles the G3c delivery's process_incremental API with the already committed
run_incremental/resume_incremental_run APIs; it never creates a parallel ledger.
"""
from __future__ import annotations

from typing import Any

from .extraction_work_state import extraction_work_id
from .incremental_commit import _arguments, _window
from .incremental_execution import run_incremental, resume_incremental_run
from .incremental_run_state import KEY, TERMINAL, load_run, public_result
from .index import turn_key
from .llm import ModelRouter, ModelUnavailable
from .process_common import _read_processed


def _resolve(service: Any, model: Any, router: Any) -> Any:
    if model is not None and router is not None:
        raise ValueError("ambiguous_model_route")
    backend = model if model is not None else router
    if backend is None:
        backend = ModelRouter.from_config(service.vault.config())
    if isinstance(backend, ModelRouter):
        if backend.mode not in {"api", "auto"} or (backend.mode == "auto" and backend.host is not None):
            raise ModelUnavailable("incremental planner requires a fixed API route")
        backend = backend.api
    if (backend is None or not callable(getattr(backend, "complete", None))
            or getattr(backend, "single_pass_safe", False) is not True):
        raise ModelUnavailable("backend must guarantee a single dispatch per complete call")
    return backend


def _receipt(result: dict[str, Any]) -> dict[str, Any]:
    # Retain the canonical runner result; aliases do not introduce new identities.
    return {**result, "retry_available": result["execution_status"] == "retryable",
            "reservations": result["reserved_requests"]}


def process_incremental(service: Any, *, source: str, session_id: str, turn_id: str,
                        scope: Any = None, priority_memory_ids=(), candidate_limit: int = 12,
                        model: Any = None, router: Any = None, recover: bool = False) -> dict[str, Any]:
    """Process a captured turn; retry transport failure only with recover=True.

    Saved responses and commit recovery need no configured model. The explicit
    run-ID API remains the unambiguous recovery route after source cleanup.
    """
    if type(recover) is not bool:
        raise ValueError("invalid_recover")
    if model is not None and router is not None:
        raise ValueError("ambiguous_model_route")
    args = _arguments(source, session_id, turn_id, scope, priority_memory_ids, candidate_limit, False)
    with service.vault.lock():
        processed = _read_processed(service.vault.processed_state_path)
        try:
            selected, _ = _window(service, source, session_id, turn_key(turn_id))
        except (OSError, ValueError):
            # Only a unique terminal receipt can stand in for cleaned source.
            matches = [load_run(processed, key) for key in processed.get(KEY, {})]
            matches = [r for r in matches if r["arguments"] == args]
            if len(matches) != 1 or matches[0]["status"] not in TERMINAL:
                raise ValueError("source_not_complete_or_ambiguous_receipt") from None
            run = matches[0]
        else:
            budget_id = extraction_work_id(selected, request_kind="automatic", intent_id="automatic")
            run = load_run(processed, "inc-run-" + budget_id.removeprefix("work-"))
            if run is not None and run["arguments"] != args:
                raise ValueError("incremental_run_arguments_changed")
        if run is not None and run["status"] == "retryable" and not recover:
            return _receipt(public_result(run))
        stored_commit = run is not None and run["commit_work_id"] in processed.get("incremental_commits", {})
        needs_model = run is None or (run["status"] not in TERMINAL | {"response_ready", "committing"}
                                    and not stored_commit)
    backend = _resolve(service, model, router) if needs_model else None
    if run is not None:
        result = resume_incremental_run(service, run["run_id"], backend=backend)
    else:
        result = run_incremental(service, source=source, session_id=session_id, turn_id=turn_id,
                                 scope=scope, priority_memory_ids=args["priority_memory_ids"],
                                 candidate_limit=candidate_limit, backend=backend)
    return _receipt(result)
