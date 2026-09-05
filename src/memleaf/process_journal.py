"""Inbox ownership, progress, cleanup and recovery selection."""
from __future__ import annotations
import errno
import json
import os
import re
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Optional
from .capture import _safe_turn_id
from .index import EVENT_V2_BLOCK, extract_event_keys, turn_key
from .inbox import InboxEvent, InboxTurn, parse_inbox
from .locking import atomic_write_json, atomic_write_text
from .turn_plan import turn_identity_key
from .redaction import redact_text
from .vault import safe_component
from .process_common import ProcessingError, _FAILED_STATUS, _LEGACY_PROCESSING_GRACE_SECONDS, _MAX_SESSION_LINEAGE_DEPTH, _PROCESSING_LEASE_SECONDS, _PROCESSING_STATUS, _Snapshot, _as_int, _failure_metadata, _now_value, _parse_time, _read_processed, _safe_scope_background, _session_key


class ProcessJournal:
    def __init__(self, service: Any):
        self.service = service

    def _write_processed_unlocked(self, processed: Mapping[str, Any]) -> None:
        atomic_write_json(self.service.vault.processed_index_path, dict(processed))


    def _cleanup_hours(self) -> int:
        try:
            config = self.service.vault.config()
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            raise ProcessingError("cannot read process.inbox_cleanup_hours") from error
        process = config.get("process") if isinstance(config, Mapping) else None
        hours = process.get("inbox_cleanup_hours") if isinstance(process, Mapping) else None
        if type(hours) is not int or hours < 0:
            raise ProcessingError("invalid process.inbox_cleanup_hours")
        return hours


    @staticmethod
    def _owner_pid_status(owner_pid: Any) -> Optional[bool]:
        """Return whether a local processing owner is alive.

        ``None`` means that the marker does not contain a usable PID or that
        the platform cannot answer the question.  In that case callers use
        the short legacy grace period rather than assuming ownership is gone.
        """

        if isinstance(owner_pid, bool) or not isinstance(owner_pid, int):
            return None
        if owner_pid <= 0:
            return False
        if os.name == "nt":
            from .process_owner import windows_pid_status
            return windows_pid_status(owner_pid)
        if os.name != "posix":
            return None
        try:
            os.kill(owner_pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # The process exists but is owned by another user.
            return True
        except OSError as error:
            if error.errno == errno.ESRCH:
                return False
            if error.errno == errno.EPERM:
                return True
            return None
        return True


    @classmethod
    def _processing_marker_live(cls, marker: Any, now: str) -> bool:
        if not isinstance(marker, Mapping) or marker.get("status") != _PROCESSING_STATUS:
            return False
        started = _parse_time(marker.get("started_at"))
        current = _parse_time(now)
        if started is None or current is None:
            return False
        owner_status = cls._owner_pid_status(marker.get("owner_pid"))
        if owner_status is False:
            # A killed MCP worker cannot keep a processing claim alive.  This
            # is what makes a timed-out provider call immediately retryable.
            return False
        lease = _PROCESSING_LEASE_SECONDS if owner_status is True else _LEGACY_PROCESSING_GRACE_SECONDS
        return (current - started).total_seconds() <= lease


    def _session_path_without_create(self, state_key: str) -> Optional[Path]:
        try:
            source, session_id = state_key.split("/", 1)
            safe_component(source, "source")
            safe_component(session_id, "session id")
        except (ValueError, AttributeError):
            return None
        path = self.service.vault.inbox_path / source / f"{session_id}.md"
        return path


    def _remove_turn_blocks(self, path: Path, entry: Mapping[str, Any]) -> bool:
        if not path.exists():
            return False
        if path.is_symlink():
            raise ProcessingError("unsafe inbox session path")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ProcessingError("cannot read inbox session") from error
        target_keys = {
            item.casefold()
            for item in entry.get("event_keys", [])
            if isinstance(item, str)
        }
        target_turn_key = entry.get("turn_key")
        target_index = entry.get("turn_index")
        removed_keys: set[str] = set()

        def replace(match: re.Match[str]) -> str:
            try:
                metadata = json.loads(match.group("meta"))
            except (TypeError, ValueError):
                return match.group(0)
            if not isinstance(metadata, Mapping):
                return match.group(0)
            key = metadata.get("event_key")
            matches = isinstance(key, str) and key.casefold() in target_keys
            matches = matches or (
                isinstance(target_turn_key, str)
                and metadata.get("turn_key") == target_turn_key
                and metadata.get("turn_index") == target_index
            )
            if matches:
                if isinstance(key, str):
                    removed_keys.add(key.casefold())
                return ""
            return match.group(0)

        updated = EVENT_V2_BLOCK.sub(replace, text)
        keys_for_legacy = removed_keys or target_keys
        for key in keys_for_legacy:
            updated = re.sub(
                rf"(?m)^<!--\s*memleaf:event-key:v1:{re.escape(key)}\s*-->[ \t]*(?:\r?\n|$)",
                "",
                updated,
            )
        if updated == text:
            return False
        if not extract_event_keys(updated):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            atomic_write_text(path, updated)
        return True


    def _cleanup_due_unlocked(self, processed: dict[str, Any], now: str, cleanup_hours: int) -> int:
        # ``cleanup_hours`` is read and validated by the caller even though
        # eligibility timestamps are written at successful commit time.  It
        # is accepted here to keep the lock-held cleanup boundary explicit.
        del cleanup_hours
        now_time = _parse_time(now)
        if now_time is None:
            return 0
        sessions = processed.get("sessions")
        if not isinstance(sessions, dict):
            return 0
        due_entries: list[tuple[str, int, dict[str, Any]]] = []
        for state_key, state_value in sessions.items():
            if not isinstance(state_key, str) or not isinstance(state_value, dict):
                continue
            if self._processing_marker_live(state_value.get("processing"), now):
                continue
            entries = state_value.get("processed_turns")
            if not isinstance(entries, list):
                continue
            path = self._session_path_without_create(state_key)
            for entry_index, raw_entry in enumerate(entries):
                if not isinstance(raw_entry, dict) or raw_entry.get("cleanup_done_at"):
                    continue
                if raw_entry.get("deferred_candidates") or raw_entry.get("deferred_evidence"):
                    continue
                pending = processed.get("pending_turn_plans", {})
                state_source, _, state_session = state_key.partition("/")
                entry_key = raw_entry.get("turn_key")
                if isinstance(entry_key, str) and turn_identity_key(state_source, state_session, entry_key) in pending:
                    continue
                due = _parse_time(raw_entry.get("eligible_cleanup_at"))
                if due is None or due > now_time:
                    continue
                if path is not None:
                    self._remove_turn_blocks(path, raw_entry)
                due_entries.append((state_key, entry_index, raw_entry))
        if not due_entries:
            return 0

        updated = deepcopy(processed)
        updated_sessions = updated.setdefault("sessions", {})
        for state_key, entry_index, _ in due_entries:
            state = updated_sessions.get(state_key)
            if not isinstance(state, dict):
                raise ProcessingError("cleanup session disappeared")
            entries = state.get("processed_turns")
            if not isinstance(entries, list) or entry_index >= len(entries):
                raise ProcessingError("cleanup entry disappeared")
            entry = entries[entry_index]
            if not isinstance(entry, dict):
                raise ProcessingError("cleanup entry is invalid")
            entry["cleanup_done_at"] = now

        # Inbox blocks are intentionally removed before the ledger is marked,
        # but the ledger is only committed after the derived index succeeds.
        # If any later write fails, the entry remains eligible and a retry can
        # safely observe the already-absent target block.
        try:
            self._write_processed_unlocked(updated)
            self.service._rebuild_index_unlocked()
        except Exception:
            try:
                self._write_processed_unlocked(processed)
            except Exception:
                pass
            raise
        processed.clear()
        processed.update(updated)
        return len(due_entries)


    def _turns_by_session(self) -> dict[str, list[InboxTurn]]:
        grouped: dict[str, list[InboxTurn]] = {}
        for turn in parse_inbox(self.service.vault):
            if not isinstance(turn.source, str) or not isinstance(turn.session_id, str):
                continue
            grouped.setdefault(_session_key(turn.source, turn.session_id), []).append(turn)
        for values in grouped.values():
            values.sort(key=lambda turn: (turn.turn_index or 0, turn.turn_key or ""))
        return grouped


    def _snapshot(
        self,
        *,
        source: str | None,
        session_id: str | None,
        now: str,
        cleanup_hours: int,
        scope: Any = None,
    ) -> tuple[list[_Snapshot], int]:
        with self.service.vault.lock():
            self.service._recover_compaction_unlocked()
            processed = _read_processed(self.service.vault.processed_index_path)
            cleaned = self._cleanup_due_unlocked(processed, now, cleanup_hours)
            grouped = self._turns_by_session()
            sessions = processed.setdefault("sessions", {})
            snapshots: list[_Snapshot] = []
            for state_key, turns in grouped.items():
                if source is not None and not state_key.startswith(f"{source}/"):
                    continue
                if session_id is not None and state_key != _session_key(source or state_key.split("/", 1)[0], session_id):
                    continue
                state = sessions.get(state_key)
                if not isinstance(state, dict):
                    state = {}
                processing = state.get("processing")
                if self._processing_marker_live(processing, now):
                    continue
                processed_entries = state.get("processed_turns", [])
                if not isinstance(processed_entries, list):
                    processed_entries = []
                processed_keys = {
                    entry.get("turn_key")
                    for entry in processed_entries
                    if isinstance(entry, Mapping) and isinstance(entry.get("turn_key"), str)
                }
                processed_indices = {
                    _as_int(entry.get("turn_index"), -1)
                    for entry in processed_entries
                    if isinstance(entry, Mapping)
                }
                watermark = max(
                    _as_int(state.get("watermark"), 0),
                    _as_int(state.get("processed_watermark"), 0),
                    *(index for index in processed_indices if index > 0),
                )
                by_index = {
                    turn.turn_index: turn
                    for turn in turns
                    if isinstance(turn.turn_index, int) and turn.turn_index > 0
                }
                next_index = watermark + 1
                selected: list[InboxTurn] = []
                # A temporary model coverage omission gets one natural retry.
                # Missing original evidence and ambiguous ownership wait for
                # new input or an explicit scope; no timer or busy retry loop.
                explicit_retry = scope is not None and scope not in ("", [])
                deferred = [entry for entry in processed_entries
                    if isinstance(entry, dict)
                    and (entry.get("deferred_candidates") or entry.get("deferred_evidence"))]
                deferred.sort(key=lambda entry: _as_int(entry.get("turn_index"), 0))
                automatic_retries = 0
                for entry in deferred:
                    can_retry = self.retryable_deferred(entry)
                    if not explicit_retry and (not can_retry or automatic_retries >= 4):
                        continue
                    turn = next((item for item in turns
                        if item.turn_key == entry.get("turn_key") and item.complete), None)
                    if turn is not None:
                        selected.append(turn)
                        if not explicit_retry:
                            entry["automatic_retry_count"] = _as_int(entry.get("automatic_retry_count"), 0) + 1
                            automatic_retries += 1
                while next_index in by_index:
                    turn = by_index[next_index]
                    if not turn.complete:
                        break
                    if turn.turn_key in processed_keys or next_index in processed_indices:
                        next_index += 1
                        continue
                    selected.append(turn)
                    next_index += 1
                if not selected:
                    continue
                token = uuid.uuid4().hex
                state["processing"] = {
                    "status": _PROCESSING_STATUS,
                    "token": token,
                    "owner_pid": os.getpid(),
                    "turn_keys": [turn.turn_key for turn in selected],
                    "turn_indices": [turn.turn_index for turn in selected],
                    "started_at": now,
                }
                sessions[state_key] = state
                snapshots.extend(_Snapshot(turn, token, state_key) for turn in selected)
            if snapshots:
                self._write_processed_unlocked(processed)
            return snapshots, cleaned


    def _mark_failed(self, snapshots: list[_Snapshot], error: BaseException) -> None:
        try:
            now = _now_value(getattr(self.service, "clock", None))
            failure_code, failure_stage, validation_reason, validation_detail, attempt_count = _failure_metadata(error)
            with self.service.vault.lock():
                processed = _read_processed(self.service.vault.processed_index_path)
                sessions = processed.setdefault("sessions", {})
                for snapshot in snapshots:
                    state = sessions.get(snapshot.state_key)
                    if not isinstance(state, dict):
                        continue
                    marker = state.get("processing")
                    if not isinstance(marker, Mapping) or marker.get("token") != snapshot.token:
                        continue
                    failed_marker = {
                        "status": _FAILED_STATUS,
                        "token": snapshot.token,
                        "turn_keys": list(marker.get("turn_keys", [])),
                        "turn_indices": list(marker.get("turn_indices", [])),
                        "failed_at": now,
                    }
                    failed_marker["failure_code"] = failure_code
                    if failure_stage is not None:
                        failed_marker["failure_stage"] = failure_stage
                    if validation_reason is not None:
                        failed_marker["validation_reason"] = validation_reason
                    if validation_detail is not None:
                        failed_marker["validation_detail"] = validation_detail
                    if attempt_count is not None:
                        failed_marker["attempt_count"] = attempt_count
                    state["processing"] = failed_marker
                self._write_processed_unlocked(processed)
        except Exception:
            # Preserve the original safe error; a future call can recover an
            # orphaned processing marker by replacing it.
            return


    def _state_for_snapshot_unlocked(self, snapshot: _Snapshot, processed: Mapping[str, Any]) -> Mapping[str, Any]:
        sessions = processed.get("sessions", {})
        state = sessions.get(snapshot.state_key) if isinstance(sessions, Mapping) else None
        if not isinstance(state, Mapping):
            return {}

        # Compression rotates the physical session before the next visible
        # turn.  Resolve only a missing child scope through the persisted
        # parent chain; an explicit child scope remains authoritative.
        child_scope = _safe_scope_background(state)
        if child_scope:
            return state
        current = state
        visited = {snapshot.state_key}
        for _ in range(_MAX_SESSION_LINEAGE_DEPTH):
            parent_session_id = current.get("lineage_parent_session_id")
            if not isinstance(parent_session_id, str) or not parent_session_id:
                break
            try:
                parent_session_id = safe_component(parent_session_id, "parent session id")
            except ValueError:
                break
            parent_key = _session_key(snapshot.turn.source, parent_session_id)
            if parent_key in visited:
                break
            visited.add(parent_key)
            parent_state = sessions.get(parent_key) if isinstance(sessions, Mapping) else None
            if not isinstance(parent_state, Mapping):
                break
            parent_scope = _safe_scope_background(parent_state)
            if parent_scope:
                inherited = dict(state)
                inherited["scopes"] = list(parent_scope) if isinstance(parent_scope, list) else parent_scope
                return inherited
            current = parent_state
        return state


    @staticmethod
    def _processed_memory_ids(processed: Mapping[str, Any], event_key_value: str) -> Optional[list[str]]:
        sessions = processed.get("sessions")
        if not isinstance(sessions, Mapping):
            return None
        for state in sessions.values():
            if not isinstance(state, Mapping):
                continue
            entries = state.get("processed_turns")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                keys = entry.get("event_keys")
                if isinstance(keys, list) and any(
                    isinstance(key, str) and key.casefold() == event_key_value.casefold()
                    for key in keys
                ):
                    ids = entry.get("memory_ids", [])
                    return [item for item in ids if isinstance(item, str)] if isinstance(ids, list) else []
        return None


    def _deferred_counts(
        self,
        *,
        source: str | None = None,
        session_id: str | None = None,
    ) -> tuple[int, int]:
        """Return unresolved scope candidates in the requested session scope."""

        candidates = 0
        turns = 0
        with self.service.vault.lock():
            processed = _read_processed(self.service.vault.processed_index_path)
        sessions = processed.get("sessions")
        if not isinstance(sessions, Mapping):
            return 0, 0
        for state_key, state in sessions.items():
            if not isinstance(state_key, str):
                continue
            if source is not None and not state_key.startswith(f"{source}/"):
                continue
            if session_id is not None:
                state_source, separator, state_session = state_key.partition("/")
                if not separator or state_session != session_id or (
                    source is not None and state_source != source
                ):
                    continue
            if not isinstance(state, Mapping):
                continue
            entries = state.get("processed_turns")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                deferred = entry.get("deferred_candidates")
                if isinstance(deferred, list) and deferred:
                    turns += 1
                    candidates += sum(1 for item in deferred if isinstance(item, Mapping))
        return candidates, turns


    @staticmethod
    def retryable_deferred(entry: Mapping[str, Any]) -> bool:
        if _as_int(entry.get("automatic_retry_count"), 0) >= 1:
            return False
        reasons = {row.get("reason") for field in ("deferred_candidates", "deferred_evidence")
                   for row in entry.get(field, []) if isinstance(row, Mapping)}
        return "coverage_unresolved" in reasons

    def _coverage_result(self, source: str | None, session_id: str | None) -> dict[str, Any]:
        with self.service.vault.lock():
            processed = _read_processed(self.service.vault.processed_index_path)
        unresolved = 0
        retryable = 0
        partial = False
        for key, state in processed.get("sessions", {}).items():
            if source is not None and not key.startswith(source + "/"):
                continue
            if session_id is not None and key.partition("/")[2] != session_id:
                continue
            for entry in state.get("processed_turns", []):
                if isinstance(entry, Mapping) and self.retryable_deferred(entry):
                    retryable += 1
                unresolved += len(entry.get("deferred_evidence", []))
                partial = partial or bool(entry.get("deferred_candidates") or entry.get("deferred_evidence"))
        return {"execution_status": "ok", "coverage_status": "partial" if partial else "complete",
                "unresolved_evidence_count": unresolved, "retryable_deferred_turns": retryable}


    def _remember_turn(
        self,
        *,
        content: str,
        source: str,
        session_id: str,
        turn_id: str,
        event_key_value: str,
        scopes: Any,
        now: str,
        cleanup_hours: int,
    ) -> tuple[Optional[_Snapshot], Optional[Mapping[str, Any]], Optional[InboxTurn], int]:
        with self.service.vault.lock():
            self.service._recover_compaction_unlocked()
            processed = _read_processed(self.service.vault.processed_index_path)
            cleaned = self._cleanup_due_unlocked(processed, now, cleanup_hours)
            existing = self._processed_memory_ids(processed, event_key_value)
            if existing is not None:
                return None, {"memory_ids": existing}, None, cleaned
            sessions = processed.setdefault("sessions", {})
            state_key = _session_key(source, session_id)
            state = sessions.get(state_key)
            if not isinstance(state, dict):
                state = {}
            if self._processing_marker_live(state.get("processing"), now):
                raise ProcessingError("session is already being processed")
            turns = state.get("turns")
            if not isinstance(turns, dict):
                turns = {}
            stable_key = turn_key(turn_id)
            index = _as_int(turns.get(stable_key), 0)
            if index <= 0:
                index = max(_as_int(state.get("next_turn_index"), 1), 1)
                turns[stable_key] = index
                state["next_turn_index"] = index + 1
            state["turns"] = turns
            token = uuid.uuid4().hex
            state["processing"] = {
                "status": _PROCESSING_STATUS,
                "token": token,
                "owner_pid": os.getpid(),
                "turn_keys": [stable_key],
                "turn_indices": [index],
                "started_at": now,
            }
            sessions[state_key] = state
            processed["sessions"] = sessions
            self._write_processed_unlocked(processed)
        event = InboxEvent(
            source=source,
            session_id=session_id,
            turn_key=stable_key,
            turn_index=index,
            role="user",
            event_key=event_key_value,
            content=redact_text(content),
            turn_id=_safe_turn_id(turn_id),
            timestamp=now,
        )
        turn = InboxTurn(source, session_id, stable_key, index, (event,))
        candidate = {
            "candidate_id": f"remember-{event_key_value[:16]}",
            "memory": event.content,
            "evidence_event_ids": [event_key_value],
            "duplicate": False,
            "worth": True,
            "type": "other",
            "scopes": scopes if scopes is not None else ["global"],
            "scope_source": "user",
        }
        return _Snapshot(turn, token, state_key), candidate, turn, cleaned



    def cancel_forgotten_unlocked(self, memory_ids: set[str]) -> None:
        """Cancel only old operations referencing explicitly forgotten targets."""
        from .turn_plan import cancel_frozen_targets, turn_identity_key
        processed = _read_processed(self.service.vault.processed_index_path)
        changed = False
        revoked = set()
        plans = processed.get("pending_turn_plans", {})
        for key, stored in list(plans.items()):
            replacement, removed = cancel_frozen_targets(stored, memory_ids)
            if removed:
                plans[key] = replacement
                revoked.add(key)
                changed = True
        operations = processed.get("pending_operations", {})
        for operation_id, operation in list(operations.items()):
            correction = operation.get("scope_correction") or {}
            refs = {operation.get("memory_id"), correction.get("target_memory_id"),
                    correction.get("survivor_memory_id")}
            if refs.intersection(memory_ids):
                revoked.add(turn_identity_key(operation["source"], operation["session_id"], operation["turn_key"]))
                del operations[operation_id]
                changed = True
        for session_key, state in processed.get("sessions", {}).items():
            source, _, session_id = session_key.partition("/")
            marker = state.get("processing", {})
            if any(turn_identity_key(source, session_id, key) in revoked
                   for key in marker.get("turn_keys", [])):
                state["processing"] = {"status": "idle", "reason": "explicit_forget_cancelled_plan"}
                changed = True
            for entry in state.get("processed_turns", []):
                ids = entry.get("memory_ids", [])
                if memory_ids.intersection(ids):
                    entry["memory_ids"] = [mid for mid in ids if mid not in memory_ids]
                    changed = True
        if changed:
            self._write_processed_unlocked(processed)
