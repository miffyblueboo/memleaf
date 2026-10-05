"""Optional maintenance after extraction, sharing its sealed request allowance.

No model call below the configured threshold. At most one batch request can
use a freshly completed turn's remaining allowance; neither polling nor a
process restart grants another budget. Only hashes and counters are persisted.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
import uuid

from .compaction import Compactor, CompactionError, _clock_now
from .extraction_work_state import (_read_budget_state_unlocked, _reserve_maintenance_unlocked,
                                    _remember_maintenance_snapshot_unlocked)
from .incremental_run_state import (OWNER, load_run, owner_live, register_owner,
                                    unregister_owner, safe_call_metrics)
from .locking import atomic_write_json
from .models import Memory
from .process_common import _read_processed
from .prompts import COMPACT_SYSTEM
from .retention import RetentionManager


def _signature(selected):
    values = sorted((Compactor._prompt_memory(c) for c in selected), key=lambda m: m["memory_id"])
    raw = json.dumps([COMPACT_SYSTEM, values], ensure_ascii=False,
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _MaintenanceBackend:
    def __init__(self):
        self.backend = None
        self.calls = 0
        self.failed = False
        self.usage = {}

    def complete(self, prompt, **kwargs):
        consume = getattr(self.backend, "consume_call_metrics", None)
        if callable(consume):
            try:
                consume()
            except Exception:
                pass
        try:
            self.calls += 1
            return self.backend.complete(prompt, **kwargs)
        except Exception:
            self.failed = True
            raise
        finally:
            if callable(consume):
                try:
                    self.usage = safe_call_metrics(consume())
                except Exception:
                    pass

    def metrics(self, status):
        if not self.calls:
            return {}
        bucket = {"call_count": self.calls, "failed_calls": int(self.failed),
                  "invalid_output_count": int(status == "invalid_output"), **self.usage}
        return {"total": bucket, "stages": {"compact": bucket},
                "calls": [{"stage": "compact", "operation": "compact_primary",
                           "call_index": 1, "failed": self.failed,
                           "invalid_output": status == "invalid_output", **self.usage}]}


def maintain_lifecycle(service, result, *, run_ids=(), model=None, router=None):
    """Attach independent maintenance outcomes without undoing committed facts."""
    result["maintenance_model_calls"] = 0
    manager = RetentionManager(service)
    try:
        result["retention"] = manager.maintain(_clock_now(getattr(service, "clock", None)))
    except (OSError, ValueError, RuntimeError):
        result["retention"] = {"maintenance_status": "deferred", "code": "maintenance_state_unavailable"}
    retention = result["retention"]
    retention.setdefault("maintenance_status", "completed")
    if retention.get("maintenance_status") in {"deferred", "partial"}:
        result["compaction"] = {"status": "deferred", "code": retention.get("code"), "backend_calls": 0}
        return result
    if not run_ids or result.get("execution_status") != "completed":
        result["compaction"] = {"status": "not_run", "reason": "no_fresh_completed_turn", "backend_calls": 0}
        return result

    backend = _MaintenanceBackend()
    token = uuid.uuid4().hex
    claimed = False
    selected_input = []
    ordinal = None

    def safe_dependencies(processed):
        _, protected, unresolved = manager._dependencies_unlocked(processed)
        if protected or unresolved:
            raise CompactionError("pending mutation dependencies")

    def admit(selected, threshold, ratio):
        nonlocal claimed, selected_input, ordinal
        fingerprint = _signature(selected)
        with service._mutation_boundary():
            processed = _read_processed(service.vault.processed_state_path)
            if owner_live(processed):
                return "processing_busy"
            safe_dependencies(processed)
            # One successful source allowance, never a newly allocated budget.
            run = next((r for rid in reversed(run_ids) if (r := load_run(processed, rid)) is not None
                        and r["status"] == "completed" and r.get("budget_finalized")), None)
            if run is None:
                return "maintenance_budget_unavailable"
            from .incremental_runtime import _resolve
            backend.backend = _resolve(service, model, router)
            ordinal, reason = _reserve_maintenance_unlocked(service.vault,
                work_id=run["budget_id"], turn_id=run["turn_budget_id"], snapshot=fingerprint,
                minimum_requests=run["reserved_requests"])
            if reason:
                return reason
            register_owner(token)
            claimed = True
            processed[OWNER] = {"run_id": run["run_id"], "pid": os.getpid(), "token": token}
            atomic_write_json(service.vault.processed_state_path, processed)
            selected_input = selected
        return None

    def before_commit():
        # Called inside the compactor's mutation boundary. New explicit work,
        # forget/cancel, or an ownership change during IO postpones the write.
        processed = _read_processed(service.vault.processed_state_path)
        owner = processed.get(OWNER, {})
        run = load_run(processed, owner.get("run_id")) if owner.get("token") == token else None
        if run is None or run["status"] != "completed":
            raise CompactionError("maintenance ownership changed")
        safe_dependencies(processed)

    try:
        compaction = Compactor(service).auto(model=backend, admit=admit, before_commit=before_commit)
        if compaction.get("status") == "compacted":
            # Remember the resulting input too: another turn must not pay to
            # compress these same heads again merely because total size is high.
            try:
                with service.vault.lock():
                    from .query_scan import MAX_FILE_BYTES
                    current = []
                    for candidate in selected_input:
                        if candidate.path.is_symlink():
                            raise CompactionError("unsafe maintenance target")
                        with candidate.path.open("rb") as stream:
                            raw = stream.read(MAX_FILE_BYTES + 1)
                        if len(raw) > MAX_FILE_BYTES:
                            raise CompactionError("unsafe maintenance target")
                        current.append(replace(candidate, memory=Memory.from_markdown(raw.decode("utf-8"), candidate.path)))
                    state = _read_budget_state_unlocked(service.vault)
                    _remember_maintenance_snapshot_unlocked(service.vault, state, _signature(current))
            except (OSError, ValueError, RuntimeError):
                compaction["deduplication_status"] = "deferred"
                compaction["code"] = "maintenance_state_unavailable"
    except (OSError, ValueError, RuntimeError):
        compaction = {"status": "deferred", "code": "maintenance_state_unavailable"}
    finally:
        if claimed:
            try:
                with service.vault.lock():
                    processed = _read_processed(service.vault.processed_state_path)
                    if processed.get(OWNER, {}).get("token") == token:
                        processed.pop(OWNER)
                        atomic_write_json(service.vault.processed_state_path, processed)
            except (OSError, ValueError, RuntimeError):
                pass  # A stale local/dead owner cannot grant another request.
            finally:
                unregister_owner(token)

    compaction["backend_calls"] = backend.calls
    if ordinal is not None:
        compaction["request_ordinal"] = ordinal
    metrics = backend.metrics(compaction.get("status"))
    if metrics:
        compaction["model_metrics"] = metrics
        from .process_jobs import _aggregate_model_metrics
        result["model_metrics"] = _aggregate_model_metrics([result.get("model_metrics", {}), metrics])
    result["compaction"] = compaction
    result["maintenance_model_calls"] = backend.calls
    count_key = "model_calls" if "model_calls" in result else "model_calls_this_invocation"
    result[count_key] = result.get(count_key, 0) + backend.calls
    if compaction.get("status") == "compacted":
        try:
            after = manager.maintain(_clock_now(getattr(service, "clock", None)))
            for key in ("history_pruned", "closed_todos_retired", "provenance_rewritten"):
                after[key] = after.get(key, 0) + retention.get(key, 0)
            result["retention"] = after
        except (OSError, ValueError, RuntimeError):
            result["retention"]["maintenance_status"] = "deferred"
            result["retention"]["code"] = "maintenance_state_unavailable"
    return result
