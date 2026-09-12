"""Durable request-count state for one background extraction work item.

A detached worker can die after a provider request but before memleaf records a
normal model failure.  The process-job ID survives that worker restart, so use
it as the stable work identity and reserve each outbound single-pass request
*before* dispatch.  A restarted worker therefore cannot reopen the two-call
automatic budget for the same turn.

Older releases also recorded a wall-clock start. Those timestamps are accepted
for compatibility but never used to shorten requests or forbid a later commit.
Request counters, not elapsed time, remain authoritative across restarts.
Deterministic no-write turns never consult this model-request ledger.

This state contains only control identifiers, counters, and timestamps; never
prompts, responses, evidence bodies, credentials, or exception text.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Mapping

from .locking import atomic_write_json, read_json


_VERSION = 1
_PROCESS_JOB_VERSION = 1
_MAX_WORKS = 128
_MAX_TURNS_PER_WORK = 64


class ExtractionWorkStateError(RuntimeError):
    """The durable extraction authorization/budget ledger cannot be trusted safely."""


def _budget_path(vault: Any) -> Path:
    return vault.state_path / "extraction_request_budget.json"


def _process_jobs_path(vault: Any) -> Path:
    return vault.state_path / "process_jobs.json"


def _empty_state() -> dict[str, Any]:
    return {"version": _VERSION, "works": {}, "order": []}


def _valid_identifier(value: Any, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and "\x00" not in value
        and "\n" not in value
        and "\r" not in value
    )


def _valid_epoch(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _normalize_turn_state(value: Any) -> dict[str, Any]:
    """Accept the unreleased counter-only shape without reopening requests.

    Earlier builds on this development branch persisted ``turns[turn_id]`` as
    an integer request count.  Treat that as the same consumed request count
    with no historical wall-clock start. Existing timestamps are preserved for
    compatibility only; no request ordinal is reset and no expiry is inferred.
    """

    if type(value) is int and 0 <= value <= 2:
        return {"requests": value, "started_at_epoch": None}
    if not isinstance(value, Mapping):
        raise ExtractionWorkStateError("invalid extraction request budget counter")
    requests = value.get("requests")
    started = value.get("started_at_epoch")
    if type(requests) is not int or not 0 <= requests <= 2:
        raise ExtractionWorkStateError("invalid extraction request budget counter")
    if started is not None and not _valid_epoch(started):
        raise ExtractionWorkStateError("invalid extraction work start time")
    return {
        "requests": requests,
        "started_at_epoch": float(started) if started is not None else None,
    }


def _read_budget_state_unlocked(vault: Any) -> dict[str, Any]:
    path = _budget_path(vault)
    if not path.exists():
        return _empty_state()
    if path.is_symlink() or not path.is_file():
        raise ExtractionWorkStateError("unsafe extraction request budget state")
    try:
        value = read_json(path)
    except (OSError, UnicodeError, TypeError, ValueError) as error:
        # Do not clear/recreate damaged authorization/retry state: that would
        # silently reopen the provider request budget after corruption.
        raise ExtractionWorkStateError("cannot read extraction request budget state") from error
    if not isinstance(value, Mapping) or value.get("version") != _VERSION:
        raise ExtractionWorkStateError("invalid extraction request budget state")
    works = value.get("works")
    order = value.get("order")
    if not isinstance(works, Mapping) or not isinstance(order, list):
        raise ExtractionWorkStateError("invalid extraction request budget state")
    normalized_works: dict[str, dict[str, Any]] = {}
    for work_id, raw in works.items():
        if not _valid_identifier(work_id, maximum=200) or not isinstance(raw, Mapping):
            raise ExtractionWorkStateError("invalid extraction request budget work")
        turns = raw.get("turns")
        if not isinstance(turns, Mapping) or len(turns) > _MAX_TURNS_PER_WORK:
            raise ExtractionWorkStateError("invalid extraction request budget turns")
        normalized_turns: dict[str, dict[str, Any]] = {}
        for turn_id, turn_state in turns.items():
            if not _valid_identifier(turn_id, maximum=800):
                raise ExtractionWorkStateError("invalid extraction request budget turn id")
            normalized_turns[turn_id] = _normalize_turn_state(turn_state)
        normalized_works[work_id] = {"turns": normalized_turns}
    normalized_order = [
        item for item in order
        if _valid_identifier(item, maximum=200) and item in normalized_works
    ]
    if len(normalized_order) != len(set(normalized_order)) or set(normalized_order) != set(normalized_works):
        raise ExtractionWorkStateError("invalid extraction request budget order")
    if len(normalized_works) > _MAX_WORKS:
        raise ExtractionWorkStateError("extraction request budget state exceeds bound")
    return {"version": _VERSION, "works": normalized_works, "order": normalized_order}


def _work_unlocked(state: dict[str, Any], *, work_id: str) -> dict[str, Any]:
    works = state["works"]
    order = state["order"]
    work = works.get(work_id)
    if work is not None:
        if not isinstance(work, dict) or not isinstance(work.get("turns"), dict):
            raise ExtractionWorkStateError("invalid extraction request budget work")
        return work
    if len(works) >= _MAX_WORKS:
        oldest = next((item for item in order if item != work_id), None)
        if oldest is None:
            raise ExtractionWorkStateError("extraction request budget state is full")
        works.pop(oldest, None)
        order.remove(oldest)
    work = {"turns": {}}
    works[work_id] = work
    order.append(work_id)
    return work


def active_background_work_id(
    vault: Any,
    *,
    source: str | None,
    session_id: str | None,
) -> str | None:
    """Return the process-job ID owned by this exact worker, if any.

    Reading the process-job file is deliberately strict when it exists.  A
    damaged runtime ownership file must not be interpreted as "not a worker",
    because doing so would silently fall back to a fresh in-memory budget.
    """

    if source is None or session_id is None:
        return None
    path = _process_jobs_path(vault)
    with vault.lock():
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ExtractionWorkStateError("unsafe process job state")
        try:
            value = read_json(path)
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            raise ExtractionWorkStateError("cannot read process job state") from error
    if not isinstance(value, Mapping) or value.get("version") != _PROCESS_JOB_VERSION:
        raise ExtractionWorkStateError("invalid process job state version")
    jobs = value.get("jobs")
    order = value.get("order")
    active_id = value.get("active_job_id")
    if not isinstance(jobs, Mapping) or not isinstance(order, list):
        raise ExtractionWorkStateError("invalid process job state shape")

    seen: set[str] = set()
    for job_id in order:
        if not _valid_identifier(job_id, maximum=200) or job_id in seen or job_id not in jobs:
            raise ExtractionWorkStateError("invalid process job state order")
        if not isinstance(jobs.get(job_id), Mapping):
            raise ExtractionWorkStateError("invalid process job record")
        seen.add(job_id)
    if seen != set(jobs):
        raise ExtractionWorkStateError("process job state order does not cover all records")

    if active_id is None:
        return None
    if not _valid_identifier(active_id, maximum=200) or active_id not in jobs:
        raise ExtractionWorkStateError("invalid active process job")
    job = jobs.get(active_id)
    if not isinstance(job, Mapping):
        raise ExtractionWorkStateError("invalid active process job")
    if (
        job.get("status") not in {"starting", "running"}
        or job.get("owner_pid") != os.getpid()
        or job.get("source") != source
        or job.get("session_id") != session_id
    ):
        return None
    return active_id


def reserve_model_request(vault: Any, *, work_id: str, turn_id: str) -> int | None:
    """Atomically reserve the next provider request and return ordinal 1/2.

    ``None`` means this stable work+turn already consumed both requests.  The
    reservation happens before the outbound call, so a process kill after this
    write still consumes that attempt conservatively.
    """

    if not _valid_identifier(work_id, maximum=200):
        raise ExtractionWorkStateError("invalid extraction work id")
    if not _valid_identifier(turn_id, maximum=800):
        raise ExtractionWorkStateError("invalid extraction turn id")
    with vault.lock():
        state = _read_budget_state_unlocked(vault)
        work = _work_unlocked(state, work_id=work_id)
        turns = work["turns"]
        turn_state = turns.get(turn_id)
        if turn_state is None:
            if len(turns) >= _MAX_TURNS_PER_WORK:
                raise ExtractionWorkStateError("extraction work turn budget is full")
            # Keep the existing on-disk shape. A timestamp is not required for
            # request accounting and must not become a hidden expiry again.
            turn_state = {"requests": 0, "started_at_epoch": None}
            turns[turn_id] = turn_state
        else:
            turn_state = _normalize_turn_state(turn_state)
            turns[turn_id] = turn_state
        count = turn_state["requests"]
        if count >= 2:
            return None
        ordinal = count + 1
        turn_state["requests"] = ordinal
        atomic_write_json(_budget_path(vault), state, mode=0o600)
        return ordinal


def complete_turn_budget(vault: Any, *, work_id: str, turn_id: str) -> bool:
    """Best-effort removal after the turn crossed its durable commit boundary."""

    if not _valid_identifier(work_id, maximum=200) or not _valid_identifier(turn_id, maximum=800):
        return False
    try:
        with vault.lock():
            state = _read_budget_state_unlocked(vault)
            work = state["works"].get(work_id)
            if not isinstance(work, dict) or turn_id not in work.get("turns", {}):
                return False
            work["turns"].pop(turn_id, None)
            if not work["turns"]:
                state["works"].pop(work_id, None)
                state["order"] = [item for item in state["order"] if item != work_id]
            atomic_write_json(_budget_path(vault), state, mode=0o600)
            return True
    except (OSError, UnicodeError, TypeError, ValueError, ExtractionWorkStateError):
        # Permanent memory is already committed at this point.  Cleanup of a
        # conservative stale budget row must never turn a successful memory
        # commit into a reported failure.
        return False


__all__ = [
    "ExtractionWorkStateError",
    "active_background_work_id",
    "complete_turn_budget",
    "reserve_model_request",
]
