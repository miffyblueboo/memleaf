"""Deterministic Markdown writes for the stage-B processing slice."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .locking import atomic_write_text
from .models import Memory
from .validation import ModelOutputError


class MemoryWriter:
    """Prepare and persist model summaries while Markdown remains authoritative."""

    def __init__(self, service: Any):
        self.service = service
        self.last_metadata_merged = 0
        self.last_noop_memory_ids: set[str] = set()

    @staticmethod
    def deterministic_memory_id(
        *,
        source: str,
        session_id: str,
        turn_key: str,
        candidate_id: str,
        evidence_event_ids: Iterable[str],
    ) -> str:
        material = {
            "candidate_id": candidate_id,
            "evidence_event_ids": sorted({str(item).casefold() for item in evidence_event_ids}),
            "session_id": session_id,
            "source": source,
            "turn_key": turn_key,
        }
        digest = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"mem-{digest[:24]}"

    @staticmethod
    def _history_id(memory: Memory) -> str:
        digest = hashlib.sha256(
            json.dumps(memory.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"hist-{digest[:24]}"

    @staticmethod
    def _same_content(
        left: Memory,
        right: Memory,
        *,
        ignore_archived_at: bool = False,
        ignore_sources: bool = False,
        ignore_runtime_metadata: bool = False,
        ignore_scope_source: bool = False,
        ignore_title: bool = False,
    ) -> bool:
        """Compare fields that a retry should reproduce, excluding timestamps/hits."""

        left_extra = dict(left.extra)
        right_extra = dict(right.extra)
        if ignore_archived_at:
            left_extra.pop("archived_at", None)
            right_extra.pop("archived_at", None)
        if ignore_runtime_metadata:
            left_extra.pop("source", None)
            right_extra.pop("source", None)
        return (
            left.memory_id == right.memory_id
            and (ignore_title or left.title == right.title)
            and left.body == right.body
            and left.tags == right.tags
            and left.type == right.type
            and left.scopes == right.scopes
            and left.aliases == right.aliases
            and left.keywords == right.keywords
            and (ignore_scope_source or left.scope_source == right.scope_source)
            and (ignore_sources or left.sources == right.sources)
            and left.status == right.status
            and left.completed_at == right.completed_at
            and left_extra == right_extra
        )

    @staticmethod
    def _merge_sources(old: Iterable[Mapping[str, Any]], current: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source in list(old) + list(current):
            if not isinstance(source, Mapping):
                continue
            value = dict(source)
            key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    def _active_records(self) -> dict[str, Any]:
        return {
            record.memory.memory_id: record
            for record in self.service._read_memories_unlocked("knowledge")
        }

    @staticmethod
    def _request_event_keys(request: Mapping[str, Any]) -> set[str]:
        turn = request.get("turn")
        events = getattr(turn, "events", ())
        keys = {
            event.event_key
            for event in events
            if isinstance(getattr(event, "event_key", None), str) and event.event_key
        }
        if not keys:
            event_key = request.get("event_key")
            if isinstance(event_key, str) and event_key:
                keys.add(event_key)
        return keys

    @classmethod
    def _request_already_applied(cls, request: Mapping[str, Any], existing: Memory) -> bool:
        request_keys = cls._request_event_keys(request)
        if not request_keys:
            return False
        applied_keys = {
            source.get("event_key")
            for source in existing.sources
            if isinstance(source, Mapping) and isinstance(source.get("event_key"), str)
        }
        return request_keys.issubset(applied_keys)

    def _preflight(self, requests: list[Mapping[str, Any]]) -> None:
        active = self._active_records()
        target_ids: set[str] = set()
        target_turns: dict[str, set[tuple[Any, ...]]] = {}
        duplicate_ids: set[str] = set()
        memory_ids: set[str] = set()
        for request in requests:
            summary = request["summary"]
            duplicate_id = request.get("duplicate_memory_id")
            if duplicate_id is not None:
                if not isinstance(duplicate_id, str) or duplicate_id not in active:
                    raise ModelOutputError("duplicate target is not an active memory")
                if duplicate_id in duplicate_ids:
                    raise ModelOutputError("batch contains duplicate metadata merge target")
                if summary.get("update_memory_id") is not None:
                    raise ModelOutputError("duplicate target conflicts with update target")
                duplicate_ids.add(duplicate_id)
                continue
            target_id = summary.get("update_memory_id")
            if target_id is not None:
                turn = request.get("turn")
                turn_identity = (
                    getattr(turn, "source", None),
                    getattr(turn, "session_id", None),
                    getattr(turn, "turn_key", None),
                )
                seen_turns = target_turns.setdefault(target_id, set())
                if turn_identity in seen_turns:
                    raise ModelOutputError("batch contains duplicate update target")
                seen_turns.add(turn_identity)
                target_ids.add(target_id)
                target = active.get(target_id)
                if target is None:
                    raise ModelOutputError("update target is not an active memory")
                target_memory = getattr(target, "memory", target)
                target_type = (
                    target_memory.type
                    if isinstance(target_memory, Memory)
                    else target_memory.get("type")
                    if isinstance(target_memory, Mapping)
                    else None
                )
                if target_type != summary.get("type"):
                    raise ModelOutputError("update target type does not match summary")
            deterministic_id = request["memory_id"]
            if deterministic_id in memory_ids:
                raise ModelOutputError("batch contains duplicate deterministic memory id")
            if deterministic_id in duplicate_ids:
                raise ModelOutputError("batch memory id collides with metadata merge target")
            if deterministic_id in target_ids:
                raise ModelOutputError("batch memory id collides with update target")
            if target_id is not None and target_id.casefold() == deterministic_id.casefold():
                raise ModelOutputError("batch memory id collides with update target")
            memory_ids.add(deterministic_id)
            if "/" in deterministic_id or "\\" in deterministic_id or deterministic_id in (".", ".."):
                raise ModelOutputError("invalid deterministic memory id")
            # Make the request visible to the rest of this same commit batch.
            # This is needed when a later pending turn updates a memory that
            # an earlier pending turn has just created; the real filesystem
            # write has not happened yet, but the target is still valid.
            if target_id is not None:
                active[target_id] = summary
            else:
                active[deterministic_id] = summary
        if target_ids.intersection(duplicate_ids):
            raise ModelOutputError("batch update target collides with metadata merge target")
        if memory_ids.intersection(duplicate_ids):
            raise ModelOutputError("batch memory id collides with metadata merge target")

    def _core_sources(self, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        turn = request["turn"]
        title = request["conversation_title"]
        result: list[dict[str, Any]] = []
        for event in turn.events:
            item: dict[str, Any] = {
                "event_key": event.event_key,
                "session_id": turn.session_id,
                "turn_id": event.turn_id or "",
                "conversation_title": title,
            }
            result.append(item)
        if not result:
            result.append(
                {
                    "event_key": request["event_key"],
                    "session_id": turn.session_id,
                    "turn_id": request.get("turn_id", ""),
                    "conversation_title": title,
                }
            )
        return result

    def _build_memory(
        self,
        request: Mapping[str, Any],
        *,
        existing: Memory | None,
        now: str,
    ) -> Memory:
        summary = request["summary"]
        memory_id = existing.memory_id if existing is not None else request["memory_id"]
        created = existing.created if existing is not None else now
        status = summary.get("status")
        if summary["type"] == "todo" and status is None:
            status = "active"
        extra = dict(existing.extra) if existing is not None else {}
        extra["source"] = request["turn"].source
        if request.get("explicit_remember") is True:
            extra["explicit_remember"] = True
        core_sources = self._core_sources(request)
        return Memory(
            memory_id=memory_id,
            title=summary["title"],
            body=summary["body"],
            tags=list(summary["tags"]),
            type=summary["type"],
            scopes=list(summary["scopes"]),
            aliases=list(summary.get("aliases", [])),
            keywords=list(summary.get("keywords", [])),
            scope_source=summary.get("scope_source"),
            sources=self._merge_sources(existing.sources if existing is not None else [], core_sources),
            created=created,
            updated=now,
            hit_count=existing.hit_count if existing is not None else 0,
            last_hit_at=existing.last_hit_at if existing is not None else None,
            status=status,
            completed_at=summary.get("completed_at"),
            extra=extra,
        )

    def _build_duplicate_memory(self, request: Mapping[str, Any], existing: Memory, *, now: str) -> Memory:
        summary = request["summary"]
        scopes = list(existing.scopes)
        candidate_scopes = summary.get("scopes", [])
        if isinstance(candidate_scopes, list):
            scopes = self._merge_scope_values(scopes, candidate_scopes)
        scope_source = summary.get("scope_source") or existing.scope_source
        return Memory(
            memory_id=existing.memory_id,
            title=existing.title,
            body=existing.body,
            tags=list(existing.tags),
            type=existing.type,
            scopes=scopes,
            aliases=list(existing.aliases),
            keywords=list(existing.keywords),
            scope_source=scope_source,
            sources=self._merge_sources(existing.sources, self._core_sources(request)),
            created=existing.created,
            updated=now,
            hit_count=existing.hit_count,
            last_hit_at=existing.last_hit_at,
            status=existing.status,
            completed_at=existing.completed_at,
            extra=dict(existing.extra),
        )

    @staticmethod
    def _merge_scope_values(old: Iterable[str], current: Iterable[str]) -> list[str]:
        values: list[str] = []
        for scope in list(old) + list(current):
            if not isinstance(scope, str) or scope in values:
                continue
            values.append(scope)
        if "unscoped" in values and len(values) > 1:
            values = [scope for scope in values if scope != "unscoped"]
        return values or ["global"]

    def _write_history(self, old: Memory, *, superseded_by: str, archived_at: str) -> str:
        history_id = self._history_id(old)
        extra = dict(old.extra)
        extra.update(
            {
                "active_memory_id": old.memory_id,
                "superseded_by": superseded_by,
                "archived_at": archived_at,
            }
        )
        historical = Memory(
            memory_id=history_id,
            title=old.title,
            body=old.body,
            tags=list(old.tags),
            type=old.type,
            scopes=list(old.scopes),
            aliases=list(old.aliases),
            keywords=list(old.keywords),
            scope_source=old.scope_source,
            sources=[dict(item) for item in old.sources],
            created=old.created,
            updated=old.updated,
            hit_count=old.hit_count,
            last_hit_at=old.last_hit_at,
            status=old.status,
            completed_at=old.completed_at,
            extra=extra,
        )
        path = self.service.vault.memory_path(history_id, "history")
        if path.exists():
            if path.is_symlink():
                raise ModelOutputError("unsafe history path")
            try:
                current = Memory.from_markdown(path.read_text(encoding="utf-8"), path)
            except (OSError, UnicodeError, ValueError) as error:
                raise ModelOutputError("existing history version is invalid") from error
            if not self._same_content(current, historical, ignore_archived_at=True):
                raise ModelOutputError("history version collision")
        else:
            atomic_write_text(path, historical.to_markdown())
        return history_id

    def _write_one(
        self,
        request: Mapping[str, Any],
        *,
        now: str,
        active_records: Mapping[str, Any] | None = None,
    ) -> Memory:
        active = active_records if active_records is not None else self._active_records()
        duplicate_id = request.get("duplicate_memory_id")
        if duplicate_id is not None:
            existing_record = active.get(duplicate_id)
            if existing_record is None:
                raise ModelOutputError("duplicate target is not an active memory")
            existing = getattr(existing_record, "memory", existing_record)
            # A gate duplicate in automatic processing is a read-only
            # observation.  It must not append the query as a source or
            # rewrite metadata, because doing so creates a new version of an
            # otherwise unchanged memory.  Explicit remember keeps its
            # existing metadata-merge behavior.
            if request.get("explicit_remember") is not True:
                self.last_noop_memory_ids.add(request["memory_id"])
                return existing
            desired = self._build_duplicate_memory(request, existing, now=now)
            if self._same_content(existing, desired):
                return existing
            path = self.service.vault.memory_path(existing.memory_id, "knowledge")
            if path.is_symlink():
                raise ModelOutputError("unsafe knowledge path")
            atomic_write_text(path, desired.to_markdown())
            self.last_metadata_merged += 1
            return desired
        summary = request["summary"]
        target_id = summary.get("update_memory_id")
        existing_record = active.get(target_id) if target_id is not None else active.get(request["memory_id"])
        if target_id is not None and existing_record is None:
            raise ModelOutputError("update target is not an active memory")
        existing = (
            getattr(existing_record, "memory", existing_record)
            if existing_record is not None
            else None
        )
        if existing is not None and self._request_already_applied(request, existing):
            if request.get("explicit_remember") is not True:
                self.last_noop_memory_ids.add(request["memory_id"])
            return existing
        desired = self._build_memory(request, existing=existing, now=now)
        if existing is not None and self._same_content(existing, desired):
            if target_id is not None and request.get("explicit_remember") is not True:
                self.last_noop_memory_ids.add(request["memory_id"])
            return existing
        if (
            existing is not None
            and target_id is not None
            and request.get("explicit_remember") is not True
            and self._same_content(
                existing,
                desired,
                ignore_sources=True,
                ignore_runtime_metadata=True,
                ignore_scope_source=True,
                ignore_title=True,
            )
        ):
            self.last_noop_memory_ids.add(request["memory_id"])
            return existing
        if existing is not None:
            self._write_history(existing, superseded_by=desired.memory_id, archived_at=now)
        path = self.service.vault.memory_path(desired.memory_id, "knowledge")
        if path.is_symlink():
            raise ModelOutputError("unsafe knowledge path")
        atomic_write_text(path, desired.to_markdown())
        return desired

    def write_many_unlocked(self, requests: list[Mapping[str, Any]], *, now: str) -> list[Memory]:
        """Write a prevalidated batch; no index rebuild is performed here."""

        self.last_metadata_merged = 0
        self.last_noop_memory_ids = set()
        if not requests:
            return []
        self._preflight(requests)
        written: list[Memory] = []
        active_records = self._active_records()
        for request in requests:
            memory = self._write_one(request, now=now, active_records=active_records)
            written.append(memory)
            active_records[memory.memory_id] = memory
        return written


__all__ = ["MemoryWriter"]
