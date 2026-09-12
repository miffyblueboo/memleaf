"""Public processing orchestration; one planner and one commit boundary."""
from __future__ import annotations
import hashlib
from typing import Any, Mapping
from .admission import analyze_turn_evidence, memory_writes_disabled
from .evidence_policy import retain_tool_evidence
from .index import event_key
from .memory_writer import MemoryWriter
from .turn_plan import FrozenTurn, turn_plan_key
from .scope_state import ScopeError, normalize_scopes
from .vault import safe_component
from .process_common import ProcessingError, _event_payload, _now_value, _read_processed
from .turn_audit import TurnAudit
from .model_execution import ModelExecutor
from .process_journal import ProcessJournal
from .planning_context import PlanningContext
from .single_pass_memory_planner import SinglePassMemoryPlanner
from .memory_commit import MemoryCommitter
from .extraction_budget import ExtractionTiming, aggregate_extraction_metrics, budget_single_pass_backend
from .extraction_work_state import (
    active_background_work_id,
    complete_turn_budget,
    reserve_model_request,
)
from .llm.router import freeze_model_route, limit_model_requests


_MAX_TURNS_PER_PROCESS = 4


class Processor:
    def __init__(self, service: Any):
        self.service = service
        self._extraction_timings: list[dict[str, int]] = []
        self.writer = MemoryWriter(service)
        self.audit = TurnAudit()
        self.model = ModelExecutor(service)
        self.journal = ProcessJournal(service)
        self.inputs = PlanningContext(service, audit=self.audit, journal=self.journal)
        self.planner = SinglePassMemoryPlanner(service, audit=self.audit, model=self.model, inputs=self.inputs)
        self.committer = MemoryCommitter(service, writer=self.writer, audit=self.audit, journal=self.journal)

    def _auto_compact(self, *, model: Any = None, router: Any = None) -> dict[str, Any]:
        """Run compaction only when an explicit maintenance caller asks for it.

        Extraction no longer calls this method on its latency-critical return
        path. Keeping the helper preserves the maintenance API without
        coupling memory settlement to a second model workflow.
        """
        from .compaction import Compactor

        return Compactor(self.service).auto(model=model, router=router)

    @staticmethod
    def _critical_path_compaction_status(
        *, reason: str = "outside_extraction_critical_path"
    ) -> dict[str, str]:
        return {"status": "not_run", "reason": reason}

    def _attach_failure_metrics(self, error: BaseException) -> None:
        """Attach structural-only model telemetry for outer failure reporters."""

        try:
            setattr(error, "model_metrics", self.model.metrics())
            if self._extraction_timings:
                setattr(error, "extraction_metrics", aggregate_extraction_metrics(self._extraction_timings))
        except Exception:
            return

    def _turn_writes_disabled(self, turn: Any) -> bool:
        """Return only the deterministic user-authored no-write admission."""

        events = _event_payload(turn)
        policy_config = self.service.vault.config()
        for event in events:
            event["tool_evidence"] = retain_tool_evidence(
                event["tool_evidence"], policy_config
            )
        return memory_writes_disabled(analyze_turn_evidence(events))

    def _limit_claimed_snapshots(
        self,
        snapshots: list[Any],
        *,
        scope: Any = None,
    ) -> tuple[list[Any], int]:
        """Bound backlog drain and release claims this invocation skips."""

        if len(snapshots) <= _MAX_TURNS_PER_PROCESS:
            return snapshots, 0
        kept = list(snapshots[:_MAX_TURNS_PER_PROCESS])
        skipped = list(snapshots[_MAX_TURNS_PER_PROCESS:])
        kept_by_state: dict[str, list[Any]] = {}
        skipped_by_state: dict[str, list[Any]] = {}
        all_by_state: dict[str, list[Any]] = {}
        for snapshot in snapshots:
            all_by_state.setdefault(snapshot.state_key, []).append(snapshot)
        for snapshot in kept:
            kept_by_state.setdefault(snapshot.state_key, []).append(snapshot)
        for snapshot in skipped:
            skipped_by_state.setdefault(snapshot.state_key, []).append(snapshot)

        explicit_retry = scope is not None and scope not in ("", [])
        with self.service.vault.lock():
            processed = _read_processed(self.service.vault.processed_state_path)
            sessions = processed.setdefault("sessions", {})
            if not isinstance(sessions, dict):
                raise ProcessingError("processed sessions are invalid")
            for state_key, claimed in all_by_state.items():
                state = sessions.get(state_key)
                if not isinstance(state, dict):
                    raise ProcessingError("processing session disappeared")
                marker = state.get("processing")
                token = claimed[0].token
                if not isinstance(marker, Mapping) or marker.get("token") != token:
                    raise ProcessingError("processing ownership changed")
                if any(snapshot.token != token for snapshot in claimed):
                    raise ProcessingError("processing ownership changed")

                retained = kept_by_state.get(state_key, [])
                if retained:
                    narrowed = dict(marker)
                    narrowed["turn_keys"] = [snapshot.turn.turn_key for snapshot in retained]
                    narrowed["turn_indices"] = [snapshot.turn.turn_index for snapshot in retained]
                    state["processing"] = narrowed
                else:
                    state["processing"] = {
                        "status": "idle",
                        "reason": "backlog_limit_release",
                    }

                if not explicit_retry:
                    skipped_keys = {
                        snapshot.turn.turn_key
                        for snapshot in skipped_by_state.get(state_key, [])
                        if isinstance(snapshot.turn.turn_key, str)
                    }
                    entries = state.get("processed_turns")
                    if skipped_keys and isinstance(entries, list):
                        for entry in entries:
                            if (
                                not isinstance(entry, dict)
                                or entry.get("turn_key") not in skipped_keys
                                or not (
                                    entry.get("deferred_candidates")
                                    or entry.get("deferred_evidence")
                                )
                            ):
                                continue
                            count = entry.get("automatic_retry_count")
                            if type(count) is int and count > 0:
                                if count == 1:
                                    entry.pop("automatic_retry_count", None)
                                else:
                                    entry["automatic_retry_count"] = count - 1
                sessions[state_key] = state
            self.journal._write_processed_unlocked(processed)
        return kept, len(skipped)

    @staticmethod
    def _scope_snapshot(state: Mapping[str, Any]) -> tuple[bool, Any]:
        if "scopes" not in state:
            return False, None
        value = state.get("scopes")
        if isinstance(value, list):
            return True, list(value)
        if isinstance(value, tuple):
            return True, list(value)
        return True, value

    @staticmethod
    def _restore_scope_snapshot(
        state: Mapping[str, Any],
        snapshot: tuple[bool, Any],
    ) -> dict[str, Any]:
        result = dict(state)
        present, value = snapshot
        if present:
            result["scopes"] = list(value) if isinstance(value, list) else value
        else:
            result.pop("scopes", None)
        return result

    @staticmethod
    def _turn_budget_id(snapshot: Any) -> str:
        turn = snapshot.turn
        return f"{turn.source}/{turn.session_id}/{turn.turn_key}"

    def process(
        self,
        *,
        source: str | None = None,
        session_id: str | None = None,
        model: Any = None,
        router: Any = None,
        scope: Any = None,
    ) -> dict[str, Any]:
        self._extraction_timings = []
        if source is not None:
            source = safe_component(source, "source")
        if session_id is not None:
            session_id = safe_component(session_id, "session id")
        background_work_id = active_background_work_id(
            self.service.vault,
            source=source,
            session_id=session_id,
        )
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
                **self.journal._coverage_result(source, session_id, turns=()),
                "processed_turns": 0,
                "memories_written": 0,
                "memory_ids": [],
                "metadata_merged": 0,
                "cleaned_turns": cleaned,
                "deferred_candidates": deferred_candidates,
                "deferred_inbox_turns": deferred_turns,
                "pending_inbox_turns": 0,
                "extraction_metrics": aggregate_extraction_metrics(()),
                "model_metrics": self.model.metrics(),
                "compaction": self._critical_path_compaction_status(),
            }

        try:
            snapshots, pending_inbox_turns = self._limit_claimed_snapshots(
                snapshots, scope=scope
            )
        except Exception as error:
            self._attach_failure_metrics(error)
            self.journal._mark_failed(snapshots, error)
            raise

        scope_baseline: dict[str, tuple[bool, Any]] = {}
        with self.service.vault.lock():
            initial_processed = _read_processed(self.service.vault.processed_state_path)
            for snapshot in snapshots:
                if snapshot.state_key in scope_baseline:
                    continue
                initial_state = self.journal._state_for_snapshot_unlocked(
                    snapshot, initial_processed
                )
                scope_baseline[snapshot.state_key] = self._scope_snapshot(initial_state)

        backend = None
        all_ids: list[str] = []
        metadata_merged = 0
        self.audit._planned_related = []
        self.audit._deferred_by_turn = {}
        self.audit._dispositions_by_turn = {}
        self.audit._evidence_by_turn = {}
        self.audit._planned_settled_sources = set()
        current_index = 0
        turn_timing: ExtractionTiming | None = None
        try:
            for current_index, snapshot in enumerate(snapshots):
                self.audit._planned_related = []
                self.audit._planned_settled_sources = set()
                durable_turn_budget_id = self._turn_budget_id(snapshot)
                turn_timing = ExtractionTiming()

                with self.service.vault.lock():
                    processed = _read_processed(self.service.vault.processed_state_path)
                    state = self.journal._state_for_snapshot_unlocked(snapshot, processed)
                state = self._restore_scope_snapshot(
                    state,
                    scope_baseline.get(snapshot.state_key, (False, None)),
                )
                stored_plan = processed.get("pending_turn_plans", {}).get(turn_plan_key(snapshot.turn))
                if stored_plan is not None:
                    restored = FrozenTurn.restore(stored_plan, snapshot.turn)
                    ref = (snapshot.turn.source, snapshot.turn.session_id, snapshot.turn.turn_key)
                    turn_requests, turn_scopes = restored["requests"], restored["scopes"]
                    self.audit._dispositions_by_turn[ref] = restored["candidate_dispositions"]
                    self.audit._evidence_by_turn[ref] = restored["evidence_dispositions"]
                    self.audit._deferred_by_turn[ref] = restored["deferred_candidates"]
                elif self._turn_writes_disabled(snapshot.turn):
                    turn_requests, turn_scopes = [], []
                else:
                    if backend is None:
                        backend = self.model._resolve_backend(model=model, router=router)
                    turn_backend = backend
                    if getattr(backend, "single_pass_safe", False) is True:
                        reserve_request = None
                        if background_work_id is not None:
                            reserve_request = lambda work_id=background_work_id, turn_id=durable_turn_budget_id: reserve_model_request(
                                self.service.vault,
                                work_id=work_id,
                                turn_id=turn_id,
                            )
                        turn_backend = budget_single_pass_backend(
                            backend,
                            reserve_request=reserve_request,
                        )
                    turn_requests, turn_scopes = self.planner._collect_turn_outputs(
                        turn_backend, snapshot.turn, state, scope=scope
                    )

                turn_timing.begin_commit()
                ref = (snapshot.turn.source, snapshot.turn.session_id, snapshot.turn.turn_key)
                ids = self.committer._commit_success(
                    [snapshot],
                    turn_requests,
                    now=_now_value(getattr(self.service, "clock", None)),
                    cleanup_hours=cleanup_hours,
                    observed_scopes={ref: turn_scopes},
                    deferred_candidates={
                        ref: self.audit._deferred_by_turn.get(ref, [])
                    },
                )
                if background_work_id is not None:
                    complete_turn_budget(
                        self.service.vault,
                        work_id=background_work_id,
                        turn_id=durable_turn_budget_id,
                    )
                all_ids.extend(ids)
                metadata_merged += self.writer.last_metadata_merged
                self._extraction_timings.append(turn_timing.finish())
                turn_timing = None
        except Exception as error:
            if turn_timing is not None:
                self._extraction_timings.append(turn_timing.finish(failed=True))
            self._attach_failure_metrics(error)
            self.journal._mark_failed(snapshots[current_index:], error)
            raise

        no_memory_changes = not all_ids and metadata_merged == 0
        compaction = self._critical_path_compaction_status(
            reason="no_memory_changes" if no_memory_changes else "outside_extraction_critical_path"
        )
        deferred_candidates, deferred_turns = self.journal._deferred_counts(
            source=source,
            session_id=session_id,
        )
        return {
            **self.journal._coverage_result(
                source,
                session_id,
                turns=[snapshot.turn for snapshot in snapshots],
            ),
            "processed_turns": len(snapshots),
            "memories_written": len(all_ids),
            "memory_ids": all_ids,
            "metadata_merged": metadata_merged,
            "cleaned_turns": cleaned,
            "deferred_candidates": deferred_candidates,
            "deferred_inbox_turns": deferred_turns,
            "pending_inbox_turns": pending_inbox_turns,
            "extraction_metrics": aggregate_extraction_metrics(self._extraction_timings),
            "model_metrics": self.model.metrics(),
            "compaction": compaction,
        }

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
                "pending_inbox_turns": 0,
                "model_metrics": self.model.metrics(),
                "compaction": self._critical_path_compaction_status(),
            }
        backend = None
        self.audit._planned_related = []
        self.audit._deferred_by_turn = {}
        try:
            with self.service.vault.lock():
                processed = _read_processed(self.service.vault.processed_state_path)
                state = self.journal._state_for_snapshot_unlocked(snapshot, processed)
            stored = processed.get("pending_turn_plans", {}).get(turn_plan_key(turn))
            if stored is not None:
                restored = FrozenTurn.restore(stored, turn)
                requests, turn_scopes = restored["requests"], restored["scopes"]
            else:
                backend = self.model._resolve_backend(model=model, router=router)
                # Explicit remember has the same global request discipline as
                # automatic extraction: no hidden host->API fallback and no
                # more than two actual model dispatches (primary + one repair).
                backend = limit_model_requests(freeze_model_route(backend), 2)
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
                "pending_inbox_turns": 0,
                "model_metrics": self.model.metrics(),
                "compaction": self._critical_path_compaction_status(),
            }
        except Exception as error:
            self._attach_failure_metrics(error)
            self.journal._mark_failed([snapshot], error)
            raise


__all__ = ["Processor", "ProcessingError"]
