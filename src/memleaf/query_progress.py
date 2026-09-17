"""Non-mutating, bounded observations of pipeline progress and retained capacity.

A retained receipt is not a fact source or permission to replay. These observations
never free an allowance, launch a worker, expire an owner, or collect state.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from collections import Counter

from .inbox import parse_inbox_text, _event_metadata
from .index import EVENT_V2_BLOCK
from .incremental_journal import KEY as COMMIT_KEY, load_work, public_result, resolved_parent_ids
from .incremental_run_state import KEY as RUN_KEY, MAX_RUNS, TERMINAL, load_run, owner_live
from .process_common import _read_processed
from .recording_policy import recording_allowed
from .query_scan import markdown_paths, MAX_FILE_BYTES, MAX_TOTAL_BYTES

MAX_CONTROL_BYTES = 32 * 1024 * 1024


def _control_stamp(path):
    if path.is_symlink():
        raise ValueError("invalid_control_path")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONTROL_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_CONTROL_BYTES:
        raise ValueError("control_limit")
    return hashlib.sha256(raw).hexdigest()


def observe_progress(vault) -> dict:
    """Return only counts/codes; unknown control state cannot hide local facts."""
    try:
        before = _control_stamp(vault.processed_state_path)
        state = _read_processed(vault.processed_state_path)
        runs = [load_run(state, key) for key in state.get(RUN_KEY, {})]
        resolved = resolved_parent_ids(state)
        works = [load_work(state, key) for key in state.get(COMMIT_KEY, {})]
        pending_commits = sum(public_result(w)["execution_status"] == "recovery_required"
                              for w in works if w["work_id"] not in resolved)
        pending_runs = sum(r["status"] not in TERMINAL for r in runs)
        unresolved_runs = sum(r["status"] in {"blocked", "failed", "completed_with_unresolved"} for r in runs)
        pending = incomplete = 0
        paths, issues = markdown_paths(vault.root, "inbox")
        unknown = bool(issues)
        size = 0
        for path in paths:
            if path.resolve() != path.absolute() or path.is_symlink() or not path.is_file():
                unknown = True
                continue
            stamp = path.lstat()
            if not stat.S_ISREG(stamp.st_mode):
                unknown = True
                continue
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (stamp.st_dev, stamp.st_ino):
                    raise ValueError("source_changed")
                raw = stream.read(MAX_FILE_BYTES + 1)
            size += len(raw)
            if len(raw) > MAX_FILE_BYTES or size > MAX_TOTAL_BYTES:
                unknown = True
                break
            text = raw.decode("utf-8")
            turns = parse_inbox_text(text, source=path.parent.name, session_id=path.stem)
            # Legacy/invalid wrappers cannot establish a complete observation.
            blocks = list(EVENT_V2_BLOCK.finditer(text))
            metadata = _event_metadata(text)
            if (text.count("<!-- memleaf:event:") > len(blocks)
                    or len(metadata) != len(blocks)
                    or any(not isinstance(m.get("event_key"), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", m["event_key"]) for m in metadata)
                    or any(not t.processable for t in turns)):
                unknown = True
            if text.strip() and not turns:
                unknown = True
            for turn in turns:
                if not turn.turn_key or not recording_allowed(state, turn.source, turn.session_id, turn.turn_key):
                    continue
                entries = state.get("sessions", {}).get(f"{turn.source}/{turn.session_id}", {}).get("processed_turns", [])
                if not isinstance(entries, list):
                    raise ValueError("invalid_processed_turns")
                matched = [e for e in entries if isinstance(e, dict)
                           and e.get("turn_key") == turn.turn_key
                           and set(e.get("event_keys", [])) == set(turn.event_keys)]
                if len(matched) > 1:
                    raise ValueError("ambiguous_processed_turns")
                if matched and not matched[0].get("deferred_evidence") and not matched[0].get("deferred_candidates"):
                    continue
                # Single-input remember uses its own finalized receipt; selected
                # messages from a normal turn do not consume that automatic turn.
                explicit = any(getattr(e, "explicit_input", None) for e in turn.events)
                done = any(r["source"] == turn.source and r["session_id"] == turn.session_id
                    and r["turn_key"] == turn.turn_key and r["status"] in {"completed", "cancelled"}
                    and (explicit or r.get("request_kind", "automatic") == "automatic")
                    and set(turn.event_keys).issubset(set(r["source_keys"])) for r in runs)
                if not done:
                    if turn.complete:
                        pending += 1
                    else:
                        incomplete += 1
            now = path.stat()
            if (stamp.st_size, stamp.st_mtime_ns, stamp.st_ctime_ns) != (now.st_size, now.st_mtime_ns, now.st_ctime_ns):
                unknown = True
        # The queue reader validates but never starts or recovers a worker.
        from .process_jobs import _read_state, _state_path
        jobs_path = _state_path(vault)
        job_before = _control_stamp(jobs_path)
        jobs = _read_state(vault)
        queued = sum(j.get("status") in {"queued", "running"} for j in jobs["jobs"].values())
        final_paths, final_issues = markdown_paths(vault.root, "inbox")
        unknown = unknown or paths != final_paths or bool(final_issues)
        changed = before != _control_stamp(vault.processed_state_path) or job_before != _control_stamp(jobs_path)
        unresolved = unresolved_runs + len(state.get("pending_turn_plans", {}))
        status = "unknown" if unknown or changed else "pending" if (
            pending or incomplete or pending_runs or pending_commits or queued or unresolved) else "current"
        return {"status": status, "scope": "vault", "pending_turns": pending,
                "incomplete_turns": incomplete, "pending_commits": pending_commits,
                "unresolved_runs": unresolved, "queued_jobs": queued}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError, RuntimeError):
        return {"status": "unknown", "scope": "vault", "code": "control_state_unavailable"}


def retention_inventory(vault) -> dict:
    """Read-only preflight, NOT a GC authorization or a safe-to-delete list."""
    before = _control_stamp(vault.processed_state_path)
    state = _read_processed(vault.processed_state_path)
    runs = [load_run(state, key) for key in state.get(RUN_KEY, {})]
    works = [load_work(state, key) for key in state.get(COMMIT_KEY, {})]
    statuses = Counter(r["status"] for r in runs)
    if before != _control_stamp(vault.processed_state_path):
        raise ValueError("control_state_changed")
    return {"read_only": True, "collection_authorized": False,
            "retained_runs": len(runs), "run_limit": MAX_RUNS,
            "remaining_run_slots": max(0, MAX_RUNS - len(runs)),
            "capacity_status": "full" if len(runs) >= MAX_RUNS else "available",
            "retained_commits": len(works), "retained_by_status": dict(sorted(statuses.items())),
            "pending_commits": sum(public_result(w)["execution_status"] == "recovery_required" for w in works),
            "owner_live": owner_live(state),
            "limitations": ["terminal_receipts_may_suppress_replay", "collection_requires_dependency_and_window_contract"]}
