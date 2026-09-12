"""Production-shaped automatic planner for the B3 single-pass experiment.

This class intentionally reuses the existing MemoryPlanner request identity,
retry ledger, Core validation and writer contracts while replacing the normal
automatic Gate -> reconciliation -> Summary -> semantic-review chain with one
semantic model call. Explicit remember remains delegated until its dedicated
B3 contract is validated.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Iterable, Mapping, Optional

from .admission import analyze_turn_evidence, partition_evidence_units, summary_evidence
from .evidence_policy import retain_tool_evidence
from .extraction_capability import supports_single_pass_protocol
from .memory_planner import (
    MemoryPlanner,
    _explicit_project_scope_authorizations,
    _model_project_scope_is_source_grounded,
)
from .process_common import (
    ProcessingError,
    _automatic_create_conflicts,
    _event_payload,
    _grounded_due_dates,
    _normalize_summary_dates,
    _summary_date_grounding_violations,
)
from .single_pass_plan import run_single_pass_stage
from .turn_plan import dedup_digest, revision_digest
from .validation import ModelOutputError, parse_summarize_output
from .llm import ModelUnavailable


class SinglePassMemoryPlanner(MemoryPlanner):
    """One semantic model call for an ordinary automatic turn."""

    @staticmethod
    def _fallback_scopes(scope_background: Any) -> tuple[list[str], str]:
        if isinstance(scope_background, str) and scope_background:
            return [scope_background], "session_context"
        if isinstance(scope_background, (list, tuple)):
            values = [item for item in scope_background if isinstance(item, str) and item]
            if values:
                return list(dict.fromkeys(values)), "session_context"
        return ["unscoped"], "insufficient_context"

    @staticmethod
    def _scope_keyset(value: Any) -> frozenset[str]:
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, (list, tuple, set, frozenset)):
            values = value
        else:
            values = []
        return frozenset(
            item.casefold()
            for item in values
            if isinstance(item, str) and item
        )

    @classmethod
    def _derived_scope_source(
        cls,
        scopes: Iterable[str],
        scope_background: Any,
        explicit_scope: Any,
    ) -> str:
        """Derive Scope provenance from Core-owned context, never model labels."""

        selected = cls._scope_keyset(scopes)
        if selected == frozenset({"unscoped"}):
            return "insufficient_context"
        explicit = cls._scope_keyset(explicit_scope)
        if explicit_scope is not None and selected and selected.issubset(explicit):
            return "user"
        background = cls._scope_keyset(scope_background)
        if selected and background and selected.issubset(background):
            return "session_context"
        return "model"

    @staticmethod
    def _claim_unit_ids(claims: Iterable[Mapping[str, Any]]) -> list[str]:
        return list(dict.fromkeys(
            claim["unit_id"]
            for claim in claims
            if isinstance(claim, Mapping) and isinstance(claim.get("unit_id"), str)
        ))

    @staticmethod
    def _claim_event_keys(
        claims: Iterable[Mapping[str, Any]],
        by_unit: Mapping[str, Any],
    ) -> list[str]:
        return list(dict.fromkeys(
            by_unit[claim["unit_id"]].event_key
            for claim in claims
            if isinstance(claim, Mapping)
            and isinstance(claim.get("unit_id"), str)
            and claim["unit_id"] in by_unit
        ))

    def _finalize_evidence_audit(
        self,
        *,
        turn: Any,
        evidence_units: Iterable[Any],
        retry_ledger: Mapping[str, Any],
        settled_unit_ids: set[str],
        settled_source_ids: set[str],
        planned_settled_sources: set[Any],
        covered_by_unit: Mapping[str, list[str]],
        no_memory_by_unit: Mapping[str, str],
    ) -> None:
        turn_ref = (turn.source, turn.session_id, turn.turn_key)
        evidence_dispositions: list[dict[str, Any]] = []
        for unit in evidence_units:
            source_identity = self._evidence_source_identity(
                unit, source=turn.source, session_id=turn.session_id
            )
            settled = unit.unit_id in settled_unit_ids or source_identity in settled_source_ids
            if settled:
                previous = retry_ledger["settled_rows_by_unit"].get(unit.unit_id)
                if previous is None:
                    previous = retry_ledger["settled_rows_by_source"].get(source_identity)
                if retry_ledger.get("same_turn") and isinstance(previous, Mapping):
                    row = dict(previous)
                    row["unit_id"] = unit.unit_id
                    row["event_key"] = unit.event_key
                    row["source_identity"] = source_identity
                else:
                    memory_id = retry_ledger["settled_memory_by_source"].get(source_identity)
                    row = {
                        "unit_id": unit.unit_id,
                        "event_key": unit.event_key,
                        "decision": "NO_CHANGE",
                        "reason": "already_processed",
                        "candidate_ids": [],
                        "source_identity": source_identity,
                    }
                    if isinstance(memory_id, str) and memory_id:
                        row["memory_id"] = memory_id
            elif unit.origin == "unknown":
                row = {
                    "unit_id": unit.unit_id,
                    "event_key": unit.event_key,
                    "decision": "DEFERRED",
                    "reason": "incomplete_tool_evidence",
                    "candidate_ids": [],
                    "source_identity": source_identity,
                }
            elif unit.unit_id in covered_by_unit:
                row = {
                    "unit_id": unit.unit_id,
                    "event_key": unit.event_key,
                    "decision": "CANDIDATE",
                    "reason": "candidate_checked",
                    "candidate_ids": list(dict.fromkeys(covered_by_unit[unit.unit_id])),
                    "source_identity": source_identity,
                }
            elif unit.unit_id in no_memory_by_unit:
                row = {
                    "unit_id": unit.unit_id,
                    "event_key": unit.event_key,
                    "decision": "NO_CHANGE",
                    "reason": no_memory_by_unit[unit.unit_id],
                    "candidate_ids": [],
                    "source_identity": source_identity,
                }
            elif getattr(unit, "can_support", False) is True:
                row = {
                    "unit_id": unit.unit_id,
                    "event_key": unit.event_key,
                    "decision": "DEFERRED",
                    "reason": "coverage_unresolved",
                    "candidate_ids": [],
                    "source_identity": source_identity,
                }
            else:
                row = {
                    "unit_id": unit.unit_id,
                    "event_key": unit.event_key,
                    "decision": "NO_CHANGE",
                    "reason": str(unit.origin),
                    "candidate_ids": [],
                    "source_identity": source_identity,
                }
            row["source_kind"] = (
                "external" if unit.origin == "external_observation" else "conversation"
            )
            evidence_dispositions.append(row)

        disposition_by_candidate = {
            row.get("candidate_id").casefold(): row.get("disposition")
            for row in self.audit._dispositions_by_turn.get(turn_ref, [])
            if isinstance(row, Mapping) and isinstance(row.get("candidate_id"), str)
        }
        resolved_unit_ids: set[str] = set()
        for row in evidence_dispositions:
            candidate_ids = row.get("candidate_ids", [])
            statuses = [
                disposition_by_candidate.get(candidate_id.casefold())
                for candidate_id in candidate_ids
                if isinstance(candidate_id, str)
            ]
            candidate_settled = bool(candidate_ids) and len(statuses) == len(candidate_ids) and all(
                status in {"CREATE", "UPDATE", "NO_CHANGE"} for status in statuses
            )
            if row.get("decision") == "CANDIDATE" and not candidate_settled:
                # A candidate that remains DEFERRED has not settled its source
                # evidence. Preserve that fact in the evidence ledger instead
                # of presenting semantic coverage as a terminal outcome.
                row["decision"] = "DEFERRED"
                row["reason"] = "coverage_unresolved"
                row["candidate_ids"] = []
            if (
                row.get("decision") == "CANDIDATE" and candidate_settled
            ) or (
                row.get("decision") == "NO_CHANGE"
                and row.get("reason") not in {"coverage_unresolved", "incomplete_tool_evidence"}
            ):
                unit_id = row.get("unit_id")
                if isinstance(unit_id, str):
                    resolved_unit_ids.add(unit_id)
                if row.get("source_kind") == "external" and isinstance(row.get("source_identity"), str):
                    planned_settled_sources.add((turn.source, turn.session_id, row["source_identity"]))

        deferred_rows = self.audit._deferred_by_turn.get(turn_ref, [])
        retry_resolved: set[str] = set()
        if retry_ledger.get("same_turn"):
            for previous in retry_ledger.get("current_candidates", []):
                if not isinstance(previous, Mapping) or previous.get("disposition") != "DEFERRED":
                    continue
                candidate_id = previous.get("candidate_id")
                evidence_ids = previous.get("evidence_unit_ids", [])
                if (
                    isinstance(candidate_id, str)
                    and isinstance(evidence_ids, list)
                    and evidence_ids
                    and all(isinstance(uid, str) and uid in resolved_unit_ids for uid in evidence_ids)
                ):
                    retry_resolved.add(candidate_id.casefold())
        unique_deferred: dict[str, dict[str, Any]] = {}
        for raw in deferred_rows:
            if not isinstance(raw, Mapping) or not isinstance(raw.get("candidate_id"), str):
                continue
            key = raw["candidate_id"].casefold()
            if key in retry_resolved or disposition_by_candidate.get(key) not in {None, "DEFERRED"}:
                continue
            unique_deferred.setdefault(key, dict(raw))
        self.audit._deferred_by_turn[turn_ref] = list(unique_deferred.values())
        self.audit._evidence_by_turn[turn_ref] = evidence_dispositions

    def _collect_turn_outputs(
        self,
        backend: Any,
        turn: Any,
        state: Mapping[str, Any],
        *,
        explicit: bool = False,
        explicit_candidate: Optional[Mapping[str, Any]] = None,
        scope: Any = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        # Explicit remember already has a proven one-call summarize contract.
        # Ordinary extraction uses B3 whenever the backend can speak the
        # protocol. Strict deadline/outbound-request safety is a separate
        # capability enforced by Processor only for ``single_pass_safe``.
        if explicit:
            return super()._collect_turn_outputs(
                backend,
                turn,
                state,
                explicit=explicit,
                explicit_candidate=explicit_candidate,
                scope=scope,
            )
        if not supports_single_pass_protocol(backend):
            # Automatic extraction has one explicit B3 protocol.  Capability
            # insufficiency is a routing error, never permission to hide a
            # fallback into the legacy multi-stage semantic pipeline.
            raise ModelUnavailable(
                "configured model route does not support B3 single-pass extraction",
                stage="single_pass",
            )

        authorized_project_scopes = _explicit_project_scope_authorizations(scope)
        events = _event_payload(turn)
        policy_config = self.service.vault.config()
        for event in events:
            event["tool_evidence"] = retain_tool_evidence(event["tool_evidence"], policy_config)
        evidence_units = analyze_turn_evidence(events)
        partition = partition_evidence_units(evidence_units)
        retry_ledger = self._retry_ledger(state, turn)
        settled_unit_ids = set(retry_ledger["settled_unit_ids"])
        settled_source_ids = set(retry_ledger["settled_source_ids"])
        planned_settled_sources = getattr(self.audit, "_planned_settled_sources", None)
        if not isinstance(planned_settled_sources, set):
            planned_settled_sources = set()
            self.audit._planned_settled_sources = planned_settled_sources
        settled_source_ids.update({
            value[2]
            for value in planned_settled_sources
            if isinstance(value, tuple)
            and len(value) == 3
            and value[0] == turn.source
            and value[1] == turn.session_id
            and isinstance(value[2], str)
        })
        source_identity_by_unit = {
            unit.unit_id: self._evidence_source_identity(
                unit, source=turn.source, session_id=turn.session_id
            )
            for unit in partition.physical
        }
        planning_units = tuple(
            unit
            for unit in partition.physical
            if unit.unit_id not in settled_unit_ids
            and source_identity_by_unit[unit.unit_id] not in settled_source_ids
        )
        turn_ref = (turn.source, turn.session_id, turn.turn_key)
        if retry_ledger.get("same_turn"):
            self.audit._dispositions_by_turn[turn_ref] = deepcopy(retry_ledger.get("current_candidates", []))
            self.audit._deferred_by_turn[turn_ref] = deepcopy(retry_ledger.get("current_deferred", []))
        else:
            self.audit._deferred_by_turn.setdefault(turn_ref, [])

        covered_by_unit: dict[str, list[str]] = {}
        no_memory_by_unit: dict[str, str] = {}
        observed_scopes: list[str] = []
        requests: list[dict[str, Any]] = []

        if not planning_units:
            self._finalize_evidence_audit(
                turn=turn,
                evidence_units=evidence_units,
                retry_ledger=retry_ledger,
                settled_unit_ids=settled_unit_ids,
                settled_source_ids=settled_source_ids,
                planned_settled_sources=planned_settled_sources,
                covered_by_unit=covered_by_unit,
                no_memory_by_unit=no_memory_by_unit,
            )
            return [], observed_scopes

        related, scope_background, native_refs, _scope_fallback, lookup_complete = self.inputs._single_pass_related(
            turn,
            state,
            scope,
            overlay=self.audit._planned_related,
            physical_units=planning_units,
        )
        local_related = [
            dict(item) for item in related
            if isinstance(item, Mapping) and item.get("native") is not True
        ]
        native_related = [
            dict(item) for item in related
            if isinstance(item, Mapping) and item.get("native") is True
        ]
        scope_registry = self.inputs._scope_registry_projection()
        with self.service.vault.lock():
            validation_scope_registry = self.service.vault.config().get("scopes", {})
        title = self.inputs._conversation_title(turn)
        by_unit = {unit.unit_id: unit for unit in planning_units}
        related_native_ids = [
            item["native_id"] for item in native_related
            if isinstance(item.get("native_id"), str)
        ]
        target_revisions: dict[str, str] = {}
        scope_correction_plans: dict[str, dict[str, Any]] = {}
        for item in local_related:
            memory_id = item.get("memory_id")
            active = self.inputs._active_memory_by_id(memory_id)
            if active is not None:
                target_revisions[active.memory_id.casefold()] = revision_digest(active)

        validated_candidates: dict[str, dict[str, Any]] = {}

        def validate_memory(
            candidate_id: str,
            decision: str,
            target_id: str | None,
            target_record: Mapping[str, Any] | None,
            proposed: Mapping[str, Any],
            claims: list[dict[str, Any]],
            decision_context: Mapping[str, Any],
        ) -> Mapping[str, Any]:
            unit_ids = self._claim_unit_ids(claims)
            event_keys = self._claim_event_keys(claims, by_unit)
            if not unit_ids or not event_keys:
                raise ModelOutputError("B3 memory has no admitted evidence", validation_detail="invalid_evidence")

            target_memory = self.inputs._active_memory_by_id(target_id) if target_id else None
            if decision == "UPDATE":
                if target_memory is None:
                    raise ModelOutputError("B3 update target disappeared", validation_detail="invalid_update_target")
                memory_type = target_memory.type
                if "scopes" in decision_context:
                    scopes = list(decision_context.get("scopes", []))
                    scope_source = self._derived_scope_source(scopes, scope_background, scope)
                else:
                    scopes = list(target_memory.scopes)
                    scope_source = target_memory.scope_source
            else:
                memory_type = decision_context.get("type")
                scopes = list(decision_context.get("scopes", []))
                scope_source = self._derived_scope_source(scopes, scope_background, scope)

            candidate = {
                "candidate_id": candidate_id,
                "memory": str(proposed.get("body", "")),
                "duplicate": False,
                "worth": True,
                "type": memory_type,
                "scopes": scopes,
                "scope_source": scope_source,
                "evidence_event_ids": event_keys,
                "evidence_unit_ids": unit_ids,
                "_evidence_unit_ids": unit_ids,
                "_evidence_bindings": [dict(claim) for claim in claims],
            }
            if decision == "UPDATE" and isinstance(target_id, str):
                candidate["update_memory_id"] = target_id
            if decision in {"CREATE", "UPDATE"} and not _model_project_scope_is_source_grounded(
                candidate,
                planning_units,
                validation_scope_registry,
                authorized_project_scopes,
            ):
                raise ModelOutputError(
                    "B3 project scope is not grounded by claimed source",
                    validation_detail="scope_not_grounded",
                )
            if (
                decision == "UPDATE"
                and target_memory is not None
                and [scope.casefold() for scope in scopes]
                    != [scope.casefold() for scope in target_memory.scopes]
            ):
                correction_plan = self.inputs._scope_correction_plan(
                    candidate,
                    turn,
                    self.service.vault.config(),
                )
                if (
                    not isinstance(correction_plan, Mapping)
                    or correction_plan.get("ambiguous")
                    or correction_plan.get("unresolved")
                    or not isinstance(correction_plan.get("target_memory_id"), str)
                    or correction_plan["target_memory_id"].casefold() != target_memory.memory_id.casefold()
                    or not isinstance(correction_plan.get("new_scope"), str)
                    or correction_plan["new_scope"].casefold() not in {
                        scope.casefold() for scope in scopes if isinstance(scope, str)
                    }
                ):
                    raise ModelOutputError(
                        "B3 cross-scope UPDATE is not authorized by explicit correction evidence",
                        validation_detail="scope_drift",
                    )
                scope_correction_plans[candidate_id] = dict(correction_plan)

            admitted_events = summary_evidence(candidate, planning_units, events=events)
            admitted_keys = tuple(dict.fromkeys(
                event["event_key"] for event in admitted_events
                if isinstance(event, Mapping) and isinstance(event.get("event_key"), str)
            ))
            grounded_dates = _grounded_due_dates(turn, evidence_events=admitted_events)
            summary = dict(proposed)
            if decision == "UPDATE" and target_memory is not None:
                summary.setdefault("title", target_memory.title)
                summary.setdefault("tags", list(target_memory.tags))
                summary.setdefault("aliases", list(target_memory.aliases))
                summary.setdefault("keywords", list(target_memory.keywords))
                if target_memory.type == "todo" and "status" not in summary:
                    summary["status"] = target_memory.status or "active"
                summary["update_memory_id"] = target_memory.memory_id
            else:
                summary.setdefault("tags", [])
            summary["type"] = memory_type
            summary["scopes"] = scopes
            if isinstance(scope_source, str) and scope_source:
                summary["scope_source"] = scope_source
            summary["sources"] = [{"event_key": key} for key in admitted_keys]
            summary["evidence_event_ids"] = list(admitted_keys)

            normalized_raw = _normalize_summary_dates(
                json.dumps(summary, ensure_ascii=False),
                turn,
                candidate,
            )
            parsed = parse_summarize_output(
                normalized_raw,
                current_event_keys=admitted_keys,
                related_native_ids=related_native_ids,
                related_memory_ids=[target_memory.memory_id] if target_memory is not None else [],
                scope_registry=validation_scope_registry,
                expected_type=memory_type,
                expected_update_memory_id=target_memory.memory_id if target_memory is not None else None,
                expected_target_type=target_memory.type if target_memory is not None else None,
                expected_scopes=scopes,
                expected_scope_source=scope_source if isinstance(scope_source, str) else None,
                allowed_due_dates=grounded_dates,
                allow_no_change=False,
                allow_update_target=target_memory is not None,
            )
            if _summary_date_grounding_violations(
                parsed,
                grounded_dates=grounded_dates,
                source_texts=[
                    event.get("content", "") for event in admitted_events
                    if isinstance(event, Mapping) and event.get("role") in {"user", "assistant"}
                ],
                preserved_texts=(
                    target_memory.title,
                    target_memory.body,
                    target_memory.due_date,
                ) if target_memory is not None else (),
            ):
                raise ModelOutputError(
                    "B3 memory contains a date absent from admitted evidence",
                    validation_detail="relative_time",
                )
            validated_candidates[candidate_id] = candidate
            return parsed

        result = run_single_pass_stage(
            self.model,
            backend,
            evidence_units=planning_units,
            related_memories=local_related,
            native_memories=native_related,
            scope_background=scope_background,
            scope_registry=scope_registry,
            lookup_complete=lookup_complete,
            validate_memory=validate_memory,
            diagnostic_context={
                "source": turn.source,
                "session_id": turn.session_id,
                "turn_index": turn.turn_index,
            },
        )

        fallback_scopes, fallback_scope_source = self._fallback_scopes(scope_background)
        for row in result["items"]:
            candidate_id = row["candidate_id"]
            claims = row["evidence"]
            unit_ids = self._claim_unit_ids(claims)
            for unit_id in unit_ids:
                covered_by_unit.setdefault(unit_id, []).append(candidate_id)
            decision = row["decision"]

            if decision in {"CREATE", "UPDATE"}:
                summary = dict(row["memory"])
                candidate = validated_candidates[candidate_id]
                candidate["evidence_unit_ids"] = unit_ids
                if summary["scopes"] == ["unscoped"] or summary.get("scope_source") == "insufficient_context":
                    self.audit._defer_candidate(
                        turn_ref,
                        candidate,
                        "scope_required",
                        scopes=summary["scopes"],
                        scope_source=summary.get("scope_source"),
                    )
                    continue
                if decision == "CREATE" and _automatic_create_conflicts(
                    candidate,
                    summary,
                    related,
                    ignore_memory_ids=[request.get("memory_id") for request in requests],
                ):
                    self.audit._record_disposition(
                        turn_ref, candidate, "NO_CHANGE", reason="already_covered"
                    )
                    continue
                if any(
                    dedup_digest(request.get("summary", {})) == dedup_digest(summary)
                    for request in requests
                ):
                    self.audit._record_disposition(
                        turn_ref, candidate, "NO_CHANGE", reason="same_turn_duplicate"
                    )
                    continue
                correction_plan = scope_correction_plans.get(candidate_id)
                if (
                    isinstance(correction_plan, Mapping)
                    and isinstance(correction_plan.get("survivor_memory_id"), str)
                    and correction_plan.get("survivor_memory_id")
                ):
                    request = self.inputs._scope_correction_request(
                        candidate,
                        turn,
                        correction_plan,
                        conversation_title=title,
                        native_refs=native_refs,
                    )
                    request["evidence_unit_ids"] = unit_ids
                    requests.append(request)
                    survivor = self.inputs._active_memory_by_id(correction_plan["survivor_memory_id"])
                    self.audit._record_disposition(
                        turn_ref,
                        candidate,
                        "UPDATE",
                        memory_id=survivor.memory_id if survivor is not None else correction_plan["survivor_memory_id"],
                    )
                    if survivor is not None:
                        for value in survivor.scopes:
                            if value not in observed_scopes:
                                observed_scopes.append(value)
                    continue
                request = self._request(
                    summary,
                    turn,
                    candidate_id=candidate_id,
                    conversation_title=title,
                    native_refs=native_refs,
                )
                request["evidence_unit_ids"] = unit_ids
                target = summary.get("update_memory_id")
                if isinstance(target, str) and target:
                    expected_revision = target_revisions.get(target.casefold())
                    if not expected_revision:
                        active = self.inputs._active_memory_by_id(target)
                        if active is None:
                            raise ProcessingError("B3 update target has no planning revision")
                        expected_revision = revision_digest(active)
                    request["expected_revision"] = expected_revision
                requests.append(request)
                self.audit._record_disposition(
                    turn_ref,
                    candidate,
                    decision,
                    memory_id=target if isinstance(target, str) else request["memory_id"],
                )
                for value in summary["scopes"]:
                    if isinstance(value, str) and value not in observed_scopes:
                        observed_scopes.append(value)
                continue

            target = row.get("target_memory_id")
            target_memory = self.inputs._active_memory_by_id(target) if isinstance(target, str) else None
            candidate = {
                "candidate_id": candidate_id,
                "memory": "",
                "duplicate": False,
                "worth": decision == "DEFERRED",
                "type": target_memory.type if target_memory is not None else None,
                "scopes": list(target_memory.scopes) if target_memory is not None else list(fallback_scopes),
                "scope_source": target_memory.scope_source if target_memory is not None else fallback_scope_source,
                "evidence_unit_ids": unit_ids,
            }
            if decision == "NO_CHANGE":
                self.audit._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="single_pass_no_change",
                    memory_id=target if isinstance(target, str) else None,
                )
                if target_memory is not None:
                    for value in target_memory.scopes:
                        if value not in observed_scopes:
                            observed_scopes.append(value)
            else:
                self.audit._defer_candidate(
                    turn_ref,
                    candidate,
                    row["reason"],
                    scopes=candidate["scopes"],
                    scope_source=candidate["scope_source"],
                )

        for row in result["no_memory"]:
            no_memory_by_unit[row["unit_id"]] = row["reason"]

        self._finalize_evidence_audit(
            turn=turn,
            evidence_units=evidence_units,
            retry_ledger=retry_ledger,
            settled_unit_ids=settled_unit_ids,
            settled_source_ids=settled_source_ids,
            planned_settled_sources=planned_settled_sources,
            covered_by_unit=covered_by_unit,
            no_memory_by_unit=no_memory_by_unit,
        )
        return requests, observed_scopes


__all__ = ["SinglePassMemoryPlanner"]
