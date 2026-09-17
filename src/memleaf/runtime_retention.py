"""Explicit lossless terminal-state compaction on the existing control files.

This releases working slots, not old authorization. Exact sealed receipts remain
bounded and readable. No time-based deletion, source replay or model call occurs.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import stat
from typing import Any

from . import extraction_work_state as budgets
from . import incremental_journal as commits
from . import incremental_run_state as runs
from .locking import atomic_write_json
from .models import utc_now
from .process_common import _read_processed
from .process_journal import ProcessJournal
from .validation import parse_strict_json
from .receipt_codec import encode_receipt, is_compact, ledger_usage, MAX_COMPACT_RECEIPTS

MAX_FILES_BYTES = 32 * 1024 * 1024
MAX_BATCH = 128


class RuntimeRetentionError(OSError):
    """A partial physical compaction; original decisions are never rolled back."""
    def __init__(self, result: dict[str, Any]):
        super().__init__("runtime_state_compaction_interrupted; preview current state before retry")
        self.result = result


def _bytes(path):
    if path.is_symlink():
        raise ValueError("unsafe_runtime_state")
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("unsafe_runtime_state")
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILES_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_FILES_BYTES:
        raise ValueError("runtime_state_too_large")
    return raw


def _revision(values):
    return commits.digest({key: hashlib.sha256(raw).hexdigest() if raw is not None else None
                           for key, raw in values.items()})


def _plan_revision(values, max_records):
    return commits.digest(["lossless-retention-v1", _revision(values), max_records,
                           runs.MAX_RUNS, MAX_COMPACT_RECEIPTS, budgets._MAX_RETIRED_WORKS])


def _dump(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, separators=(",", ": ")) + "\n").encode("utf-8")


def _paths(service):
    return {"processed": service.vault.processed_state_path, "budget": budgets._budget_path(service.vault)}


def _view(service):
    paths = _paths(service)
    raw = {key: _bytes(path) for key, path in paths.items()}
    processed = _read_processed(paths["processed"])
    budget = budgets._read_budget_state_unlocked(service.vault)
    loaded_runs = {key: runs.load_run(processed, key) for key in processed.get(runs.KEY, {})}
    loaded_works = {key: commits.load_work(processed, key) for key in processed.get(commits.KEY, {})}
    # Validates every cumulative parent link, including settled-operation copies.
    resolved = commits.resolved_parent_ids(processed)
    journal = ProcessJournal(service)
    if runs.owner_live(processed) or any(
            isinstance(s, dict) and journal._processing_marker_live(s.get("processing"), utc_now())
            for s in processed.get("sessions", {}).values()):
        raise ValueError("processing_busy")
    if raw != {key: _bytes(path) for key, path in paths.items()}:
        raise ValueError("runtime_state_changed")
    return raw, processed, budget, loaded_runs, loaded_works, resolved


def _sizes(processed, budget):
    run_usage = ledger_usage(processed.get(runs.KEY, {}), compact_version=runs.COMPACT_VERSION)
    work_usage = ledger_usage(processed.get(commits.KEY, {}), compact_version=commits.COMPACT_VERSION)
    retired = sum(row.get("retired", False) for row in budget["works"].values())
    return {"runs_full": run_usage["full"], "runs_compact": run_usage["compact"],
            "commits_full": work_usage["full"], "commits_compact": work_usage["compact"],
            "budgets_active": len(budget["works"]) - retired, "budgets_retired": retired,
            "remaining_run_slots": max(0, runs.MAX_RUNS - run_usage["full"]),
            "remaining_budget_slots": max(0, budgets._MAX_WORKS - len(budget["works"]) + retired)}


def _prepare(service, max_records):
    raw, processed, budget, loaded_runs, loaded_works, resolved = _view(service)
    after = deepcopy(processed)
    # Keep opaque extension fields and original counter representation in the
    # physical file. The normalized view above is used only for validation.
    after_budget = parse_strict_json(raw["budget"].decode("utf-8")) if raw["budget"] is not None else deepcopy(budget)
    selected = {"runs": [], "commits": [], "budgets": []}
    reasons = Counter()
    # A terminal label alone is insufficient: index/source receipt and the
    # durable allowance must have reached their terminal states too.
    for key, run in loaded_runs.items():
        if is_compact(after[runs.KEY][key], runs.COMPACT_VERSION):
            continue
        work = loaded_works.get(run["commit_work_id"])
        row = budget["works"].get(run["budget_id"], {}).get("turns", {}).get(run["turn_budget_id"])
        if (run["status"] != "completed" or not run.get("budget_finalized")
                or not row or not row["completed"] or row["requests"] < run["reserved_requests"]
                or work is None or commits.public_result(work)["execution_status"] != "completed"
                or any(k in run for k in ("request", "response", "retention_request", "recovery_seed", "partial_basis"))):
            reasons["run_not_proven_complete"] += 1
            continue
        if len(selected["runs"]) >= max_records:
            reasons["batch_limit"] += 1
            continue
        if ledger_usage(after[runs.KEY], compact_version=runs.COMPACT_VERSION)["compact"] >= MAX_COMPACT_RECEIPTS:
            reasons["receipt_retention_full"] += 1
            continue
        after[runs.KEY][key] = encode_receipt(after[runs.KEY][key], version=runs.COMPACT_VERSION)
        runs.load_run(after, key)
        selected["runs"].append(key)
    for key, work in loaded_works.items():
        if is_compact(after[commits.KEY][key], commits.COMPACT_VERSION):
            continue
        # A resolved parent remains byte-equivalent; it is not removed, so the
        # child's provenance and copied operation IDs still validate on restart.
        if ((commits.public_result(work)["execution_status"] != "completed" and key not in resolved)
                or not work["receipt_settled"] or work["index_status"] != "current"
                or any(op["state"] not in commits.TERMINAL or "before" in op or "after" in op
                       for op in work["operations"])):
            reasons["commit_not_proven_complete"] += 1
            continue
        if len(selected["commits"]) >= max_records:
            reasons["batch_limit"] += 1
            continue
        if ledger_usage(after[commits.KEY], compact_version=commits.COMPACT_VERSION)["compact"] >= MAX_COMPACT_RECEIPTS:
            reasons["receipt_retention_full"] += 1
            continue
        after[commits.KEY][key] = encode_receipt(after[commits.KEY][key], version=commits.COMPACT_VERSION)
        commits.load_work(after, key)
        selected["commits"].append(key)
    # Retire only an exact budget whose users are all proven complete, retaining
    # the original turns/counts. Unknown old job budgets are not guessed here.
    by_budget = {}
    for run in loaded_runs.values():
        by_budget.setdefault(run["budget_id"], []).append(run)
    for key, stored in after_budget["works"].items():
        row = budget["works"][key]
        if row.get("retired"):
            continue
        linked = by_budget.get(key, [])
        if (not linked or not row["turns"] or not all(t["completed"] for t in row["turns"].values())
                or {r["turn_budget_id"] for r in linked} != set(row["turns"])
                or any(not is_compact(after[runs.KEY][r["run_id"]], runs.COMPACT_VERSION) for r in linked)):
            reasons["budget_not_proven_complete"] += 1
            continue
        if len(selected["budgets"]) >= max_records:
            reasons["batch_limit"] += 1
            continue
        if sum(w.get("retired", False) for w in after_budget["works"].values()) >= budgets._MAX_RETIRED_WORKS:
            reasons["budget_retention_full"] += 1
            continue
        stored["retired"] = True
        after_budget["version"] = budgets._RETIRED_VERSION
        selected["budgets"].append(key)
    changes = {}
    if after != processed:
        changes["processed"] = after
    if selected["budgets"]:
        changes["budget"] = after_budget
    encoded = {key: _dump(value) for key, value in changes.items()}
    if (any(len(value) > MAX_FILES_BYTES for value in encoded.values())
            or len(encoded.get("budget", b"")) > budgets._MAX_BUDGET_BYTES):
        raise ValueError("runtime_state_too_large")
    # Full semantic equivalence, not just matching payload checksums.
    for key, old in loaded_runs.items():
        if runs.load_run(after, key) != old:
            raise ValueError("retention_changed_run")
    for key, old in loaded_works.items():
        if commits.load_work(after, key) != old:
            raise ValueError("retention_changed_commit")
    commits.resolved_parent_ids(after)
    result = {"execution_status": "preview", "state_revision": _plan_revision(raw, max_records), "read_only": True,
              "selected": {key: len(value) for key, value in selected.items()},
              "skipped": dict(sorted(reasons.items())), "before": _sizes(processed, budget),
              "after": _sizes(after, after_budget), "changed_files": sorted(changes),
              "bytes_before": sum(len(v) for v in raw.values() if v is not None),
              "bytes_after": sum(len(encoded.get(k, v) or b"") for k, v in raw.items()),
              "model_calls": 0, "decisions_deleted": 0, "sources_deleted": 0,
              "limits": {"full_runs": runs.MAX_RUNS, "compact_receipts_per_kind": MAX_COMPACT_RECEIPTS,
                         "retired_budgets": budgets._MAX_RETIRED_WORKS, "batch_per_kind": max_records},
              "limitations": ["lossless_compaction_not_time_window_erasure", "bounded_retained_suppression",
                              "no_partial_expiry", "legacy_writer_requires_cold_switch"]}
    return raw, changes, encoded, result


def compact_runtime_state(service: Any, *, dry_run: bool = True, expected_revision: str | None = None,
                          max_records: int = 64) -> dict[str, Any]:
    """Preview by default; apply only to the exact observed control revision.

    Only existing processed/budget files can change. Incomplete, failed and
    cancelled runs retain their normal representation and authority. Independent
    files are atomically replaced, not a global transaction; interruptions are
    resumed by previewing again. This operation is never run by read/status.
    """
    if type(dry_run) is not bool or type(max_records) is not int or not 1 <= max_records <= MAX_BATCH:
        raise ValueError("invalid_runtime_retention_options")
    if not dry_run and (not isinstance(expected_revision, str) or len(expected_revision) != 64):
        raise ValueError("runtime_retention_revision_required")
    if dry_run:
        _, _, _, result = _prepare(service, max_records)
        return result
    with service.vault.lock():
        raw, changes, encoded, result = _prepare(service, max_records)
        if expected_revision != result["state_revision"]:
            raise ValueError("runtime_state_changed")
        result.update(read_only=False, execution_status="completed", applied_files=[])
        paths = _paths(service)
        for key, value in changes.items():
            # Detect edits to either dependency before each replacement. This
            # does not claim atomicity against an uncooperative external editor.
            expected = {name: encoded[name] if name in result["applied_files"] else old for name, old in raw.items()}
            if expected != {name: _bytes(path) for name, path in paths.items()}:
                result.update(execution_status="interrupted", code="runtime_state_changed")
                raise RuntimeRetentionError(result)
            try:
                atomic_write_json(paths[key], value)
            except OSError as error:
                try:
                    if _bytes(paths[key]) == encoded[key]:
                        result["applied_files"].append(key)
                except (OSError, ValueError):
                    pass
                result.update(execution_status="interrupted", code="runtime_state_write_failed")
                raise RuntimeRetentionError(result) from error
            result["applied_files"].append(key)
        result["state_revision"] = _plan_revision({key: _bytes(path) for key, path in paths.items()}, max_records)
        return result
