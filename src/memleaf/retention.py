"""Deterministic lifecycle maintenance for closed todos and historical Markdown."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .locking import atomic_unlink, atomic_write_text
from .models import Memory
from .process_common import _read_processed
from .source_policy import MAX_MEMORY_SOURCES, merge_sources


class RetentionError(RuntimeError):
    pass


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_days(now: datetime, value: Any) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 86400.0)


def _references_target(value: Any, memory_id: str) -> bool:
    """Conservative pending-state scan; false positives only postpone cleanup."""
    target = memory_id.casefold()
    if isinstance(value, str):
        return value.casefold() == target
    if isinstance(value, Mapping):
        return any(_references_target(item, memory_id) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_references_target(item, memory_id) for item in value)
    return False


class RetentionManager:
    def __init__(self, service: Any):
        self.service = service

    @staticmethod
    def _current_record(record: Any) -> Memory:
        from .query_scan import MAX_FILE_BYTES
        from .turn_plan import revision_digest
        if record.path.is_symlink():
            raise RetentionError("unsafe maintenance target")
        with record.path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise RetentionError("maintenance target exceeds byte limit")
        current = Memory.from_markdown(raw.decode("utf-8"), record.path)
        if revision_digest(current) != revision_digest(record.memory):
            raise RetentionError("maintenance target changed")
        return current

    def _settings(self) -> tuple[int, str, int, int]:
        config = self.service.vault.config()
        process = config.get("process") if isinstance(config, Mapping) else None
        history = config.get("history") if isinstance(config, Mapping) else None
        closed_days = process.get("closed_todo_retention_days") if isinstance(process, Mapping) else None
        policy = history.get("policy") if isinstance(history, Mapping) else None
        retention_days = history.get("retention_days") if isinstance(history, Mapping) else None
        max_versions = history.get("max_versions_per_memory") if isinstance(history, Mapping) else None
        if type(closed_days) is not int or closed_days < 0:
            raise RetentionError("invalid closed todo retention")
        if policy not in {"bounded", "keep_all"}:
            raise RetentionError("invalid history policy")
        if type(retention_days) is not int or retention_days < 1:
            raise RetentionError("invalid history retention days")
        if type(max_versions) is not int or max_versions < 1:
            raise RetentionError("invalid history max versions")
        return closed_days, policy, retention_days, max_versions

    def _bound_legacy_provenance_unlocked(self, pending: Mapping[str, Any], protected=frozenset()) -> int:
        """Compact oversized pre-policy source lists without changing memory content.

        Active targets referenced by frozen/pending writes are left untouched so
        maintenance cannot invalidate their optimistic revision while a replay is
        still authorized. Historical files are never automatic write targets.
        """

        rewritten = 0
        for area in ("knowledge", "history"):
            for record in self.service._read_memories_unlocked(area):
                memory = record.memory
                if self._history_group(memory) in protected or memory.memory_id.casefold() in protected:
                    continue
                if len(memory.sources) <= MAX_MEMORY_SOURCES:
                    continue
                if area == "knowledge" and _references_target(pending, memory.memory_id):
                    continue
                if record.path.is_symlink():
                    raise RetentionError("unsafe memory path during provenance maintenance")
                latest = self._current_record(record)
                memory.hit_count, memory.last_hit_at = latest.hit_count, latest.last_hit_at
                bounded_sources, source_metadata = merge_sources([], memory.sources, extra=memory.extra)
                memory.sources = bounded_sources
                memory.extra.update(source_metadata)
                atomic_write_text(record.path, memory.to_markdown())
                rewritten += 1
        return rewritten

    @staticmethod
    def _history_group(memory: Memory) -> str:
        for key in ("original_memory_id", "active_memory_id"):
            value = memory.extra.get(key)
            if isinstance(value, str) and value:
                return value.casefold()
        return memory.memory_id.casefold()

    def _retire_closed_todos_unlocked(self, now: datetime, closed_days: int) -> int:
        # Closed todos remain current identities.  Age changes their default
        # presentation, not whether the durable target still exists for
        # NO_CHANGE, reopen, or later correction.  Keep the legacy setting in
        # config for compatibility, but no longer move these heads to history.
        del now, closed_days
        return 0

    def _prune_history_unlocked(
        self, now: datetime, policy: str, retention_days: int, max_versions: int, protected=frozenset()
    ) -> int:
        if policy == "keep_all":
            return 0
        groups: dict[str, list[Any]] = {}
        for record in self.service._read_memories_unlocked("history"):
            groups.setdefault(self._history_group(record.memory), []).append(record)
        removed = 0
        for identity, records in groups.items():
            if identity in protected:
                continue
            records.sort(
                key=lambda record: (
                    _parse_time(record.memory.extra.get("archived_at"))
                    or _parse_time(record.memory.updated)
                    or datetime.min.replace(tzinfo=timezone.utc),
                    record.memory.memory_id,
                ),
                reverse=True,
            )
            for index, record in enumerate(records):
                age = _age_days(
                    now,
                    record.memory.extra.get("archived_at") or record.memory.updated,
                )
                over_count = index >= max_versions
                over_age = age is not None and age >= retention_days
                if not (over_count or over_age):
                    continue
                if record.path.is_symlink():
                    raise RetentionError("unsafe history path")
                self._current_record(record)
                atomic_unlink(record.path)
                removed += 1
        return removed

    def maintain(self, now_value: str) -> dict[str, Any]:
        now = _parse_time(now_value)
        if now is None:
            raise RetentionError("invalid retention clock")
        closed_days, policy, retention_days, max_versions = self._settings()
        with self.service._mutation_boundary():
            processed = _read_processed(self.service.vault.processed_state_path)
            pending = {
                "pending_operations": processed.get("pending_operations", {}),
                "pending_turn_plans": processed.get("pending_turn_plans", {}),
            }
            # History/provenance are recovery inputs, not merely display data.
            # Defer this optional pass while any retained mutation may need them.
            # The conservative whole-pass hold is bounded and has no new model IO.
            from .incremental_journal import KEY as COMMIT_KEY, load_work, public_result, resolved_parent_ids
            from .incremental_run_state import KEY as RUN_KEY, load_run, TERMINAL
            from .memory_update import pending_explicit_mutations
            try:
                resolved = resolved_parent_ids(processed)
                works = [load_work(processed, key) for key in processed.get(COMMIT_KEY, {})]
                runs = [load_run(processed, key) for key in processed.get(RUN_KEY, {})]
                explicit, _, protected = pending_explicit_mutations(self.service.vault)
                unresolved = (any(pending.values()) or any(
                    w["work_id"] not in resolved and public_result(w)["execution_status"] != "completed"
                    for w in works) or any(run["status"] not in TERMINAL for run in runs)
                    )
            except (OSError, ValueError, TypeError, KeyError, RuntimeError, RecursionError) as error:
                raise RetentionError("cannot validate pending mutation dependencies") from error
            if unresolved:
                return {"provenance_rewritten": 0, "closed_todos_retired": 0, "history_pruned": 0,
                        "history_policy": policy, "maintenance_status": "deferred",
                        "code": "pending_mutation_dependencies", "cleanup_deferred_for_recovery": True,
                        "protected_history_groups": len(protected)}
            from .query_scan import scan_memories, ensure_scan_current
            snapshot = scan_memories(self.service.vault, include_history=True)
            if snapshot.issues:
                return {"provenance_rewritten": 0, "closed_todos_retired": 0, "history_pruned": 0,
                        "history_policy": policy, "maintenance_status": "deferred",
                        "code": "memory_scan_incomplete"}
            ensure_scan_current(self.service.vault, snapshot)
            provenance_rewritten = self._bound_legacy_provenance_unlocked(pending, protected)
            retired = self._retire_closed_todos_unlocked(now, closed_days)
            pruned = self._prune_history_unlocked(now, policy, retention_days, max_versions, protected)
            if provenance_rewritten or retired or pruned:
                self.service._rebuild_index_unlocked()
        return {
            "provenance_rewritten": provenance_rewritten,
            "closed_todos_retired": retired,
            "history_pruned": pruned,
            "history_policy": policy,
            "cleanup_deferred_for_recovery": False,
            "protected_history_groups": len(protected),
            **({"code": "pending_mutation_dependencies", "maintenance_status": "partial"} if protected else {}),
        }


__all__ = ["RetentionError", "RetentionManager"]
