"""The local Markdown memory core API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .budget import (
    MAX_SCOPE_CATALOG_CHARS,
    MAX_SCOPE_CATALOG_ITEMS,
    MAX_SEARCH_CANDIDATE_CHARS,
    MAX_SEARCH_CANDIDATE_ITEMS,
    context_limit,
    directory_entry,
    fit_directory_items,
    payload_chars,
)
from .capture import capture_event
from .index import (
    build_tags_index,
    event_key,
    extract_event_keys,
)
from .locking import atomic_write_json, atomic_write_text, read_json
from .models import (
    CaptureResult,
    DirectoryEntry,
    ForgetAboutResult,
    Memory,
    MemoryVersionError,
    utc_now,
)
from .retrieval import (
    candidate_matches_query,
    filter_by_scope,
    fulltext_score,
    inherited_scopes,
    memory_scope_rank,
    matching_index_terms,
    normalize_term,
    RetrievalError,
)
from .scope_state import (
    ScopeError,
    normalize_scopes,
    resolve_project_path_scope,
    resolve_query_project_scope,
    validate_scope_key,
    validate_scope_registry,
)
from .vault import Vault, safe_component
from .query_scan import QueryScan, ScanRecord, scan_memories, ensure_scan_current
from .query_progress import observe_progress


@dataclass
class _Record:
    memory: Memory
    path: Path
    area: str
    score: int = 0
    scope_rank: int = 0


def _native_memory(item: Mapping[str, Any]) -> Memory:
    native_id = item.get("native_id", item.get("memory_id"))
    if not isinstance(native_id, str) or not native_id:
        raise ValueError("invalid native memory id")
    title = item.get("title")
    body = item.get("body")
    if not isinstance(title, str) or not title:
        title = str(item.get("native_source_id", "native"))
    if not isinstance(body, str):
        body = ""
    source_id = item.get("native_source_id", item.get("source", ""))
    agent = item.get("native_agent", item.get("agent", ""))
    locator = item.get("locator", "")
    share = item.get("share") is True
    return Memory(
        memory_id=native_id,
        title=title,
        body=body,
        type="other",
        scopes=["global"],
        created=utc_now(),
        updated=utc_now(),
        extra={
            "native": True,
            "native_source_id": source_id,
            "native_agent": agent,
            "locator": locator,
            "share": share,
        },
    )


def _memory_version(memory: Memory) -> str:
    """Hash stable memory content/metadata while ignoring read accounting."""

    value = memory.to_dict()
    value.pop("hit_count", None)
    value.pop("last_hit_at", None)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _native_version(native_id: str, title: str, scopes: list[str], body: str) -> str:
    payload = json.dumps(
        {
            "memory_id": native_id,
            "title": title,
            "scopes": list(scopes),
            "body": body,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _page_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _encode_page_cursor(kind: str, fingerprint: str, offset: int, clock: dict | None = None) -> str:
    payload = json.dumps(
        {"kind": kind, "fingerprint": fingerprint, "offset": offset, **({"clock": clock} if clock else {})},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_page_cursor(
    cursor: str | None,
    *,
    kind: str,
    fingerprint: str,
    maximum: int,
) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
        raise RetrievalError("invalid_cursor", "retrieval cursor is invalid")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        value = json.loads(raw.decode("ascii"))
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise RetrievalError("invalid_cursor", "retrieval cursor is invalid") from None
    if not isinstance(value, dict) or value.get("kind") != kind:
        raise RetrievalError("invalid_cursor", "retrieval cursor is invalid")
    if value.get("fingerprint") != fingerprint:
        raise RetrievalError("stale_cursor", "retrieval results changed; restart the query")
    offset = value.get("offset")
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
        or offset > maximum
        or (maximum > 0 and offset >= maximum)
    ):
        raise RetrievalError("invalid_cursor", "retrieval cursor is invalid")
    return offset


class Memleaf:
    """Local Markdown memory service; no LLM, MCP, or host integration."""

    def __init__(
        self,
        vault: Vault | Path | str | None = None,
        *,
        router: Any = None,
        model: Any = None,
        clock: Any = None,
        native_memory_reader: Any = None,
    ):
        self.vault = vault if isinstance(vault, Vault) else Vault(vault)
        self.router = router if router is not None else model
        self.clock = clock
        self.native_memory_reader = native_memory_reader

    @classmethod
    def initialize(cls, path: Path | str | None = None) -> "Memleaf":
        return cls(Vault.initialize(path))

    def capture(
        self,
        source: str,
        session_id: str,
        turn_id: str,
        role: str,
        content: str,
        event_id: Optional[str] = None,
        *,
        record: bool = True,
        visible: bool = True,
        tool_evidence: Any = None,
        source_time: Optional[str] = None,
        source_sequence: Optional[int] = None,
        message_id: Optional[str] = None,
        message_revision: Optional[str] = None,
        previous_message_revision: Optional[str] = None,
        previous_message_id: Optional[str] = None,
        final: Optional[bool] = None,
    ) -> CaptureResult:
        """Capture one visible user/assistant event into inbox."""

        return capture_event(
            self.vault,
            source=source,
            session_id=session_id,
            turn_id=turn_id,
            role=role,
            content=content,
            event_id=event_id,
            record=record,
            visible=visible,
            tool_evidence=tool_evidence,
            source_time=source_time,
            source_sequence=source_sequence,
            message_id=message_id,
            message_revision=message_revision,
            previous_message_revision=previous_message_revision,
            previous_message_id=previous_message_id,
            final=final,
        )

    def session_lineage(
        self,
        source: str,
        session_id: str,
        *,
        parent_session_id: str | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        """Persist or clear one host session's compression parent.

        This is control metadata only.  It never changes inbox events or
        memory sources; processing may use the parent solely when the child
        session has not recorded a scope of its own.
        """

        source = safe_component(source, "source")
        session_id = safe_component(session_id, "session id")
        if type(reset) is not bool:
            raise ValueError("reset must be a boolean")
        if reset:
            if parent_session_id is not None:
                raise ValueError("reset cannot include a parent session")
        else:
            if parent_session_id is None:
                raise ValueError("parent session is required")
            parent_session_id = safe_component(parent_session_id, "parent session id")
            if parent_session_id == session_id:
                raise ValueError("session cannot be its own parent")

        state_key = f"{source}/{session_id}"
        with self.vault.lock():
            self._recover_compaction_unlocked()
            from .process_common import _read_processed
            processed = _read_processed(self.vault.processed_state_path)
            sessions = processed.setdefault("sessions", {})
            if not isinstance(sessions, dict):
                raise ValueError("processed sessions are invalid")
            state = sessions.get(state_key)
            if not isinstance(state, dict):
                state = {}

            if reset:
                changed = "lineage_parent_session_id" in state
                state.pop("lineage_parent_session_id", None)
                if changed:
                    sessions[state_key] = state
                    atomic_write_json(self.vault.processed_state_path, processed)
                return {
                    "session_id": session_id,
                    "parent_session_id": None,
                    "cleared": changed,
                }

            existing = state.get("lineage_parent_session_id")
            if existing is not None and existing != parent_session_id:
                raise ValueError("session lineage parent is already bound")
            state["lineage_parent_session_id"] = parent_session_id
            sessions[state_key] = state
            atomic_write_json(self.vault.processed_state_path, processed)
            return {
                "session_id": session_id,
                "parent_session_id": parent_session_id,
                "linked": True,
            }

    def migration_preflight(self) -> dict[str, Any]:
        """Read-only local checks; never authorizes switching production routes."""
        from .migration import migration_preflight
        return migration_preflight(self)

    def backup_for_migration(self, destination: Path | str, *, expected_snapshot: str,
                             writers_stopped: bool = False) -> dict[str, Any]:
        """Create an exclusive, verified local backup of an inspected Vault."""
        from .migration import backup_for_migration
        return backup_for_migration(self, destination, expected_snapshot=expected_snapshot,
                                    writers_stopped=writers_stopped)

    def compact_runtime_state(self, *, dry_run: bool = True,
                              expected_revision: str | None = None,
                              max_records: int = 64) -> dict[str, Any]:
        """Preview or explicitly compact proven terminal control receipts."""
        from .runtime_retention import compact_runtime_state
        return compact_runtime_state(self, dry_run=dry_run,
                                     expected_revision=expected_revision, max_records=max_records)

    def process(
        self,
        *,
        source: str | None = None,
        session_id: str | None = None,
        model: Any = None,
        router: Any = None,
        scope: Any = None,
        pipeline: str | None = None,
        recover: bool = False,
    ) -> dict[str, Any]:
        """Process inbox turns through the sole incremental engine.

        The pipeline argument is retained only for compatibility. None or
        incremental selects the same runtime; legacy is rejected. recover
        authorizes only the remaining transport attempt, never an automatic
        partial semantic replan.
        """
        from .processing_route import select_pipeline, process_inbox
        select_pipeline(self.vault.config(), pipeline)
        if type(recover) is not bool:
            raise ValueError("invalid_recover")
        return process_inbox(self, source=source, session_id=session_id,
                             model=model, router=router, scope=scope, recover=recover)

    def preview_incremental(self, *, source: str, session_id: str, turn_id: str,
                            response: str | None = None, expected_snapshot: str | None = None,
                            scope: Any = None, priority_memory_ids: Iterable[str] = (),
                            candidate_limit: int = 12, allow_new_scopes: bool = False) -> dict[str, Any]:
        """Read-only staged items request/compiler; does not call a model or write."""
        from .incremental_preview import preview_incremental
        return preview_incremental(
            self, source=source, session_id=session_id, turn_id=turn_id,
            response=response, expected_snapshot=expected_snapshot, scope=scope,
            priority_memory_ids=priority_memory_ids, candidate_limit=candidate_limit,
            allow_new_scopes=allow_new_scopes,
        )

    def remember(
        self,
        content: str | None = None,
        *,
        text: str | None = None,
        source: str = "memleaf",
        session_id: str = "remember",
        turn_id: str | None = None,
        event_id: str | None = None,
        intent_id: str | None = None,
        scopes: Any = None,
        model: Any = None,
        router: Any = None,
        pipeline: str | None = None,
        recover: bool = False,
        source_time: str | None = None,
    ) -> dict[str, Any]:
        """Explicit text retention through the sole incremental engine."""
        from .processing_route import select_remember_pipeline
        select_remember_pipeline(self.vault.config(), pipeline)
        if type(recover) is not bool:
            raise ValueError("invalid_recover")
        from .remember_route import remember_text
        return remember_text(self, content=content, text=text, source=source,
                             session_id=session_id, turn_id=turn_id, event_id=event_id,
                             intent_id=intent_id, scopes=scopes, model=model, router=router,
                             recover=recover, source_time=source_time)

    def compact(self, *, model: Any = None, router: Any = None) -> dict[str, Any]:
        """Compact low-priority active knowledge through an injected model."""

        from .compaction import Compactor

        return Compactor(self).compact(model=model, router=router)

    def remember_incremental(self, *, source: str, session_id: str, turn_id: str,
                             intent_id: str, selected_source_refs: list[str], retention_request: str,
                             scope: Any = None, priority_memory_ids: Iterable[str] = (),
                             candidate_limit: int = 12, model: Any = None, router: Any = None,
                             recover: bool = False, allow_new_scopes: bool = False) -> dict[str, Any]:
        """Opt-in selected retention; does not consume the automatic source turn."""
        from .incremental_runtime import remember_incremental
        return remember_incremental(self, source=source, session_id=session_id, turn_id=turn_id,
                                    intent_id=intent_id, selected_source_refs=selected_source_refs,
                                    retention_request=retention_request, scope=scope,
                                    priority_memory_ids=priority_memory_ids, candidate_limit=candidate_limit,
                                    model=model, router=router, recover=recover,
                                    allow_new_scopes=allow_new_scopes)

    def process_incremental(self, *, source: str, session_id: str, turn_id: str,
                            scope: Any = None, priority_memory_ids: Iterable[str] = (),
                            candidate_limit: int = 12, model: Any = None, router: Any = None,
                            recover: bool = False, allow_new_scopes: bool = False) -> dict[str, Any]:
        """Configured fixed-API facade on the existing incremental runner."""
        from .incremental_runtime import process_incremental
        return process_incremental(self, source=source, session_id=session_id, turn_id=turn_id,
                                   scope=scope, priority_memory_ids=priority_memory_ids,
                                   candidate_limit=candidate_limit, model=model, router=router,
                                   recover=recover, allow_new_scopes=allow_new_scopes)

    def run_incremental(self, *, source: str, session_id: str, turn_id: str,
                        backend: Any = None, scope: Any = None,
                        priority_memory_ids=(), candidate_limit: int = 12,
                        allow_new_scopes: bool = False) -> dict[str, Any]:
        """Run one captured turn through the incremental execution engine."""
        from .incremental_execution import run_incremental
        return run_incremental(self, source=source, session_id=session_id, turn_id=turn_id,
                               backend=backend, scope=scope, priority_memory_ids=priority_memory_ids,
                               candidate_limit=candidate_limit, allow_new_scopes=allow_new_scopes)

    def recover_incremental_partial(self, run_id: str, *, mode: str = "replan",
                                    context_memory_ids=(), model: Any = None,
                                    router: Any = None) -> dict[str, Any]:
        """Explicitly repair structure or replan unresolved sources; no new budget."""
        from .incremental_partial import recover_incremental_partial
        return recover_incremental_partial(self, run_id, mode=mode,
                                           context_memory_ids=context_memory_ids,
                                           model=model, router=router)

    def resume_incremental_run(self, run_id: str, *, backend: Any = None) -> dict[str, Any]:
        """Resume a frozen response/commit without another model request when possible."""
        from .incremental_execution import resume_incremental_run
        return resume_incremental_run(self, run_id, backend=backend)

    def apply_incremental(self, *, response: str, expected_snapshot: str, intent_id: str,
                          source: str, session_id: str, turn_id: str, scope: Any = None,
                          priority_memory_ids=(), candidate_limit: int = 12,
                          allow_new_scopes: bool = False) -> dict[str, Any]:
        """Explicit staged commit; never calls a model or changes the default route."""
        from .incremental_commit import apply_incremental
        return apply_incremental(self, response=response, expected_snapshot=expected_snapshot,
                                 intent_id=intent_id, source=source, session_id=session_id,
                                 turn_id=turn_id, scope=scope, priority_memory_ids=priority_memory_ids,
                                 candidate_limit=candidate_limit, allow_new_scopes=allow_new_scopes)

    def resume_incremental(self, work_id: str) -> dict[str, Any]:
        """Resume frozen operations of a known staged work, not a new model plan."""
        from .incremental_commit import resume_incremental
        return resume_incremental(self, work_id)

    @contextmanager
    def _mutation_boundary(self):
        """Serialize permanent changes after recovering interrupted maintenance.

        The caller retains its own authorization, journal and revision checks.
        This boundary does not run models or replay pending automatic writes.
        In particular, forget may cancel a pending write before it can replay.
        """
        with self.vault.lock():
            self._recover_compaction_unlocked()
            from .incremental_journal import reconcile_applied_unlocked
            reconcile_applied_unlocked(self)
            yield

    def _recover_compaction_unlocked(self) -> None:
        """Recover a pending compaction while the vault lock is held."""

        from .compaction import Compactor

        Compactor(self)._recover_pending_unlocked()

    def _read_memories_unlocked(self, area: str) -> list[_Record]:
        records: list[_Record] = []
        for path in self.vault.list_markdown(area):
            try:
                memory = Memory.from_markdown(path.read_text(encoding="utf-8"), path)
            except (OSError, UnicodeError, ValueError):
                # A malformed user file is not allowed to prevent rebuilding
                # the index for all other source-of-truth files.
                continue
            records.append(_Record(memory=memory, path=path, area=area))
        return records

    def _records_by_id_unlocked(self, include_history: bool) -> dict[str, _Record]:
        records: dict[str, _Record] = {}
        for record in self._read_memories_unlocked("knowledge"):
            records.setdefault(record.memory.memory_id, record)
        if include_history:
            for record in self._read_memories_unlocked("history"):
                records.setdefault(record.memory.memory_id, record)
        return records

    def _find_records_unlocked(self, memory_id: str, include_history: bool = True) -> list[_Record]:
        matches = []
        for record in self._read_memories_unlocked("knowledge"):
            if record.memory.memory_id == memory_id:
                matches.append(record)
        if include_history:
            for record in self._read_memories_unlocked("history"):
                if record.memory.memory_id == memory_id:
                    matches.append(record)
        return matches

    def _forget_scan_unlocked(self) -> QueryScan:
        """A deletion must be able to account for linked historical copies."""
        snapshot = scan_memories(self.vault, include_history=True)
        if any(issue.code != "duplicate_id" for issue in snapshot.issues):
            raise RetrievalError("memory_unreadable", "cannot enumerate all deletion copies")
        return snapshot

    def _find_forget_records_unlocked(self, memory_id: str) -> list[_Record]:
        """Resolve an exact current/retired ID, or only an addressed history ID."""
        snapshot = self._forget_scan_unlocked()
        identity = memory_id.casefold()
        direct = [r for r in snapshot._all_records if r.memory.memory_id.casefold() == identity]
        current = any(r.area == "knowledge" for r in direct)
        if direct and not current:
            targets = direct
        else:
            targets = [r for r in snapshot._all_records if r in direct or (r.area == "history" and any(
                isinstance(r.memory.extra.get(k), str) and r.memory.extra[k].casefold() == identity
                for k in ("active_memory_id", "original_memory_id")))]
        ensure_scan_current(self.vault, snapshot)
        return targets

    def _forget_target_records_unlocked(self, record: _Record) -> list[_Record]:
        if record.area == "knowledge":
            return self._find_forget_records_unlocked(record.memory.memory_id)
        linked = record.memory.extra.get("active_memory_id")
        if isinstance(linked, str) and linked:
            return self._find_forget_records_unlocked(linked)
        return [record]

    @staticmethod
    def _forget_group_key(record: _Record) -> str:
        if record.area == "knowledge":
            return f"active:{record.memory.memory_id}"
        if record.area == "history":
            linked = record.memory.extra.get("active_memory_id")
            if isinstance(linked, str) and linked:
                return f"active:{linked}"
        return f"memory:{record.memory.memory_id}"

    def _forget_groups_unlocked(self, records: Iterable[_Record]) -> list[list[_Record]]:
        groups: dict[str, list[_Record]] = {}
        for record in records:
            groups.setdefault(self._forget_group_key(record), []).append(record)
        return list(groups.values())

    def _read_tags_index_unlocked(self) -> dict:
        self._recover_compaction_unlocked()
        from .incremental_journal import KEY, load_work
        from .process_common import _read_processed
        processed = _read_processed(self.vault.processed_state_path)
        works = processed.get(KEY, {})
        if not isinstance(works, dict):
            raise ValueError("invalid_incremental_ledger")
        if any(load_work(processed, key)["index_status"] != "current" for key in works):
            return build_tags_index(
                [r.memory for r in self._read_memories_unlocked("knowledge") if r.memory.validity == "valid"],
                [r.memory for r in self._read_memories_unlocked("history")],
            )
        try:
            value = read_json(self.vault.tags_index_path)
            if not isinstance(value, dict):
                raise ValueError
            for key in ("tags", "aliases", "keywords", "wikilinks"):
                if not isinstance(value.get(key), dict):
                    raise ValueError
            history = value.get("history")
            if not isinstance(history, dict):
                raise ValueError
            for key in ("tags", "aliases", "keywords", "wikilinks"):
                if not isinstance(history.get(key), dict):
                    raise ValueError
            return value
        except (OSError, ValueError, TypeError, UnicodeError, KeyError):
            self._rebuild_index_unlocked()
            try:
                value = read_json(self.vault.tags_index_path)
            except (OSError, ValueError, TypeError) as error:
                raise RuntimeError("memleaf tag index cannot be rebuilt") from error
            if not isinstance(value, dict):
                raise RuntimeError("memleaf tag index cannot be rebuilt")
            return value

    def _rebuild_index_unlocked(self) -> dict[str, int]:
        """Rebuild only data that is fully derivable from source files."""
        knowledge = self._read_memories_unlocked("knowledge")
        history = self._read_memories_unlocked("history")
        atomic_write_json(
            self.vault.tags_index_path,
            build_tags_index(
                [record.memory for record in knowledge if record.memory.validity == "valid"],
                [record.memory for record in history],
            ),
        )
        event_keys: set[str] = set()
        for path in self.vault.list_markdown("inbox"):
            try:
                event_keys.update(extract_event_keys(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError):
                continue
        return {
            "knowledge": len(knowledge),
            "history": len(history),
            "events": len(event_keys),
        }

    def rebuild_index(self) -> dict[str, Any]:
        """Rebuild Markdown indexes and fully refresh configured native sources."""

        with self.vault.lock():
            self._recover_compaction_unlocked()
            result = self._rebuild_index_unlocked()
            from .native_index import NativeIndexer

            result.update(NativeIndexer(self.vault).refresh_unlocked(full=True))
            return result

    def refresh_native_sources(self, *, full: bool = False) -> dict[str, Any]:
        """Refresh configured native files without writing to those files."""

        with self.vault.lock():
            self._recover_compaction_unlocked()
            from .native_index import NativeIndexer

            return NativeIndexer(self.vault).refresh_unlocked(full=full)

    def read_native_segment(self, source_id: str, native_id: str) -> str | None:
        """Read one indexed native segment only after current-file validation."""

        with self.vault.lock():
            self._recover_compaction_unlocked()
            from .native_index import NativeIndexer

            return NativeIndexer(self.vault)._read_segment_unlocked(source_id, native_id)

    read_native_locator = read_native_segment

    def write_memory(
        self,
        memory: Memory | Mapping[str, Any],
        area: str = "knowledge",
        *,
        overwrite: bool = True,
    ) -> Memory:
        """Trusted full-document write, not a revision-checked business update.

        The historical overwrite default is retained for existing raw import and
        administrator scripts. It does not merge fields or create history.
        Set overwrite=False for create-only identity and destination checks;
        create_memory always uses that policy. Automatic planners do not use
        this raw entry point for updates.
        """

        if type(overwrite) is not bool:
            raise TypeError("overwrite must be a boolean")
        if not isinstance(memory, Memory):
            memory = Memory.from_mapping(memory)
        if area not in ("knowledge", "history"):
            raise ValueError("invalid memory area")
        from .source_policy import MAX_MEMORY_SOURCES, merge_sources
        if len(memory.sources) > MAX_MEMORY_SOURCES or any(
            key in memory.extra for key in ("source_count", "source_digest", "sources_omitted")
        ):
            bounded_sources, source_metadata = merge_sources([], memory.sources, extra=memory.extra)
            memory.sources = bounded_sources
            memory.extra.update(source_metadata)
        path = self.vault.memory_path(memory.memory_id, area)
        with self._mutation_boundary():
            if path.is_symlink():
                raise ValueError("unsafe memory path")
            if not overwrite:
                # A renamed file still owns its frontmatter identity. Use the
                # existing bounded, case-insensitive scan across both areas;
                # skipped or ambiguous files must not be treated as absence.
                snapshot = scan_memories(self.vault, include_history=True)
                snapshot.require_identity(memory.memory_id)
                if path.exists() or any(
                    record.memory.memory_id.casefold() == memory.memory_id.casefold()
                    for record in snapshot.records
                ):
                    raise FileExistsError("memory identity or destination already exists")
                if any(issue.identity is None for issue in snapshot.issues):
                    raise ValueError("memory creation requires a complete identity scan")
                ensure_scan_current(self.vault, snapshot)
            atomic_write_text(path, memory.to_markdown())
            self._rebuild_index_unlocked()
        return memory

    save_memory = write_memory
    add_memory = write_memory

    def create_memory(
        self,
        *,
        title: str,
        body: str,
        memory_id: Optional[str] = None,
        tags: Any = None,
        type: str = "other",
        scopes: Any = None,
        aliases: Any = None,
        keywords: Any = None,
        area: str = "knowledge",
        **metadata: Any,
    ) -> Memory:
        """Create a new identity; never replace an existing current/history file.

        Repeating an explicit ID is a conflict, even with identical content.
        For a trusted full-document replacement use write_memory explicitly;
        ordinary business updates require the existing revision/history path.
        """
        memory = Memory.new(
            title=title,
            body=body,
            memory_id=memory_id,
            tags=tags,
            type=type,
            scopes=scopes,
            aliases=aliases,
            keywords=keywords,
            **metadata,
        )
        return self.write_memory(memory, area=area, overwrite=False)

    def _revision_target_unlocked(self, memory_id: str) -> tuple[ScanRecord | None, QueryScan]:
        """Resolve a current write target without treating unreadable files as absent.

        Caller holds the Vault lock and rechecks the returned snapshot at its
        observation/commit boundary. Unknown identities prevent proving unique
        ownership; unrelated, identifiable bad records retain local isolation.
        """
        snapshot = scan_memories(self.vault, include_history=False)
        snapshot.require_identity(memory_id)
        if any(issue.identity is None for issue in snapshot.issues):
            raise RetrievalError("memory_unreadable", "current memory identity scan is incomplete")
        record = next((record for record in snapshot.records
                       if record.memory.memory_id.casefold() == memory_id.casefold()), None)
        return record, snapshot

    def memory_revision(self, memory_id: str) -> str | None:
        """Return a protected revision; None means proven absence, not unreadability."""

        from .turn_plan import revision_digest

        safe_component(memory_id, "memory id")
        with self.vault.lock():
            self._recover_compaction_unlocked()
            record, snapshot = self._revision_target_unlocked(memory_id)
            ensure_scan_current(self.vault, snapshot)
            return revision_digest(record.memory) if record is not None else None

    def update_memory(
        self,
        memory_id: str,
        *,
        expected_revision: str,
        patch: Mapping[str, Any],
        authorized_scopes: list[str] | None = None,
        source_time: str | None = None,
        reopen: bool = False,
        restore: bool = False,
        allow_type_change: bool = False,
    ) -> dict[str, Any]:
        """Apply an explicit structured edit, never an inferred or upsert write.

        Omitted authorized_scopes permits only the selected current scopes.
        A scope move requires both old and new scopes. Omitted fields are kept;
        only documented nullable fields may be cleared. The shared writer saves
        history and an explicit retry resumes the same frozen operation.
        """
        from .memory_update import ExplicitUpdateManager, _request
        safe_component(memory_id, "memory id")
        if (not isinstance(expected_revision, str) or len(expected_revision) != 64
                or any(c not in "0123456789abcdef" for c in expected_revision)):
            raise ValueError("expected_revision must be a protected revision")
        with self._mutation_boundary():
            if authorized_scopes is None:
                # The trusted caller selects this exact current target. Moving
                # it to another scope still requires explicit old/new scopes.
                record, snapshot = self._revision_target_unlocked(memory_id)
                ensure_scan_current(self.vault, snapshot)
                if record is None:
                    raise ValueError("memory does not exist")
                authorized_scopes = list(record.memory.scopes)
            request = _request(patch, authorized_scopes, reopen=reopen, restore=restore,
                               allow_type_change=allow_type_change, source_time=source_time)
            return ExplicitUpdateManager(self).update_unlocked(memory_id, expected_revision, request)

    def retract_memory(
        self,
        memory_id: str,
        *,
        expected_revision: str,
        reason: str | None = None,
    ) -> Memory:
        """Retract a current assertion without deleting its stable identity.

        The previous valid state is written to history.  Ordinary retrieval no
        longer exposes the assertion, while maintenance and explicit audit can
        still locate the current retracted head.  This deterministic API is
        deliberately separate from ``forget_memory``, which deletes content.
        """

        from .memory_retraction import RetractionManager

        safe_component(memory_id, "memory id")
        if not isinstance(expected_revision, str) or not expected_revision:
            raise ValueError("expected_revision must be a non-empty string")
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError("retraction reason must be non-empty text")
        reason = reason.strip() if reason is not None else None
        with self._mutation_boundary():
            return RetractionManager(self).retract_unlocked(memory_id, expected_revision, reason)

    def read(self, memory_id: str, *, include_history: bool = False) -> Optional[Memory]:
        safe_component(memory_id, "memory id")
        with self.vault.lock():
            snapshot = scan_memories(self.vault, include_history)
            snapshot.require_identity(memory_id)
            matches = [r for r in snapshot.records if r.memory.memory_id == memory_id]
            if not matches:
                return None
            memory = matches[0].memory
            if memory.validity == "retracted" and not include_history:
                return None
            ensure_scan_current(self.vault, snapshot)
            return memory

    def read_page(
        self,
        memory_id: str,
        *,
        include_history: bool = False,
        offset: int = 0,
        max_chars: int = 2000,
        expected_version: str | None = None,
    ) -> Optional[dict[str, Any]]:
        """Read one bounded body page by id, resolving shared native ids too.

        Only an active local memory's non-empty first page updates read
        accounting.  History and native source files are read-only.
        """

        safe_component(memory_id, "memory id")
        if type(include_history) is not bool:
            raise ValueError("include_history must be a boolean")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or not 0 < max_chars <= 2000:
            raise ValueError("max_chars must be an integer between 1 and 2000")
        if expected_version is not None and (
            not isinstance(expected_version, str) or not expected_version
        ):
            raise ValueError("expected_version must be a non-empty string")

        def _page(
            *,
            title: str,
            scopes: list[str],
            body: str,
            version: str,
            count_hit: bool,
            memory_type: str = "other",
            status: str | None = None,
            due_date: str | None = None,
            validity: str = "valid",
            revision: str | None = None,
            structured_fields: Mapping[str, Any] | None = None,
            record: _Record | None = None,
        ) -> dict[str, Any]:
            if expected_version is not None and expected_version != version:
                raise MemoryVersionError("memory version mismatch")
            display = directory_entry(DirectoryEntry(memory_id, title, list(scopes)))
            total_chars = len(body)
            if offset > total_chars:
                raise RetrievalError("invalid_offset", "offset exceeds the memory body length")
            if offset == total_chars:
                page_body = ""
                next_offset = None
                has_more = False
            else:
                next_offset = min(offset + max_chars, total_chars)
                page_body = body[offset:next_offset]
                has_more = next_offset < total_chars
                if not has_more:
                    next_offset = None
            hit_status = "not_counted"
            if count_hit and offset == 0 and page_body and record is not None:
                record.memory.hit_count += 1
                record.memory.last_hit_at = utc_now()
                try:
                    atomic_write_text(record.path, record.memory.to_markdown())
                    hit_status = "counted"
                except OSError:
                    hit_status = "unavailable"
            result = {
                "memory_id": memory_id,
                "title": display.title,
                "scopes": list(display.scopes),
                "body": page_body,
                "offset": offset,
                "next_offset": next_offset,
                "has_more": has_more,
                "total_chars": total_chars,
                "version": version,
                "type": memory_type,
                "status": status,
                "due_date": due_date,
                "validity": validity,
                "revision": revision or version,
                "read_accounting": hit_status,
            }
            structured_fields = structured_fields or {}
            for name in ("assignee", "waiting_on", "due_text", "due_anchor", "due_status"):
                if name in structured_fields:
                    result[name] = structured_fields[name]
            return result

        with self.vault.lock():
            snapshot = scan_memories(self.vault, include_history)
            snapshot.require_identity(memory_id)
            matches = [r for r in snapshot.records if r.memory.memory_id == memory_id]
            if matches:
                record = matches[0]
                memory = record.memory
                if memory.validity == "retracted" and not include_history:
                    return None
                from .turn_plan import revision_digest

                ensure_scan_current(self.vault, snapshot)
                return {**_page(
                    title=memory.title,
                    scopes=list(memory.scopes),
                    body=memory.body,
                    version=_memory_version(memory),
                    count_hit=record.area == "knowledge" and record.account_ready,
                    memory_type=memory.type,
                    status=(memory.status or "active") if memory.type == "todo" else memory.status,
                    due_date=memory.due_date,
                    validity=memory.validity,
                    revision=revision_digest(memory),
                    structured_fields=memory.extra,
                    record=record,
                ), **self._query_envelope(snapshot, scope=memory.scopes)}

            from .native_index import NativeIndexer

            native = NativeIndexer(self.vault).read_shared_segment_by_id_unlocked(memory_id)
            if native is None:
                return None
            title = native["title"]
            scopes = list(native["scopes"])
            body = native["body"]
            ensure_scan_current(self.vault, snapshot)
            return {**_page(
                title=title,
                scopes=scopes,
                body=body,
                version=_native_version(memory_id, title, scopes, body),
                count_hit=False,
            ), **self._query_envelope(snapshot)}

    def _query_envelope(self, snapshot, *, scope=None) -> dict[str, Any]:
        return {"knowledge_generation": snapshot.generation,
                "scan_status": snapshot.report(scope, self.vault.config()),
                "pipeline_status": observe_progress(self.vault)}

    @staticmethod
    def _page_limit(value: int | None, default: int, maximum: int) -> int:
        if value is None:
            return default
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError("limit must be a positive integer")
        return min(value, maximum)

    def _scope_catalog_entries_unlocked(self, records=None) -> list[dict[str, Any]]:
        """Build the scope map from registry and memory metadata only."""

        try:
            registry = validate_scope_registry(self.vault.config().get("scopes", {}))
        except ScopeError as error:
            raise RetrievalError("scope_catalog_error", "scope catalog is invalid") from error

        nodes: dict[str, dict[str, Any]] = {
            "global": {"parent": None, "aliases": []}
        }
        for scope, node in registry.items():
            parent = node.get("parent")
            if parent is not None and not isinstance(parent, str):
                raise RetrievalError("scope_catalog_error", "scope catalog is invalid")
            aliases = node.get("aliases", [])
            if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
                raise RetrievalError("scope_catalog_error", "scope catalog is invalid")
            nodes.setdefault(scope, {"parent": None, "aliases": []})
            nodes[scope]["parent"] = parent
            nodes[scope]["aliases"] = list(dict.fromkeys(aliases))
            for child in node.get("children", []):
                if not isinstance(child, str):
                    continue
                child_node = nodes.setdefault(child, {"parent": None, "aliases": []})
                if child_node.get("parent") is None:
                    child_node["parent"] = scope
            if isinstance(parent, str):
                nodes.setdefault(parent, {"parent": None, "aliases": []})

        # A manually-created memory can introduce a valid scope before the
        # registry is updated.  Only its scope metadata is exposed here.
        for record in records if records is not None else self._read_memories_unlocked("knowledge"):
            if record.memory.validity != "valid":
                continue
            for raw_scope in record.memory.scopes:
                try:
                    scope = validate_scope_key(raw_scope)
                except ScopeError:
                    continue
                nodes.setdefault(scope, {"parent": None, "aliases": []})

        entries = []
        for scope, node in nodes.items():
            parent = node.get("parent")
            if parent == "global":
                parent_value: str | None = "global"
            elif parent is None:
                parent_value = None
            else:
                try:
                    parent_value = validate_scope_key(parent)
                except ScopeError as error:
                    raise RetrievalError("scope_catalog_error", "scope catalog is invalid") from error
            entries.append(
                {
                    "scope": scope,
                    "parent": parent_value,
                    "aliases": list(node.get("aliases", [])),
                }
            )
        entries.sort(key=lambda item: (item["scope"] != "global", item["scope"]))
        return entries

    @staticmethod
    def _bounded_catalog_page(
        entries: list[dict[str, Any]],
        *,
        start: int,
        limit: int,
        fingerprint: str,
        envelope: dict | None = None,
    ) -> dict[str, Any]:
        selected: list[dict[str, Any]] = []
        index = start
        while index < len(entries) and len(selected) < limit:
            candidate = entries[index]
            next_index = index + 1
            has_more = next_index < len(entries)
            next_cursor = (
                _encode_page_cursor("scope_catalog", fingerprint, next_index)
                if has_more
                else None
            )
            proposed = {
                **(envelope or {}),
                "scopes": selected + [candidate],
                "has_more": has_more,
                "next_cursor": next_cursor,
            }
            if payload_chars(proposed) > MAX_SCOPE_CATALOG_CHARS:
                if not selected:
                    raise RetrievalError(
                        "scope_catalog_item_too_large",
                        "scope catalog item exceeds the response budget",
                    )
                break
            selected.append(candidate)
            index = next_index

        has_more = index < len(entries)
        return {
            **(envelope or {}),
            "scopes": selected,
            "has_more": has_more,
            "next_cursor": _encode_page_cursor("scope_catalog", fingerprint, index)
            if has_more
            else None,
        }

    def scope_catalog(self, *, cursor: str | None = None, limit: int | None = None) -> dict[str, Any]:
        """Return a paged, metadata-only map of available memory scopes."""

        page_limit = self._page_limit(limit, MAX_SCOPE_CATALOG_ITEMS, MAX_SCOPE_CATALOG_ITEMS)
        with self.vault.lock():
            snapshot = scan_memories(self.vault)
            entries = self._scope_catalog_entries_unlocked(snapshot.area("knowledge"))
            envelope = self._query_envelope(snapshot)
            fingerprint = _page_fingerprint([entries, snapshot.generation])
            start = _decode_page_cursor(
                cursor,
                kind="scope_catalog",
                fingerprint=fingerprint,
                maximum=len(entries),
            )
            ensure_scan_current(self.vault, snapshot)
            return self._bounded_catalog_page(
                entries,
                start=start,
                limit=page_limit,
                fingerprint=fingerprint,
                envelope=envelope,
            )

    @staticmethod
    def _candidate_directory(record: _Record) -> dict[str, Any]:
        # The finalized retrieval contract deliberately keeps candidate
        # entries to memory_id + title. Scope selection already happened
        # before search via the bounded Scope Map and search input.
        display = directory_entry(record.memory)
        return {
            "memory_id": record.memory.memory_id,
            "title": display.title,
        }

    @staticmethod
    def _search_fingerprint(
        records: list[_Record],
        *,
        query: str | list[str],
        scope: str | list[str] | None,
        include_history: bool,
        todo_status: str,
    ) -> str:
        stable_records = []
        for record in records:
            memory = record.memory.to_dict()
            # Reading a page updates only accounting metadata.  It must not
            # invalidate a candidate cursor or change its continuation order.
            memory.pop("hit_count", None)
            memory.pop("last_hit_at", None)
            stable_records.append(
                {
                    "memory": memory,
                    "area": record.area,
                    "score": record.score,
                    "scope_rank": record.scope_rank,
                }
            )
        return _page_fingerprint(
            {
                "query": query,
                "scope": scope,
                "include_history": include_history,
                "todo_status": todo_status,
                "records": stable_records,
            }
        )

    @staticmethod
    def _bounded_search_page(
        candidates: list[dict[str, Any]],
        *,
        start: int,
        limit: int,
        fingerprint: str,
        envelope: dict | None = None,
    ) -> dict[str, Any]:
        selected: list[dict[str, Any]] = []
        index = start
        while index < len(candidates) and len(selected) < limit:
            next_index = index + 1
            has_more = next_index < len(candidates)
            next_cursor = (
                _encode_page_cursor("search_candidates", fingerprint, next_index)
                if has_more
                else None
            )
            proposed = {
                **(envelope or {}),
                "status": "found",
                "results": selected + [candidates[index]],
                "has_more": has_more,
                "next_cursor": next_cursor,
            }
            if payload_chars(proposed) > MAX_SEARCH_CANDIDATE_CHARS:
                if not selected:
                    raise RetrievalError(
                        "search_result_too_large",
                        "search result exceeds the response budget",
                    )
                break
            selected.append(candidates[index])
            index = next_index

        has_more = index < len(candidates)
        return {
            **(envelope or {}),
            "status": "found",
            "results": selected,
            "has_more": has_more,
            "next_cursor": _encode_page_cursor("search_candidates", fingerprint, index)
            if has_more
            else None,
        }

    def search_candidates(
        self,
        query: str | Iterable[str],
        *,
        scope: str | Iterable[str] | None = None,
        include_history: bool = False,
        todo_status: str = "active",
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Search and return only a bounded directory of candidate memories."""

        page_limit = self._page_limit(
            limit,
            MAX_SEARCH_CANDIDATE_ITEMS,
            MAX_SEARCH_CANDIDATE_ITEMS,
        )
        if isinstance(query, str):
            query_value: str | list[str] = query
        else:
            query_value = list(query)
        scope_value = self._scope_query_values(scope)
        with self.vault.lock():
            snapshot = scan_memories(self.vault, include_history)
            # The candidate-directory API is the strict public lookup
            # boundary.  Legacy ``search``/``context`` and the processing
            # pipeline deliberately retain their existing scope inference
            # semantics (for example, a temporary context scope may differ
            # from the query's project name).
            scope_value = self._validate_scope_query_unlocked(
                query_value,
                scope_value,
                self.vault.config(),
            )
            records = self._search_unlocked(
                query_value,
                scope=scope_value,
                include_history=include_history,
                todo_status=todo_status,
                limit=None,
                stable=True,
                strict_candidates=True,
                query_snapshot=snapshot,
            )
            records = [
                record
                for record in records
                if candidate_matches_query(record.memory, query_value)
            ]
            candidates = [self._candidate_directory(record) for record in records]
            fingerprint = self._search_fingerprint(
                records,
                query=query_value,
                scope=scope_value,
                include_history=include_history,
                todo_status=todo_status,
            )
            fingerprint = _page_fingerprint([fingerprint, snapshot.generation])
            envelope = self._query_envelope(snapshot, scope=scope_value)
            ensure_scan_current(self.vault, snapshot)
            start = _decode_page_cursor(
                cursor,
                kind="search_candidates",
                fingerprint=fingerprint,
                maximum=len(candidates),
            )
            if not candidates:
                if cursor is not None and start != 0:
                    raise RetrievalError("invalid_cursor", "retrieval cursor is invalid")
                return {**envelope, "status": "no_match", "results": [], "has_more": False, "next_cursor": None}
        return self._bounded_search_page(
                candidates,
                start=start,
                limit=page_limit,
                fingerprint=fingerprint,
                envelope=envelope,
            )

    @staticmethod
    def _todo_date(value: str | None, field: str) -> date | None:
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError(f"{field} must be YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{field} must be YYYY-MM-DD") from error
        if parsed.isoformat() != value:
            raise ValueError(f"{field} must be YYYY-MM-DD")
        return parsed

    @staticmethod
    def _bounded_todo_page(
        candidates: list[dict[str, Any]],
        *,
        start: int,
        limit: int,
        fingerprint: str,
        envelope: dict | None = None,
        clock: dict | None = None,
    ) -> dict[str, Any]:
        selected: list[dict[str, Any]] = []
        index = start
        while index < len(candidates) and len(selected) < limit:
            next_index = index + 1
            has_more = next_index < len(candidates)
            next_cursor = (
                _encode_page_cursor("active_todos", fingerprint, next_index, clock)
                if has_more
                else None
            )
            proposed = {
                **(envelope or {}),
                "status": "found",
                "results": selected + [candidates[index]],
                "has_more": has_more,
                "next_cursor": next_cursor,
            }
            if payload_chars(proposed) > MAX_SEARCH_CANDIDATE_CHARS:
                if not selected:
                    raise RetrievalError("search_result_too_large", "todo result exceeds the response budget")
                break
            selected.append(candidates[index])
            index = next_index
        has_more = index < len(candidates)
        return {
            **(envelope or {}),
            "status": "found",
            "results": selected,
            "has_more": has_more,
            "next_cursor": _encode_page_cursor("active_todos", fingerprint, index, clock) if has_more else None,
        }

    def list_todos(
        self,
        *,
        status: str = "active",
        scope: str | Iterable[str] | None = None,
        due_from: str | None = None,
        due_to: str | None = None,
        include_overdue: bool = True,
        include_unscheduled: bool = True,
        cursor: str | None = None,
        limit: int | None = None,
        as_of: str | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        """Enumerate current todo memories globally, independent of source session or agent."""

        if status not in {"active", "completed", "cancelled", "all"}:
            raise ValueError("invalid todo status")
        if type(include_overdue) is not bool or type(include_unscheduled) is not bool:
            raise ValueError("todo inclusion flags must be booleans")
        lower = self._todo_date(due_from, "due_from")
        upper = self._todo_date(due_to, "due_to")
        if lower is not None and upper is not None and lower > upper:
            raise ValueError("due_from must not be after due_to")
        page_limit = self._page_limit(limit, MAX_SEARCH_CANDIDATE_ITEMS, MAX_SEARCH_CANDIDATE_ITEMS)
        scope_value = self._scope_query_values(scope)
        from .query_clock import query_clock
        clock = query_clock(cursor, as_of, timezone)
        with self.vault.lock():
            snapshot = scan_memories(self.vault, status != "active")
            active_records = [
                record
                for record in snapshot.area("knowledge")
                if record.memory.type == "todo" and record.memory.validity == "valid"
            ]
            active_ids = {record.memory.memory_id.casefold() for record in snapshot.area("knowledge")}
            records = list(active_records)
            if status in {"completed", "cancelled", "all"}:
                records.extend(
                    record
                    for record in snapshot.area("history")
                    if record.memory.type == "todo"
                    and record.memory.extra.get("invalidated_reason") == "todo_closed"
                    and str(record.memory.extra.get("active_memory_id", "")).casefold() not in active_ids | snapshot.ambiguous
                )
            if scope_value is not None:
                try:
                    requested = normalize_scopes(scope_value, field="todo scope")
                except ScopeError as error:
                    raise RetrievalError("invalid_scope", "todo scope is invalid") from error
                if not requested:
                    raise RetrievalError("invalid_scope", "todo scope is invalid")
                # Complete enumeration is not a same-title relevance overlay.
                allowed = inherited_scopes(requested, self.vault.config())
                records = [r for r in records if memory_scope_rank(r.memory, allowed) >= 0]

            today = date.fromisoformat(clock["as_of"])
            unresolved_dates = unscheduled_dates = 0
            filtered: list[tuple[tuple[Any, ...], _Record]] = []
            for record in records:
                memory = record.memory
                current_status = memory.status or "active"
                if status != "all" and current_status != status:
                    continue
                parsed_due = self._todo_date(memory.due_date, "due_date") if memory.due_date is not None else None
                if parsed_due is None:
                    if memory.extra.get("due_text") and memory.extra.get("due_status") != "cleared":
                        unresolved_dates += 1
                    else:
                        unscheduled_dates += 1
                    if not include_unscheduled:
                        continue
                    sort_key = (3, date.max, memory.title.casefold(), memory.memory_id)
                else:
                    in_range = (lower is None or parsed_due >= lower) and (upper is None or parsed_due <= upper)
                    overdue = include_overdue and parsed_due < today and (upper is None or parsed_due <= upper)
                    if (lower is not None or upper is not None) and not (in_range or overdue):
                        continue
                    bucket = 0 if parsed_due < today else 1 if parsed_due == today else 2
                    sort_key = (bucket, parsed_due, memory.title.casefold(), memory.memory_id)
                filtered.append((sort_key, record))

            filtered.sort(key=lambda item: item[0])
            ordered = [record for _key, record in filtered]
            candidates = [
                {
                    "memory_id": record.memory.memory_id,
                    "title": directory_entry(record.memory).title,
                    "due_date": record.memory.due_date,
                    "history": record.area == "history",
                    **(
                        {"active_memory_id": record.memory.extra.get("active_memory_id")}
                        if record.area == "history" and isinstance(record.memory.extra.get("active_memory_id"), str)
                        else {}
                    ),
                }
                for record in ordered
            ]
            fingerprint = _page_fingerprint(
                {
                    "filters": {
                        "status": status,
                        "scope": scope_value,
                        "due_from": due_from,
                        "due_to": due_to,
                        "include_overdue": include_overdue,
                        "include_unscheduled": include_unscheduled,
                        "clock": clock,
                        "generation": snapshot.generation,
                    },
                    "records": [
                        {
                            "memory_id": record.memory.memory_id,
                            "version": _memory_version(record.memory),
                        }
                        for record in ordered
                    ],
                }
            )
            envelope = {**self._query_envelope(snapshot, scope=scope_value), "query_clock": clock,
                        "date_counts": {"unresolved": unresolved_dates, "unscheduled": unscheduled_dates}}
            ensure_scan_current(self.vault, snapshot)
            start = _decode_page_cursor(cursor, kind="active_todos", fingerprint=fingerprint, maximum=len(candidates))
            if not candidates:
                return {**envelope, "status": "no_match", "results": [], "has_more": False, "next_cursor": None}
            return self._bounded_todo_page(candidates, start=start, limit=page_limit, fingerprint=fingerprint, envelope=envelope, clock=clock)

    @staticmethod
    def _scope_query_values(scope: str | Iterable[str] | None) -> str | list[str] | None:
        if scope is None or isinstance(scope, str):
            return scope
        return list(scope)

    def _validate_scope_query_unlocked(
        self,
        query: str | Iterable[str],
        scope: str | Iterable[str] | None,
        config: Mapping[str, Any],
    ) -> str | list[str] | None:
        scope_value = self._scope_query_values(scope)
        if scope_value is None:
            return None
        try:
            requested = normalize_scopes(scope_value, field="search scope")
        except ScopeError as error:
            raise RetrievalError("invalid_scope", "search scope is invalid") from error
        query_project = resolve_query_project_scope(query, config)
        if query_project is None or "global" in requested:
            return scope_value
        query_ancestors = set(inherited_scopes([query_project], config)) - {"global"}
        requested_projects = [item for item in requested if item.startswith("project:")]
        if requested_projects:
            # An explicit project scope is a precise restriction.  A sibling
            # project must not be accepted merely because both share a parent.
            mismatch = query_project not in requested_projects
        else:
            # Domain/portfolio scopes are valid when they are ancestors of the
            # project named in the query.
            mismatch = not any(item in query_ancestors for item in requested)
        if mismatch:
            raise RetrievalError("scope_mismatch", "query project conflicts with search scope")
        return scope_value

    def _search_unlocked(
        self,
        query: str | Iterable[str],
        *,
        scope: str | Iterable[str] | None,
        include_history: bool,
        todo_status: str,
        limit: Optional[int],
        context_only_global: bool = False,
        stable: bool = False,
        strict_candidates: bool = False,
        include_retracted: bool = False,
        query_snapshot=None,
    ) -> list[_Record]:
        if isinstance(query, str):
            query_value: str | list[str] = query
        else:
            query_value = list(query)
        scope_value = self._scope_query_values(scope)
        config = self.vault.config()
        if context_only_global and scope_value is None:
            scope_value = "global"
        if not query_value or (isinstance(query_value, str) and not query_value.strip()):
            return []
        if todo_status not in ("active", "completed", "cancelled", "all"):
            raise ValueError("invalid todo status")

        if query_snapshot is None:
            index = self._read_tags_index_unlocked()
            active_records = self._read_memories_unlocked("knowledge")
            history_records = self._read_memories_unlocked("history") if include_history else []
        else:
            active_records = query_snapshot.area("knowledge")
            history_records = query_snapshot.area("history")
            index = build_tags_index([r.memory for r in active_records], [r.memory for r in history_records])
        from collections import Counter
        claims = Counter(r.memory.memory_id.casefold() for r in active_records + history_records)
        by_id: dict[str, _Record] = {}
        for record in active_records + history_records:
            if claims[record.memory.memory_id.casefold()] != 1:
                continue
            if record.memory.validity == "retracted" and not include_retracted:
                continue
            by_id.setdefault(record.memory.memory_id, record)

        # Apply scope before selecting the indexed first layer.  Otherwise a
        # tag hit in another project can suppress a valid full-text hit in the
        # requested project.
        scoped = filter_by_scope(
            [record.memory for record in by_id.values()],
            scope_value,
            config,
        )
        ranks = {memory.memory_id: rank for memory, rank in scoped}
        scoped_by_id = {memory_id: by_id[memory_id] for memory_id in ranks}

        active_index = {
            "tags": index.get("tags", {}),
            "aliases": index.get("aliases", {}),
            "keywords": index.get("keywords", {}),
            "wikilinks": index.get("wikilinks", {}),
        }
        tag_scores = matching_index_terms(active_index, query_value)
        if include_history:
            history_index = index.get("history", {})
            if isinstance(history_index, Mapping):
                historical_scores = matching_index_terms(history_index, query_value)
                for memory_id, score in historical_scores.items():
                    if memory_id not in tag_scores:
                        tag_scores[memory_id] = score
                    else:
                        tag_scores[memory_id] += score

        records: list[_Record] = []
        scoped_tag_scores = {
            memory_id: score
            for memory_id, score in tag_scores.items()
            if memory_id in scoped_by_id
        }
        if scoped_tag_scores:
            for memory_id, score in scoped_tag_scores.items():
                record = scoped_by_id.get(memory_id)
                if record is not None:
                    # Keep the indexed candidate set (the first retrieval
                    # layer), but use local full-text relevance to rank an
                    # exact title/body match above a common component tag.
                    record.score = score + fulltext_score(record.memory, query_value)
                    records.append(record)
        else:
            for record in scoped_by_id.values():
                score = fulltext_score(record.memory, query_value)
                if score:
                    record.score = score
                    records.append(record)

        if strict_candidates:
            # The legacy indexed-first path can hide an unindexed title/body
            # match behind a low-signal tag hit.  Only the public candidate
            # boundary opts into this union; ordinary search and processing
            # retain their historical first-layer behavior.
            indexed_ids = {record.memory.memory_id for record in records}
            for record in scoped_by_id.values():
                if record.memory.memory_id in indexed_ids:
                    continue
                if not candidate_matches_query(record.memory, query_value):
                    continue
                record.score = fulltext_score(record.memory, query_value)
                records.append(record)

        # An exact identifier is an explicit lookup intent.  Keep it in the
        # candidate set even when no indexed field contains the identifier and
        # rank it above ordinary term matches.
        normalized_query = normalize_term(
            query_value if isinstance(query_value, str) else " ".join(query_value)
        )
        if normalized_query:
            for record in scoped_by_id.values():
                if normalize_term(record.memory.memory_id) != normalized_query:
                    continue
                if record not in records:
                    records.append(record)
                record.score = max(record.score, 0) + 100000

        filtered: list[tuple[_Record, int]] = []
        for record in records:
            if record.memory.memory_id not in ranks:
                continue
            if record.memory.type == "todo":
                status = record.memory.status or "active"
                if todo_status != "all" and status != todo_status:
                    continue
            record.scope_rank = ranks[record.memory.memory_id]
            filtered.append((record, record.scope_rank))

        if stable:
            filtered.sort(
                key=lambda pair: (
                    pair[0].score,
                    pair[1],
                    pair[0].memory.updated,
                    pair[0].memory.memory_id,
                ),
                reverse=True,
            )
        else:
            filtered.sort(
                key=lambda pair: (
                    pair[0].score,
                    pair[1],
                    pair[0].memory.hit_count,
                    pair[0].memory.updated,
                    pair[0].memory.memory_id,
                ),
                reverse=True,
            )
        results = [record for record, _ in filtered]
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
                raise ValueError("limit must be a non-negative integer")
            results = results[:limit]
        return results

    def search(
        self,
        query: str | Iterable[str],
        *,
        scope: str | Iterable[str] | None = None,
        include_history: bool = False,
        todo_status: str = "active",
        limit: Optional[int] = None,
        view: str = "full",
    ) -> list[Memory] | list[DirectoryEntry]:
        """Search local memory; opt into the bounded body-free directory view."""

        if view not in ("full", "directory"):
            raise ValueError("view must be full or directory")

        with self.vault.lock():
            self._recover_compaction_unlocked()
            memories = [
                record.memory
                for record in self._search_unlocked(
                    query,
                    scope=scope,
                    include_history=include_history,
                    todo_status=todo_status,
                    limit=limit,
                )
            ]
            if view == "directory":
                return fit_directory_items(
                    (directory_entry(memory) for memory in memories),
                    limit=limit,
                )
            return memories

    def context(
        self,
        query: str | Iterable[str],
        *,
        scope: str | Iterable[str] | None = None,
        source: str | None = None,
        session_id: str | None = None,
        project_path: str | Path | None = None,
        include_history: bool = False,
        todo_status: str = "active",
        limit: Optional[int] = None,
    ) -> list[DirectoryEntry]:
        """Return an automatic body-free directory under the 3/600 budget."""

        if source is not None:
            source = safe_component(source, "source")
        if session_id is not None:
            session_id = safe_component(session_id, "session id")
        if (source is None) != (session_id is None):
            raise ValueError("context source and session_id must be provided together")
        bounded_limit = context_limit(limit)
        if isinstance(query, str):
            query_value: str | list[Any] = query
            if not query.strip():
                return []
        else:
            query_value = list(query)
            if not query_value or not any(str(item).strip() for item in query_value):
                return []
        session_key = f"{source}/{session_id}" if source is not None and session_id is not None else None
        with self.vault.lock():
            self._recover_compaction_unlocked()
            config = self.vault.config()
            explicit_scope = None
            if scope is not None:
                try:
                    explicit_scope = normalize_scopes(scope, field="context scope")
                except ScopeError as error:
                    raise ValueError("invalid context scope") from error

            saved_scopes: list[str] = []
            processed: dict[str, Any] | None = None
            state: dict[str, Any] | None = None
            if session_key is not None:
                try:
                    loaded = read_json(self.vault.processed_state_path)
                except (OSError, UnicodeError, TypeError, ValueError):
                    loaded = {}
                if isinstance(loaded, dict):
                    processed = loaded
                    sessions = processed.get("sessions")
                    if isinstance(sessions, Mapping):
                        loaded_state = sessions.get(session_key)
                        if isinstance(loaded_state, Mapping):
                            state = dict(loaded_state)
                            for state_field in ("scopes", "scope_background", "scope"):
                                if state_field not in state:
                                    continue
                                try:
                                    saved_scopes = normalize_scopes(state[state_field], field="session scopes")
                                except ScopeError:
                                    saved_scopes = []
                                if saved_scopes:
                                    break

            query_scope = resolve_query_project_scope(query_value, config)
            path_scope = resolve_project_path_scope(
                project_path,
                config,
                base_dir=self.vault.root,
            )
            if explicit_scope is not None:
                effective_scope = explicit_scope
            elif query_scope is not None:
                effective_scope = [query_scope]
            elif path_scope is not None:
                effective_scope = [path_scope]
            elif saved_scopes:
                effective_scope = saved_scopes
            else:
                effective_scope = ["global"]

            # A concrete configured project directory may initialize a truly
            # new session.  Query/path overrides for an existing session stay
            # temporary and do not mutate the saved state.
            if session_key is not None and path_scope is not None and not saved_scopes:
                if processed is None:
                    processed = {"version": 1, "event_keys": [], "events": {}, "sessions": {}}
                sessions = processed.setdefault("sessions", {})
                if not isinstance(sessions, dict):
                    raise RuntimeError("memleaf processed sessions are invalid")
                updated_state = dict(state or {})
                updated_state["scopes"] = [path_scope]
                sessions[session_key] = updated_state
                atomic_write_json(self.vault.processed_state_path, processed)
            records = self._search_unlocked(
                query_value,
                scope=effective_scope,
                include_history=include_history,
                todo_status=todo_status,
                limit=None,
            )
            from .native_index import NativeIndexer

            native_items = NativeIndexer(self.vault).search_unlocked(
                query_value,
                target_agent=source,
                for_context=True,
                limit=None,
            )
            native_records: list[Memory] = []
            for item in native_items:
                try:
                    native_records.append(_native_memory(item))
                except (TypeError, ValueError):
                    continue
            scoped_native = filter_by_scope(native_records, effective_scope, config)
            native_records = [memory for memory, _ in scoped_native]

            combined: list[tuple[Memory, _Record | None]] = []
            seen_bodies: set[str] = set()
            for record in records:
                normalized_body = normalize_term(record.memory.body)
                if normalized_body and normalized_body in seen_bodies:
                    continue
                if normalized_body:
                    seen_bodies.add(normalized_body)
                combined.append((record.memory, record))
            for memory in native_records:
                normalized_body = normalize_term(memory.body)
                if normalized_body and normalized_body in seen_bodies:
                    continue
                if normalized_body:
                    seen_bodies.add(normalized_body)
                combined.append((memory, None))
            entries = [directory_entry(memory) for memory, _ in combined]
            return fit_directory_items(entries, limit=bounded_limit)

    def _delete_records_unlocked(self, records: Iterable[_Record]) -> list[str]:
        from .memory_commit import MemoryCommitter

        return MemoryCommitter.forget_records_unlocked(self, records)

    def forget_memory(self, memory_id: str) -> bool:
        """Delete an exact memleaf memory without creating a history copy."""

        safe_component(memory_id, "memory id")
        with self._mutation_boundary():
            deleted = self._delete_records_unlocked(self._find_forget_records_unlocked(memory_id))
            return bool(deleted)

    def forget_about(self, query: str) -> ForgetAboutResult:
        """Delete one reliably identified target, otherwise return candidates."""

        if not isinstance(query, str) or not query.strip():
            return ForgetAboutResult(status="not_found")
        normalized = normalize_term(query)
        with self._mutation_boundary():
            # An explicit stable ID (including a retired ID) needs no second
            # confirmation. A single fuzzy hit, however, is not authorization.
            snapshot = self._forget_scan_unlocked()
            exact = []
            if "\n" not in query and "\r" not in query:
                try:
                    safe_component(query, "memory id")
                    exact = self._find_forget_records_unlocked(query)
                except ValueError as error:
                    if isinstance(error, RetrievalError):
                        raise
            if exact:
                ensure_scan_current(self.vault, snapshot)
                deleted = self._delete_records_unlocked(exact)
                return ForgetAboutResult(status="deleted", deleted=deleted)

            records = self._search_unlocked(
                query,
                scope=None,
                include_history=True,
                todo_status="all",
                limit=None,
                include_retracted=True,
            )
            if any(r.memory.memory_id.casefold() in snapshot.ambiguous and normalize_term(r.memory.title) == normalized
                   for r in snapshot._all_records):
                raise RetrievalError("memory_id_conflict", "use an exact identity to forget conflicting copies")
            title_matches = [record for record in snapshot.records if normalize_term(record.memory.title) == normalized]
            title_groups = self._forget_groups_unlocked(title_matches)
            if len(title_groups) == 1:
                ensure_scan_current(self.vault, snapshot)
                deleted = self._delete_records_unlocked(self._forget_target_records_unlocked(title_groups[0][0]))
                return ForgetAboutResult(status="deleted", deleted=deleted)
            groups = self._forget_groups_unlocked(records)
            if records:
                return ForgetAboutResult(
                    status="ambiguous",
                    candidates=[group[0].memory for group in groups],
                )
            return ForgetAboutResult(status="not_found")

    def stats(self) -> dict[str, Any]:
        """Return counts only; no memory content is logged or exposed."""

        with self.vault.lock():
            self._recover_compaction_unlocked()
            from .compaction import estimate_active_tokens

            knowledge = self._read_memories_unlocked("knowledge")
            history = self._read_memories_unlocked("history")
            config = self.vault.config()
            process = config.get("process") if isinstance(config, Mapping) else None
            threshold = process.get("memory_compact_threshold_tokens") if isinstance(process, Mapping) else None
            ratio = process.get("memory_compact_candidate_ratio") if isinstance(process, Mapping) else None
            closed_todo_days = process.get("closed_todo_retention_days") if isinstance(process, Mapping) else None
            history_config = config.get("history") if isinstance(config, Mapping) else None
            active_tokens = estimate_active_tokens([record.memory for record in knowledge])
            inbox_files = self.vault.list_markdown("inbox")
            event_count = 0
            inbox_bytes = 0
            for path in inbox_files:
                try:
                    event_count += len(extract_event_keys(path.read_text(encoding="utf-8")))
                    inbox_bytes += path.stat().st_size
                except (OSError, UnicodeError):
                    continue
            tags = set()
            aliases = set()
            keywords = set()
            for record in knowledge + history:
                tags.update(record.memory.tags)
                aliases.update(record.memory.aliases)
                keywords.update(record.memory.keywords)
            native_sources = 0
            native_segments = 0
            native_unavailable = 0
            try:
                from .native_index import NativeIndexer

                native_index = NativeIndexer(self.vault)._load_index_unlocked()
                native_summary = NativeIndexer.summary(native_index)
                native_sources = native_summary["native_sources"]
                native_segments = native_summary["native_segments"]
                native_unavailable = native_summary["native_unavailable"]
            except Exception:
                # Stats remain useful if a user damaged the rebuildable native
                # index; refresh/rebuild will report the concrete failure.
                pass
            return {
                "vault": str(self.vault.root),
                "knowledge": len(knowledge),
                "history": len(history),
                "inbox_files": len(inbox_files),
                "events": event_count,
                "tags": len(tags),
                "aliases": len(aliases),
                "keywords": len(keywords),
                "inbox_bytes": inbox_bytes,
                "active_tokens_estimate": active_tokens,
                "threshold": threshold,
                "compaction_threshold_tokens": threshold,
                "compaction_candidate_ratio": ratio,
                "compaction_due": isinstance(threshold, int) and active_tokens >= threshold,
                "closed_todo_retention_days": closed_todo_days,
                "history_policy": history_config.get("policy") if isinstance(history_config, Mapping) else None,
                "history_retention_days": history_config.get("retention_days") if isinstance(history_config, Mapping) else None,
                "history_max_versions_per_memory": history_config.get("max_versions_per_memory") if isinstance(history_config, Mapping) else None,
                "native_sources": native_sources,
                "native_segments": native_segments,
                "native_unavailable": native_unavailable,
            }


MemoryService = Memleaf
Core = Memleaf
