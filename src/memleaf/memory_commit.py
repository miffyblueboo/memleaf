"""The shared automatic/explicit commit boundary and forward recovery."""
from __future__ import annotations
import json
from typing import Any, Iterable, Mapping
from .config import save_config
from .memory_writer import MemoryWriter
from .turn_plan import dedup_digest, TurnPlan, FrozenTurn, content_digest, revision_digest, turn_plan_key
from .models import Memory
from .native_index import NativeIndexer
from .scope_state import ScopeError, register_scope_nodes
from .scope_maintenance import ScopeMaintainer, ScopeMaintenanceError
from .process_common import ProcessingError, _IDLE_STATUS, _Snapshot, _add_hours, _as_int, _read_processed, _session_key


class MemoryCommitter:
    def __init__(self, service: Any, writer: Any, audit: Any, journal: Any):
        self.service = service
        self.writer = writer
        self.audit = audit
        self.journal = journal

    def _validate_target_revisions_unlocked(self, requests: list[Mapping[str, Any]]) -> None:
        active = self.writer._active_records()
        written_targets: dict[str, tuple[str, str]] = {}
        for request in requests:
            correction = request.get("scope_correction")
            if isinstance(correction, Mapping) and correction.get("survivor_memory_id"):
                if self.writer.retirement_applied(correction, active):
                    continue
                for key, revision_key in (("target_memory_id", "expected_target_revision"),
                                          ("survivor_memory_id", "expected_survivor_revision")):
                    record = active.get(correction[key])
                    current = getattr(record, "memory", record)
                    expected = correction.get(revision_key)
                    if current is None or (expected and revision_digest(current) != expected):
                        raise ProcessingError("scope correction target changed before commit")
                continue
            target = request.get("summary", {}).get("update_memory_id")
            turn = request["turn"]
            owner = (turn.source, turn.session_id)
            if not target:
                if not request.get("duplicate_memory_id"):
                    written_targets.setdefault(request["memory_id"], owner)
                continue
            if not request.get("expected_revision"):
                continue
            if target in written_targets:
                if written_targets[target] != owner:
                    raise ProcessingError("competing sessions selected one update target")
                continue
            written_targets[target] = owner
            record = active.get(target)
            current = getattr(record, "memory", record)
            if current is None:
                raise ProcessingError("update target disappeared before commit")
            if MemoryWriter._request_already_applied(request, current):
                continue
            if revision_digest(current) != request["expected_revision"]:
                raise ProcessingError("update target changed before commit; no stale overwrite")


    def _commit_success(
        self,
        snapshots: list[_Snapshot],
        requests: list[Mapping[str, Any]],
        *,
        now: str,
        cleanup_hours: int,
        observed_scopes: Mapping[tuple[str, str, str], Iterable[str]] | None = None,
        deferred_candidates: Mapping[tuple[str, str, str], Iterable[Mapping[str, Any]]] | None = None,
    ) -> list[str]:
        if not snapshots:
            return []
        by_snapshot: dict[str, list[Mapping[str, Any]]] = {}
        for request in requests:
            turn = request["turn"]
            key = _session_key(turn.source, turn.session_id)
            by_snapshot.setdefault(key, []).append(request)
        with self.service.vault.lock():
            processed = _read_processed(self.service.vault.processed_index_path)
            sessions = processed.setdefault("sessions", {})
            for snapshot in snapshots:
                state = sessions.get(snapshot.state_key)
                marker = state.get("processing") if isinstance(state, Mapping) else None
                if not isinstance(marker, Mapping) or marker.get("token") != snapshot.token:
                    raise ProcessingError("processing ownership changed")
            written: list[Memory] = []
            all_requests = list(requests)
            claimed_native: dict[str, str] = {}
            for request in all_requests:
                summary = request.get("summary", {})
                shadow_ids = summary.get("shadow_native_ids", [])
                refs = request.get("native_refs", [])
                ref_ids = {
                    item.get("native_id").casefold()
                    for item in refs
                    if isinstance(item, Mapping) and isinstance(item.get("native_id"), str)
                }
                if not isinstance(shadow_ids, list) or any(
                    not isinstance(native_id, str) or native_id.casefold() not in ref_ids
                    for native_id in shadow_ids
                ):
                    raise ProcessingError("shadow native reference is not related to this turn")
                for native_id in shadow_ids:
                    key = native_id.casefold()
                    if key in claimed_native:
                        raise ProcessingError("native segment is shadowed more than once in one batch")
                    claimed_native[key] = request["memory_id"]

            scope_values: list[str] = []
            session_scope_updates: dict[str, list[str]] = {}
            scope_operations: list[Mapping[str, Any]] = []
            for snapshot in snapshots:
                scope_key = (
                    snapshot.turn.source,
                    snapshot.turn.session_id,
                    snapshot.turn.turn_key,
                )
                values = list((observed_scopes or {}).get(scope_key, []))
                if values:
                    session_scope_updates[snapshot.state_key] = values
                    for observed_scope in values:
                        if observed_scope not in scope_values:
                            scope_values.append(observed_scope)
            for request in all_requests:
                operations = request.get("summary", {}).get("scope_operations", [])
                if not isinstance(operations, list):
                    raise ProcessingError("invalid scope operations")
                scope_operations.extend(
                    operation for operation in operations if isinstance(operation, Mapping)
                )

            prepared_scopes = None
            if scope_operations:
                try:
                    prepared_scopes = ScopeMaintainer(self.service).prepare(
                        self.service.vault.config(),
                        operations=scope_operations,
                        observed_scopes=scope_values,
                        session_scopes=session_scope_updates,
                    )
                except (OSError, UnicodeError, ValueError, TypeError, ScopeMaintenanceError) as error:
                    raise ProcessingError("scope maintenance preflight failed") from error
                # Feed the canonical post-operation scopes to the writer too.
                # Otherwise a retry could briefly rewrite an already-migrated
                # active memory with the old source scope and create history.
                for request in all_requests:
                    summary = request.get("summary")
                    if not isinstance(summary, dict):
                        continue
                    values = summary.get("scopes")
                    if not isinstance(values, list):
                        continue
                    migrated: list[str] = []
                    for value in values:
                        current = value
                        seen: set[str] = set()
                        while current in prepared_scopes.migrations:
                            if current in seen:
                                raise ProcessingError("scope migration contains a cycle")
                            seen.add(current)
                            current = prepared_scopes.migrations[current]
                        if current not in migrated:
                            migrated.append(current)
                    summary["scopes"] = migrated
            active_effects: dict[str, str] = {}
            for record in self.writer._active_records().values():
                memory = getattr(record, "memory", record)
                active_effects.setdefault(dedup_digest(memory.to_dict()), memory.memory_id)
            for request in all_requests:
                summary = request["summary"]
                if (request.get("explicit_remember") or request.get("scope_correction")
                    or request.get("duplicate_memory_id") or summary.get("update_memory_id")):
                    continue
                fingerprint = dedup_digest(summary)
                existing_id = active_effects.get(fingerprint)
                if existing_id and existing_id != request["memory_id"]:
                    request["duplicate_memory_id"] = existing_id
                else:
                    active_effects[fingerprint] = request["memory_id"]
            self._validate_target_revisions_unlocked(all_requests)
            frozen = processed.setdefault("pending_turn_plans", {})
            for snapshot in snapshots:
                ref = (snapshot.turn.source, snapshot.turn.session_id, snapshot.turn.turn_key)
                key = turn_plan_key(snapshot.turn)
                relevant = [r for r in all_requests if r["turn"] == snapshot.turn]
                if key not in frozen:
                    frozen[key] = FrozenTurn.build(snapshot.turn, relevant,
                        scopes=(observed_scopes or {}).get(ref, ()),
                        candidates=self.audit._dispositions_by_turn.get(ref, ()),
                        evidence=self.audit._evidence_by_turn.get(ref, ()),
                        deferred=(deferred_candidates or {}).get(ref, ())).to_dict()
            if len(json.dumps(frozen, ensure_ascii=False).encode()) > 16 * 1024 * 1024:
                raise ProcessingError("pending write plans exceed safe storage budget")
            plan = TurnPlan.from_requests(all_requests)
            operations = processed.setdefault("pending_operations", {})
            previous_operation_ids = set(operations)
            for operation in plan.candidates:
                operations.setdefault(operation.operation_id, operation.to_dict())
            if frozen or plan.candidates:
                # Freeze final payloads before any knowledge/history mutation.
                self.journal._write_processed_unlocked(processed)
            if all_requests:
                written = self.writer.write_many_unlocked(all_requests, now=now)
            else:
                self.writer.last_metadata_merged = 0
                self.writer.last_noop_memory_ids = set()
            noop_memory_ids = self.writer.last_noop_memory_ids
            for request in all_requests:
                request_memory_id = request.get("memory_id")
                if request_memory_id not in noop_memory_ids:
                    continue
                duplicate_id = request.get("duplicate_memory_id")
                summary = request.get("summary")
                update_id = (
                    summary.get("update_memory_id")
                    if isinstance(summary, Mapping)
                    else None
                )
                target_id = (
                    duplicate_id
                    if isinstance(duplicate_id, str) and duplicate_id
                    else update_id
                    if isinstance(update_id, str) and update_id
                    else None
                )
                self.audit._record_request_disposition(
                    request,
                    "NO_CHANGE",
                    reason=(
                        "duplicate"
                        if isinstance(duplicate_id, str) and duplicate_id
                        else "unchanged"
                    ),
                    memory_id=target_id,
                )
            if prepared_scopes is not None:
                try:
                    ScopeMaintainer(self.service).apply_unlocked(
                        processed,
                        prepared_scopes,
                        session_scope_updates,
                    )
                except (OSError, UnicodeError, ValueError, TypeError, ScopeMaintenanceError) as error:
                    raise ProcessingError("scope maintenance commit failed") from error
            else:
                self.service._rebuild_index_unlocked()
                if scope_values:
                    try:
                        config = self.service.vault.config()
                        updated_config = register_scope_nodes(config, scope_values)
                    except ScopeError as error:
                        raise ProcessingError("invalid observed session scope") from error
                    if updated_config != config:
                        save_config(self.service.vault.config_path, updated_config)

            native_indexer = NativeIndexer(self.service.vault)
            for request, memory in zip(all_requests, written):
                shadow_ids = request["summary"].get("shadow_native_ids", [])
                if not shadow_ids:
                    continue
                # A prior attempt may have persisted the memory but failed
                # while applying its native shadow.  Re-apply that side
                # effect on retry even when the memory request itself was
                # recognized as already applied.
                shadow_keys = {value.casefold() for value in shadow_ids}
                refs = [
                    item
                    for item in request.get("native_refs", [])
                    if isinstance(item, Mapping)
                    and isinstance(item.get("native_id"), str)
                    and item["native_id"].casefold() in shadow_keys
                ]
                native_indexer.apply_shadow_unlocked(refs, memory.memory_id)
            processed = _read_processed(self.service.vault.processed_index_path)
            sessions = processed.setdefault("sessions", {})
            memory_ids_by_turn: dict[tuple[str, str], list[str]] = {}
            for request, memory in zip(all_requests, written):
                if request.get("memory_id") in noop_memory_ids:
                    continue
                turn = request["turn"]
                memory_ids_by_turn.setdefault((turn.source, turn.session_id, turn.turn_key), []).append(memory.memory_id)
            for snapshot in snapshots:
                state = sessions.get(snapshot.state_key)
                if not isinstance(state, dict):
                    raise ProcessingError("processing session disappeared")
                marker = state.get("processing")
                if not isinstance(marker, Mapping) or marker.get("token") != snapshot.token:
                    raise ProcessingError("processing ownership changed")
                entries = state.get("processed_turns")
                if not isinstance(entries, list):
                    entries = []
                existing_entry = next(
                    (
                        entry
                        for entry in entries
                        if isinstance(entry, Mapping) and entry.get("turn_key") == snapshot.turn.turn_key
                    ),
                    None,
                )
                ids = memory_ids_by_turn.get(
                    (snapshot.turn.source, snapshot.turn.session_id, snapshot.turn.turn_key), []
                )
                if isinstance(existing_entry, dict):
                    entry = existing_entry
                    entry["memory_ids"] = sorted(set(entry.get("memory_ids", []) + ids))
                else:
                    entry = {
                        "turn_key": snapshot.turn.turn_key,
                        "turn_index": snapshot.turn.turn_index,
                        "event_keys": list(snapshot.turn.event_keys),
                        "processed_at": now,
                        "eligible_cleanup_at": _add_hours(now, cleanup_hours),
                        "memory_ids": sorted(set(ids)),
                    }
                    entries.append(entry)
                scope_key = (
                    snapshot.turn.source,
                    snapshot.turn.session_id,
                    snapshot.turn.turn_key,
                )
                deferred_values = list((deferred_candidates or {}).get(scope_key, []))
                deferred_values = [
                    dict(item)
                    for item in deferred_values
                    if isinstance(item, Mapping)
                ]
                evidence_rows = self.audit._evidence_by_turn.get(scope_key, [])
                unresolved = [dict(row) for row in evidence_rows if row.get("decision") == "DEFERRED"]
                if unresolved:
                    entry["deferred_evidence"] = unresolved
                else:
                    entry.pop("deferred_evidence", None)
                if deferred_values or unresolved:
                    # Keep the complete source turn available for a later
                    # explicit-scope retry.  A missing cleanup timestamp is
                    # intentional: deleting the inbox would discard the
                    # unresolved candidate before it can be retried.
                    if deferred_values:
                        entry["deferred_candidates"] = deferred_values
                    else:
                        entry.pop("deferred_candidates", None)
                    entry["eligible_cleanup_at"] = None
                    entry.pop("cleanup_done_at", None)
                else:
                    entry.pop("deferred_candidates", None)
                    if not entry.get("cleanup_done_at"):
                        entry["eligible_cleanup_at"] = _add_hours(now, cleanup_hours)
                operations = processed.get("pending_operations", {})
                for operation_id, operation in list(operations.items()):
                    if not isinstance(operation, Mapping) or (
                        operation.get("source"), operation.get("session_id"), operation.get("turn_key")
                    ) != scope_key:
                        continue
                    # Already under the Vault lock: do not re-enter the public
                    # reader (its file lock is intentionally non-reentrant).
                    active = None
                    try:
                        active_path = self.service.vault.memory_path(operation.get("memory_id"), "knowledge")
                        if active_path.is_file() and not active_path.is_symlink():
                            active = Memory.from_markdown(active_path.read_text(encoding="utf-8"), active_path)
                    except (OSError, UnicodeError, ValueError, TypeError):
                        pass
                    applied = (active is not None
                               and MemoryWriter._request_already_applied({"turn": snapshot.turn}, active)
                               and (content_digest(active.to_dict()) == operation.get("digest")
                                    or operation_id in previous_operation_ids))
                    if operation.get("kind") == "scope_retirement":
                        applied = self.writer.retirement_applied(operation["scope_correction"], self.writer._active_records())
                    if applied:
                        self.audit._record_disposition(scope_key, operation, operation["disposition"],
                                                 memory_id=operation["memory_id"])
                        for recorded in self.audit._dispositions_by_turn.get(scope_key, []):
                            if recorded.get("candidate_id") == operation.get("candidate_id"):
                                recorded["operation_id"] = operation_id
                                recorded["replayed"] = operation_id in previous_operation_ids
                                recorded["already_applied"] = operation_id in previous_operation_ids
                        entry["memory_ids"] = sorted(set(entry.get("memory_ids", []) + [operation["memory_id"]]))
                    # Successful no-op intents are resolved too. A failed
                    # commit never reaches the final journal-clearing write.
                    operations.pop(operation_id, None)
                processed.get("pending_turn_plans", {}).pop(turn_plan_key(snapshot.turn), None)
                entry["candidate_dispositions"] = self.audit._candidate_dispositions([snapshot])
                entry["evidence_dispositions"] = self.audit._evidence_by_turn.get(scope_key, [])
                state["processed_turns"] = entries
                watermark = max(_as_int(state.get("watermark"), 0), _as_int(snapshot.turn.turn_index, 0))
                state["watermark"] = watermark
                state["processed_watermark"] = watermark
                if prepared_scopes is not None and snapshot.state_key in prepared_scopes.session_scopes:
                    current_scopes = list(prepared_scopes.session_scopes[snapshot.state_key])
                else:
                    current_scopes = list((observed_scopes or {}).get(scope_key, []))
                if current_scopes:
                    state["scopes"] = current_scopes
                sessions[snapshot.state_key] = state
            for state_key in {snapshot.state_key for snapshot in snapshots}:
                state = sessions.get(state_key)
                if isinstance(state, dict):
                    state["processing"] = {"status": _IDLE_STATUS, "last_processed_at": now}
                    sessions[state_key] = state
            self.journal._write_processed_unlocked(processed)
            return [
                memory.memory_id
                for request, memory in zip(all_requests, written)
                if request.get("memory_id") not in noop_memory_ids
            ]



    @staticmethod
    def forget_records_unlocked(service: Any, records: Iterable[Any]) -> list[str]:
        # Preflight every target before mutating anything, then keep active
        # knowledge as the retry anchor until all linked history is gone.
        unique: list[Any] = []
        seen_paths: set[Path] = set()
        for record in records:
            if record.path in seen_paths:
                continue
            seen_paths.add(record.path)
            if record.path.is_symlink():
                raise ValueError("unsafe memory path")
            unique.append(record)
        unique.sort(key=lambda record: (record.area == "knowledge", str(record.path)))

        if not unique:
            return []
        from .process_journal import ProcessJournal
        ProcessJournal(service).cancel_forgotten_unlocked({record.memory.memory_id for record in unique})
        deleted: list[str] = []
        try:
            for record in unique:
                try:
                    record.path.unlink()
                except FileNotFoundError:
                    continue
                deleted.append(record.memory.memory_id)
        except Exception:
            if deleted:
                # The Markdown files are source of truth.  Keep derived indexes
                # synchronized after a partial filesystem mutation before the
                # original deletion error is surfaced to the caller.
                try:
                    service._rebuild_index_unlocked()
                except Exception:
                    pass
            raise
        if deleted:
            service._rebuild_index_unlocked()
        return sorted(set(deleted))
