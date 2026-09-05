"""Public processing orchestration; one planner and one commit boundary."""
from __future__ import annotations
import hashlib
from typing import Any, Mapping
from .index import event_key
from .memory_writer import MemoryWriter
from .turn_plan import FrozenTurn, turn_plan_key
from .scope_state import ScopeError, normalize_scopes
from .vault import safe_component
from .process_common import ProcessingError, _now_value, _read_processed
from .turn_audit import TurnAudit
from .model_execution import ModelExecutor
from .process_journal import ProcessJournal
from .planning_context import PlanningContext
from .memory_planner import MemoryPlanner
from .memory_commit import MemoryCommitter


class Processor:
    def __init__(self, service: Any):
        self.service = service
        self.writer = MemoryWriter(service)
        self.audit = TurnAudit()
        self.model = ModelExecutor(service)
        self.journal = ProcessJournal(service)
        self.inputs = PlanningContext(service, audit=self.audit, journal=self.journal)
        self.planner = MemoryPlanner(service, audit=self.audit, model=self.model, inputs=self.inputs)
        self.committer = MemoryCommitter(service, writer=self.writer, audit=self.audit, journal=self.journal)

    def _auto_compact(self, *, model: Any = None, router: Any = None) -> dict[str, Any]:
        from .compaction import Compactor

        return Compactor(self.service).auto(model=model, router=router)


    def process(
        self,
        *,
        source: str | None = None,
        session_id: str | None = None,
        model: Any = None,
        router: Any = None,
        scope: Any = None,
    ) -> dict[str, Any]:
        if source is not None:
            source = safe_component(source, "source")
        if session_id is not None:
            session_id = safe_component(session_id, "session id")
        now = _now_value(getattr(self.service, "clock", None))
        cleanup_hours = self.journal._cleanup_hours()
        snapshots, cleaned = self.journal._snapshot(
            source=source,
            session_id=session_id,
            now=now,
            cleanup_hours=cleanup_hours,
            scope=scope,
        )
        if not snapshots:
            deferred_candidates, deferred_turns = self.journal._deferred_counts(
                source=source,
                session_id=session_id,
            )
            return {
                **self.journal._coverage_result(source, session_id),
                "processed_turns": 0,
                "memories_written": 0,
                "memory_ids": [],
                "metadata_merged": 0,
                "cleaned_turns": cleaned,
                "deferred_candidates": deferred_candidates,
                "deferred_inbox_turns": deferred_turns,
                "compaction": self._auto_compact(model=model, router=router),
            }
        backend = None
        requests: list[dict[str, Any]] = []
        observed_scopes: dict[tuple[str, str, str], list[str]] = {}
        self.audit._planned_related = []
        self.audit._deferred_by_turn = {}
        self.audit._dispositions_by_turn = {}
        self.audit._evidence_by_turn = {}
        try:
            for snapshot in snapshots:
                with self.service.vault.lock():
                    processed = _read_processed(self.service.vault.processed_index_path)
                    state = self.journal._state_for_snapshot_unlocked(snapshot, processed)
                stored_plan = processed.get("pending_turn_plans", {}).get(turn_plan_key(snapshot.turn))
                if stored_plan is not None:
                    restored = FrozenTurn.restore(stored_plan, snapshot.turn)
                    ref = (snapshot.turn.source, snapshot.turn.session_id, snapshot.turn.turn_key)
                    turn_requests, turn_scopes = restored["requests"], restored["scopes"]
                    self.audit._dispositions_by_turn[ref] = restored["candidate_dispositions"]
                    self.audit._evidence_by_turn[ref] = restored["evidence_dispositions"]
                    self.audit._deferred_by_turn[ref] = restored["deferred_candidates"]
                else:
                    if backend is None:
                        backend = self.model._resolve_backend(model=model, router=router)
                    turn_requests, turn_scopes = self.planner._collect_turn_outputs(
                        backend, snapshot.turn, state, scope=scope
                    )
                requests.extend(turn_requests)
                for request in turn_requests:
                    planned = self.planner._planned_memory(request)
                    if planned is None:
                        continue
                    planned_id = planned.get("memory_id")
                    if isinstance(planned_id, str):
                        self.audit._planned_related = [
                            item
                            for item in self.audit._planned_related
                            if item.get("memory_id", "").casefold() != planned_id.casefold()
                        ]
                    self.audit._planned_related.append(planned)
                observed_scopes[(snapshot.turn.source, snapshot.turn.session_id, snapshot.turn.turn_key)] = turn_scopes
            ids = self.committer._commit_success(
                snapshots,
                requests,
                now=_now_value(getattr(self.service, "clock", None)),
                cleanup_hours=cleanup_hours,
                observed_scopes=observed_scopes,
                deferred_candidates=self.audit._deferred_by_turn,
            )
            # A processed read-only/no-op turn must not trigger maintenance
            # writes. Explicit maintenance and no-pending-turn processing keep
            # their existing separate authorization.
            compaction = (self._auto_compact(model=backend) if ids or self.writer.last_metadata_merged
                          else {"status": "not_due", "reason": "no_memory_changes"})
            deferred_candidates, deferred_turns = self.journal._deferred_counts(
                source=source,
                session_id=session_id,
            )
            return {
                **self.journal._coverage_result(source, session_id),
                "processed_turns": len(snapshots),
                "memories_written": len(ids),
                "memory_ids": ids,
                "metadata_merged": self.writer.last_metadata_merged,
                "cleaned_turns": cleaned,
                "deferred_candidates": deferred_candidates,
                "deferred_inbox_turns": deferred_turns,
                "compaction": compaction,
            }
        except Exception as error:
            self.journal._mark_failed(snapshots, error)
            raise


    def remember(
        self,
        content: str | None = None,
        *,
        text: str | None = None,
        source: str = "memleaf",
        session_id: str = "remember",
        turn_id: str | None = None,
        event_id: str | None = None,
        scopes: Any = None,
        model: Any = None,
        router: Any = None,
    ) -> dict[str, Any]:
        value = content if content is not None else text
        if not isinstance(value, str) or not value.strip():
            raise ValueError("remember content is required")
        source = safe_component(source, "source")
        session_id = safe_component(session_id, "session id")
        normalized_scopes = None
        if scopes is not None:
            try:
                normalized_scopes = normalize_scopes(scopes, field="remember scopes")
            except ScopeError as error:
                raise ValueError("invalid remember scopes") from error
        raw_turn_id = turn_id or f"remember-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"
        if not isinstance(raw_turn_id, str) or not raw_turn_id or "\x00" in raw_turn_id or "\n" in raw_turn_id or "\r" in raw_turn_id:
            raise ValueError("invalid turn id")
        raw_event_id = event_id or f"remember/{source}/{session_id}/{raw_turn_id}"
        if not isinstance(raw_event_id, str) or not raw_event_id or "\x00" in raw_event_id or "\n" in raw_event_id or "\r" in raw_event_id:
            raise ValueError("invalid event id")
        stable_event_key = event_key(raw_event_id)
        now = _now_value(getattr(self.service, "clock", None))
        cleanup_hours = self.journal._cleanup_hours()
        snapshot, candidate, turn, cleaned = self.journal._remember_turn(
            content=value,
            source=source,
            session_id=session_id,
            turn_id=raw_turn_id,
            event_key_value=stable_event_key,
            scopes=normalized_scopes,
            now=now,
            cleanup_hours=cleanup_hours,
        )
        if snapshot is None:
            ids = list(candidate.get("memory_ids", [])) if isinstance(candidate, Mapping) else []
            return {
                "processed_turns": 0,
                "memories_written": 0,
                "memory_ids": ids,
                "metadata_merged": 0,
                "cleaned_turns": cleaned,
                "deferred_candidates": 0,
                "deferred_inbox_turns": 0,
                "compaction": self._auto_compact(model=model, router=router),
            }
        backend = None
        self.audit._planned_related = []
        self.audit._deferred_by_turn = {}
        try:
            with self.service.vault.lock():
                processed = _read_processed(self.service.vault.processed_index_path)
                state = self.journal._state_for_snapshot_unlocked(snapshot, processed)
            stored = processed.get("pending_turn_plans", {}).get(turn_plan_key(turn))
            if stored is not None:
                restored = FrozenTurn.restore(stored, turn)
                requests, turn_scopes = restored["requests"], restored["scopes"]
            else:
                backend = self.model._resolve_backend(model=model, router=router)
                requests, turn_scopes = self.planner._collect_turn_outputs(
                    backend, turn, state, explicit=True,
                    explicit_candidate=candidate, scope=normalized_scopes,
                )
            if normalized_scopes is not None:
                turn_scopes = list(normalized_scopes)
            ids = self.committer._commit_success(
                [snapshot],
                requests,
                now=_now_value(getattr(self.service, "clock", None)),
                cleanup_hours=cleanup_hours,
                observed_scopes={
                    (snapshot.turn.source, snapshot.turn.session_id, snapshot.turn.turn_key): turn_scopes
                },
            )
            return {
                "processed_turns": 1,
                "memories_written": len(ids),
                "memory_ids": ids,
                "metadata_merged": self.writer.last_metadata_merged,
                "cleaned_turns": cleaned,
                "deferred_candidates": 0,
                "deferred_inbox_turns": 0,
                "compaction": self._auto_compact(model=backend),
            }
        except Exception as error:
            self.journal._mark_failed([snapshot], error)
            raise



__all__ = ["Processor", "ProcessingError"]
