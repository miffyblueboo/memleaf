"""Deterministic lifecycle maintenance for closed todos and historical Markdown."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .locking import atomic_unlink, atomic_write_text
from .models import Memory
from .process_common import _read_processed
from .memory_writer import MemoryWriter
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

    def _bound_legacy_provenance_unlocked(self, pending: Mapping[str, Any]) -> int:
        """Compact oversized pre-policy source lists without changing memory content.

        Active targets referenced by frozen/pending writes are left untouched so
        maintenance cannot invalidate their optimistic revision while a replay is
        still authorized. Historical files are never automatic write targets.
        """

        rewritten = 0
        for area in ("knowledge", "history"):
            for record in self.service._read_memories_unlocked(area):
                memory = record.memory
                if len(memory.sources) <= MAX_MEMORY_SOURCES:
                    continue
                if area == "knowledge" and _references_target(pending, memory.memory_id):
                    continue
                if record.path.is_symlink():
                    raise RetentionError("unsafe memory path during provenance maintenance")
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
        processed = _read_processed(self.service.vault.processed_index_path)
        pending = {
            "pending_operations": processed.get("pending_operations", {}),
            "pending_turn_plans": processed.get("pending_turn_plans", {}),
        }
        writer = MemoryWriter(self.service)
        retired = 0
        for record in list(self.service._read_memories_unlocked("knowledge")):
            memory = record.memory
            if memory.type != "todo" or memory.status not in {"completed", "cancelled"}:
                continue
            anchor = memory.completed_at if memory.status == "completed" else memory.updated
            age = _age_days(now, anchor)
            if age is None or age < closed_days:
                continue
            if _references_target(pending, memory.memory_id):
                continue
            writer._write_history(
                memory,
                superseded_by=memory.memory_id,
                archived_at=now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                invalidated_reason="todo_closed",
            )
            if record.path.is_symlink():
                raise RetentionError("unsafe closed todo path")
            atomic_unlink(record.path)
            retired += 1
        return retired

    def _prune_history_unlocked(
        self, now: datetime, policy: str, retention_days: int, max_versions: int
    ) -> int:
        if policy == "keep_all":
            return 0
        groups: dict[str, list[Any]] = {}
        for record in self.service._read_memories_unlocked("history"):
            groups.setdefault(self._history_group(record.memory), []).append(record)
        removed = 0
        for records in groups.values():
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
                atomic_unlink(record.path)
                removed += 1
        return removed

    def maintain(self, now_value: str) -> dict[str, Any]:
        now = _parse_time(now_value)
        if now is None:
            raise RetentionError("invalid retention clock")
        closed_days, policy, retention_days, max_versions = self._settings()
        with self.service._mutation_boundary():
            processed = _read_processed(self.service.vault.processed_index_path)
            pending = {
                "pending_operations": processed.get("pending_operations", {}),
                "pending_turn_plans": processed.get("pending_turn_plans", {}),
            }
            provenance_rewritten = self._bound_legacy_provenance_unlocked(pending)
            retired = self._retire_closed_todos_unlocked(now, closed_days)
            pruned = self._prune_history_unlocked(now, policy, retention_days, max_versions)
            if provenance_rewritten or retired or pruned:
                self.service._rebuild_index_unlocked()
        return {
            "provenance_rewritten": provenance_rewritten,
            "closed_todos_retired": retired,
            "history_pruned": pruned,
            "history_policy": policy,
        }


__all__ = ["RetentionError", "RetentionManager"]
