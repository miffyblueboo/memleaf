"""Durable provider-request accounting for source revisions and intent.

Synchronous and background callers share work identity. Reservations precede
actual dispatch; an uncertain process exit conservatively consumes the slot.
Legacy job-keyed counters can be migrated only for a matching legacy source
turn. Time does not expire or reopen request authority. This ledger has no
message bodies, prompts, credentials, or exception text.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .locking import atomic_write_json, read_json
from .extraction_budget import MAX_MODEL_REQUESTS
from .validation import parse_strict_json


_VERSION = 1
_RETIRED_VERSION = 2
_MAX_RETIRED_WORKS = 4096
_MAX_BUDGET_BYTES = 8 * 1024 * 1024
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

    if type(value) is int and 0 <= value <= 1_000_000:
        return {
            "requests": value,
            "started_at_epoch": None,
            "request_limit_at_creation": max(value, MAX_MODEL_REQUESTS),
            "completed": False,
        }
    if not isinstance(value, Mapping):
        raise ExtractionWorkStateError("invalid extraction request budget counter")
    requests = value.get("requests")
    started = value.get("started_at_epoch")
    if type(requests) is not int or not 0 <= requests <= 1_000_000:
        raise ExtractionWorkStateError("invalid extraction request budget counter")
    if started is not None and not _valid_epoch(started):
        raise ExtractionWorkStateError("invalid extraction work start time")
    stored_limit = value.get("request_limit_at_creation", max(requests, MAX_MODEL_REQUESTS))
    if type(stored_limit) is not int or stored_limit < 1 or stored_limit > 1_000_000:
        raise ExtractionWorkStateError("invalid extraction request budget limit")
    completed = value.get("completed", False)
    if not isinstance(completed, bool):
        raise ExtractionWorkStateError("invalid extraction request budget completion")
    return {
        "requests": requests,
        "started_at_epoch": float(started) if started is not None else None,
        "request_limit_at_creation": stored_limit,
        "completed": completed,
    }


def _read_budget_state_unlocked(vault: Any) -> dict[str, Any]:
    path = _budget_path(vault)
    if not path.exists() and not path.is_symlink():
        from .state_layout import control_required
        if control_required(path):
            raise ExtractionWorkStateError("required extraction request budget state missing")
        # Legacy builds had no existence invariant. Their persisted run counters
        # still prove that this is not an unused first-allocation ledger.
        from .process_common import _read_processed
        from .incremental_run_state import KEY, load_run
        processed = _read_processed(vault.processed_state_path)
        if any(load_run(processed, key)["reserved_requests"] for key in processed.get(KEY, {})):
            raise ExtractionWorkStateError("extraction request budget evidence missing")
        return _empty_state()
    if path.is_symlink() or not path.is_file():
        raise ExtractionWorkStateError("unsafe extraction request budget state")
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_BUDGET_BYTES + 1)
        if len(raw) > _MAX_BUDGET_BYTES:
            raise ValueError("extraction budget state too large")
        value = parse_strict_json(raw.decode("utf-8"))
    except (OSError, UnicodeError, TypeError, ValueError) as error:
        # Do not clear/recreate damaged authorization/retry state: that would
        # silently reopen the provider request budget after corruption.
        raise ExtractionWorkStateError("cannot read extraction request budget state") from error
    if (not isinstance(value, Mapping) or type(value.get("version")) is not int
            or value["version"] not in {_VERSION, _RETIRED_VERSION}):
        raise ExtractionWorkStateError("invalid extraction request budget state")
    works = value.get("works")
    order = value.get("order")
    if not isinstance(works, Mapping) or not isinstance(order, list):
        raise ExtractionWorkStateError("invalid extraction request budget state")
    if len(works) > _MAX_WORKS + _MAX_RETIRED_WORKS:
        raise ExtractionWorkStateError("extraction request budget state exceeds bound")
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
        retired = raw.get("retired", False)
        if (type(retired) is not bool or ("retired" in raw and value["version"] != _RETIRED_VERSION)
                or (retired and (not normalized_turns or any(not t["completed"] for t in normalized_turns.values())))):
            raise ExtractionWorkStateError("invalid retired extraction budget")
        normalized_works[work_id] = {"turns": normalized_turns, **({"retired": True} if retired else {})}
    normalized_order = [
        item for item in order
        if _valid_identifier(item, maximum=200) and item in normalized_works
    ]
    if len(normalized_order) != len(set(normalized_order)) or set(normalized_order) != set(normalized_works):
        raise ExtractionWorkStateError("invalid extraction request budget order")
    retired_count = sum(w.get("retired", False) for w in normalized_works.values())
    if len(normalized_works) - retired_count > _MAX_WORKS or retired_count > _MAX_RETIRED_WORKS:
        raise ExtractionWorkStateError("extraction request budget state exceeds bound")
    return {"version": value["version"], "works": normalized_works, "order": normalized_order}


def _save_budget_state_unlocked(vault: Any, state: dict[str, Any]) -> None:
    encoded = (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True, separators=(",", ": ")) + "\n").encode("utf-8")
    if len(encoded) > _MAX_BUDGET_BYTES:
        raise ExtractionWorkStateError("extraction request budget state exceeds byte bound")
    from .state_layout import require_control
    require_control(vault, "extraction_request_budget.json")
    atomic_write_json(_budget_path(vault), state, mode=0o600)


def _work_unlocked(state: dict[str, Any], *, work_id: str) -> dict[str, Any]:
    works = state["works"]
    order = state["order"]
    work = works.get(work_id)
    if work is not None:
        if not isinstance(work, dict) or not isinstance(work.get("turns"), dict):
            raise ExtractionWorkStateError("invalid extraction request budget work")
        return work
    if sum(not row.get("retired", False) for row in works.values()) >= _MAX_WORKS:
        # The previous implementation deleted a completed row here, permitting
        # that exact work to reserve again. Retain its counts instead. Only
        # source-scoped work IDs identify a closed immutable set of turns;
        # old job IDs may still acquire another turn and must not be guessed.
        oldest = next((key for key in order if re.fullmatch(r"work-[0-9a-f]{64}", key)
                       and not works[key].get("retired") and works[key]["turns"]
                       and all(t["completed"] for t in works[key]["turns"].values())), None)
        if oldest is None or sum(w.get("retired", False) for w in works.values()) >= _MAX_RETIRED_WORKS:
            raise ExtractionWorkStateError("extraction request budget state is full")
        works[oldest]["retired"] = True
        state["version"] = _RETIRED_VERSION
    work = {"turns": {}}
    works[work_id] = work
    order.append(work_id)
    return work


def extraction_work_id(turn: Any, *, request_kind: str, intent_id: str) -> str:
    """Bind request budget to source revisions and trusted authorization."""

    if request_kind not in {"automatic", "explicit_remember"}:
        raise ExtractionWorkStateError("invalid extraction request kind")
    if not _valid_identifier(intent_id, maximum=800):
        raise ExtractionWorkStateError("invalid extraction intent id")
    events = []
    for event in getattr(turn, "events", ()):
        events.append(
            {
                "event_key": getattr(event, "event_key", None),
                "message_id": getattr(event, "message_id", None),
                "message_revision": getattr(event, "message_revision", None),
            }
        )
    payload = json.dumps(
        [request_kind, intent_id, turn.source, turn.session_id, turn.turn_key, events],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "work-" + hashlib.sha256(payload).hexdigest()


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


def reserve_model_request(
    vault: Any,
    *,
    work_id: str,
    turn_id: str,
    request_limit: int = MAX_MODEL_REQUESTS,
    legacy_turn_id: str | None = None,
    legacy_source_unchanged: bool = True,
) -> int | None:
    """Atomically reserve the next provider request and return its ordinal.

    ``None`` means this stable work+turn already consumed the request budget.  The
    reservation happens before the outbound call, so a process kill after this
    write still consumes that attempt conservatively.
    """

    if not _valid_identifier(work_id, maximum=200):
        raise ExtractionWorkStateError("invalid extraction work id")
    if not _valid_identifier(turn_id, maximum=800):
        raise ExtractionWorkStateError("invalid extraction turn id")
    if type(request_limit) is not int or not 1 <= request_limit <= MAX_MODEL_REQUESTS:
        raise ExtractionWorkStateError("invalid extraction request limit")
    if type(legacy_source_unchanged) is not bool:
        raise ExtractionWorkStateError("invalid legacy source compatibility flag")
    with vault.lock():
        state = _read_budget_state_unlocked(vault)
        # Previous releases keyed budgets by job ID. Moving to a source key
        # must not silently grant a second budget for the same legacy turn.
        migrated = []
        if legacy_turn_id is not None:
            if legacy_turn_id != turn_id:
                raise ExtractionWorkStateError("legacy turn budget identity mismatch")
            for old_id in list(state["order"]):
                old_work = state["works"][old_id]
                if not old_id.startswith("job-") or legacy_turn_id not in old_work["turns"]:
                    continue
                if not legacy_source_unchanged:
                    # The old job key does not identify the source revision.
                    # Do not borrow its authority OR silently start a new budget.
                    raise ExtractionWorkStateError("legacy source budget requires migration")
                migrated.append(_normalize_turn_state(old_work["turns"].pop(legacy_turn_id)))
                if not old_work["turns"]:
                    del state["works"][old_id]
                    state["order"].remove(old_id)
        work = _work_unlocked(state, work_id=work_id)
        turns = work["turns"]
        if work.get("retired"):
            if migrated:
                raise ExtractionWorkStateError("retired extraction budget requires migration")
            return None
        if migrated:
            if turn_id in turns:
                migrated.append(_normalize_turn_state(turns[turn_id]))
            turns[turn_id] = {
                "requests": min(1_000_000, sum(row["requests"] for row in migrated)),
                "started_at_epoch": None,
                "request_limit_at_creation": min([request_limit] + [row["request_limit_at_creation"] for row in migrated]),
                "completed": any(row["completed"] for row in migrated),
            }
            # Persist even when the migrated count is already exhausted.
            _save_budget_state_unlocked(vault, state)
        turn_state = turns.get(turn_id)
        if turn_state is None:
            if len(turns) >= _MAX_TURNS_PER_WORK:
                raise ExtractionWorkStateError("extraction work turn budget is full")
            # Keep the existing on-disk shape. A timestamp is not required for
            # request accounting and must not become a hidden expiry again.
            turn_state = {
                "requests": 0,
                "started_at_epoch": None,
                "request_limit_at_creation": request_limit,
                "completed": False,
            }
            turns[turn_id] = turn_state
        else:
            turn_state = _normalize_turn_state(turn_state)
            turns[turn_id] = turn_state
        if turn_state.get("completed") is True:
            return None
        count = turn_state["requests"]
        effective_limit = min(turn_state["request_limit_at_creation"], request_limit)
        if count >= effective_limit:
            return None
        ordinal = count + 1
        turn_state["requests"] = ordinal
        _save_budget_state_unlocked(vault, state)
        return ordinal


def complete_turn_budget(vault: Any, *, work_id: str, turn_id: str) -> bool:
    """Mark the authorization terminal without reopening its consumed budget."""

    if not _valid_identifier(work_id, maximum=200) or not _valid_identifier(turn_id, maximum=800):
        return False
    try:
        with vault.lock():
            state = _read_budget_state_unlocked(vault)
            work = state["works"].get(work_id)
            if not isinstance(work, dict) or turn_id not in work.get("turns", {}):
                return False
            turn_state = _normalize_turn_state(work["turns"][turn_id])
            if turn_state["completed"]:
                return True
            turn_state["completed"] = True
            work["turns"][turn_id] = turn_state
            _save_budget_state_unlocked(vault, state)
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
    "extraction_work_id",
    "reserve_model_request",
]
