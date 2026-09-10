from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()

# --- Harden the new B3 protocol before wiring it into the planner. ---
plan_path = ROOT / "src/memleaf/single_pass_plan.py"
text = plan_path.read_text(encoding="utf-8")
text = text.replace("create_allowed", "lookup_complete")
text = text.replace(
    '    "negated",\n})',
    '    "negated",\n    "native_already_covered",\n})',
)
text = text.replace(
    "CREATE: only for durable information not already represented by a supplied local memory, and only when input lookup_complete=true. UPDATE: one supplied local memory represents the same evolving future use and current evidence establishes a real change. NO_CHANGE: current evidence is a duplicate/restatement or establishes no semantic change to that supplied target. DEFERRED: a durable candidate exists but target/scope/ownership/evidence is unsafe to decide.",
    "CREATE/UPDATE/NO_CHANGE are terminal decisions and require input lookup_complete=true. CREATE: only for durable information not already represented by a supplied local memory. UPDATE: one supplied local memory represents the same evolving future use and current evidence establishes a real change. NO_CHANGE: current evidence is a duplicate/restatement or establishes no semantic change to that supplied target. DEFERRED: a durable candidate exists but target/scope/ownership/evidence or lookup completeness is unsafe to decide.",
)
text = text.replace(
    '    required = {"title", "body", "tags"}\n',
    '    required = {"title", "body"}\n',
)
old_callable = 'MemoryValidator = Callable[[str, str, str | None, Mapping[str, Any] | None, Mapping[str, Any], list[dict[str, Any]]], Mapping[str, Any]]'
new_callable = 'MemoryValidator = Callable[[str, str, str | None, Mapping[str, Any] | None, Mapping[str, Any], list[dict[str, Any]], Mapping[str, Any]], Mapping[str, Any]]'
if old_callable not in text:
    raise SystemExit("MemoryValidator anchor missing")
text = text.replace(old_callable, new_callable, 1)
old_parse_prelude = '''    if not callable(validate_memory):
        raise TypeError("validate_memory must be callable")
    _, source_units = _evidence_projection(evidence_units)
    _, local_by_key = _local_catalog(local_memories)
'''
new_parse_prelude = '''    if not callable(validate_memory):
        raise TypeError("validate_memory must be callable")
    source_units = tuple(evidence_units)
    if any(
        isinstance(unit, Mapping)
        or not isinstance(getattr(unit, "unit_id", None), str)
        or getattr(unit, "can_support", False) is not True
        for unit in source_units
    ):
        raise TypeError("B3 parser evidence_units must be validated EvidenceUnit objects")
    _evidence_projection(source_units)
    _, local_by_key = _local_catalog(local_memories)
'''
if old_parse_prelude not in text:
    raise SystemExit("parse prelude anchor missing")
text = text.replace(old_parse_prelude, new_parse_prelude, 1)
old_decision_check = '''        if set(raw_item) != decision_fields[decision]:
            raise ModelOutputError("B3 item fields do not match decision", validation_detail="unknown_fields")
        candidate_ids.add(candidate_id)
'''
new_decision_check = '''        if set(raw_item) != decision_fields[decision]:
            detail = "unknown_fields" if set(raw_item) - decision_fields[decision] else "missing_fields"
            raise ModelOutputError("B3 item fields do not match decision", validation_detail=detail)
        if decision in {"CREATE", "UPDATE", "NO_CHANGE"} and not lookup_complete:
            raise ModelOutputError(
                "incomplete B3 lookup cannot authorize a terminal decision",
                validation_detail="other_schema_violation",
            )
        candidate_ids.add(candidate_id)
'''
if old_decision_check not in text:
    raise SystemExit("decision check anchor missing")
text = text.replace(old_decision_check, new_decision_check, 1)
old_create_guard = '''        if decision == "CREATE":
            if not lookup_complete:
                raise ModelOutputError("B3 CREATE is forbidden by lookup state", validation_detail="other_schema_violation")
            if item.get("type") not in MEMORY_TYPES:
'''
new_create_guard = '''        if decision == "CREATE":
            if item.get("type") not in MEMORY_TYPES:
'''
if old_create_guard not in text:
    raise SystemExit("create guard anchor missing")
text = text.replace(old_create_guard, new_create_guard, 1)
text = text.replace(
    'validated = validate_memory(candidate_id, decision, None, None, item["memory"], normalized["evidence"])',
    'validated = validate_memory(candidate_id, decision, None, None, item["memory"], normalized["evidence"], item)',
)
text = text.replace(
    'validated = validate_memory(candidate_id, decision, target_id, target_record, item["memory"], normalized["evidence"])',
    'validated = validate_memory(candidate_id, decision, target_id, target_record, item["memory"], normalized["evidence"], item)',
)
plan_path.write_text(text, encoding="utf-8")

# Rename only the B3-facing completeness vocabulary in PlanningContext.
ctx_path = ROOT / "src/memleaf/planning_context.py"
ctx = ctx_path.read_text(encoding="utf-8")
ctx = ctx.replace(
    '        ``create_allowed`` is true only when the bounded related projection is\n        complete and a scoped fallback did not discover multiple ambiguous\n        records. UPDATE/NO_CHANGE may still use returned local targets when\n        CREATE is disabled.\n',
    '        ``lookup_complete`` is true only when the bounded related projection is\n        complete and a scoped fallback did not discover multiple ambiguous\n        records. B3 permits no CREATE/UPDATE/NO_CHANGE when this proof is false.\n',
)
ctx = ctx.replace(
    '        create_allowed = bool(bound_complete and not fallback_ambiguous)\n        return related, scope_background, native_refs, scope_fallback, create_allowed\n',
    '        lookup_complete = bool(bound_complete and not fallback_ambiguous)\n        return related, scope_background, native_refs, scope_fallback, lookup_complete\n',
)
ctx_path.write_text(ctx, encoding="utf-8")

# Update protocol tests for the hardened lookup-complete and validator contract.
test_path = ROOT / "tests/test_b3_single_pass_plan.py"
test = test_path.read_text(encoding="utf-8")
test = test.replace("create_allowed", "lookup_complete")
test = test.replace(
    'def validator(candidate_id, decision, target, target_record, proposed, evidence):',
    'def validator(candidate_id, decision, target, target_record, proposed, evidence, context):',
)
insert_anchor = '''    def test_evidence_coverage_must_be_complete_and_disjoint(self):
'''
new_test = '''    def test_incomplete_lookup_forbids_update_and_nochange_too(self):
        evidence = [unit("u1", "Alpha changed.")]
        for decision, extra in (
            ("UPDATE", {"target_memory_id": "m1", "memory": memory()}),
            ("NO_CHANGE", {"target_memory_id": "m1"}),
        ):
            with self.subTest(decision=decision):
                raw = json.dumps({
                    "protocol_version": PROTOCOL_VERSION,
                    "items": [{
                        "candidate_id": "c1",
                        "decision": decision,
                        "evidence": [claim("u1", "Alpha changed.")],
                        **extra,
                    }],
                    "no_memory": [],
                })
                with self.assertRaises(ModelOutputError):
                    parse_single_pass_output(
                        raw,
                        evidence_units=evidence,
                        local_memories=[local("m1")],
                        lookup_complete=False,
                        validate_memory=validator,
                    )

    def test_parser_requires_real_evidence_units_for_authority_validation(self):
        raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [],
            "no_memory": [{"unit_id": "u1", "reason": "no_future_value"}],
        })
        with self.assertRaises(TypeError):
            parse_single_pass_output(
                raw,
                evidence_units=[{"unit_id": "u1", "event_key": "e", "role": "user", "content": "x"}],
                local_memories=[],
                lookup_complete=True,
                validate_memory=validator,
            )

'''
if insert_anchor not in test:
    raise SystemExit("test insertion anchor missing")
test = test.replace(insert_anchor, new_test + insert_anchor, 1)
test_path.write_text(test, encoding="utf-8")

planner_path = ROOT / "src/memleaf/single_pass_memory_planner.py"
planner_path.write_text(r'''"""Production-shaped automatic planner for the B3 single-pass experiment.

This class intentionally reuses the existing MemoryPlanner request identity,
retry ledger, Core validation and writer contracts while replacing the normal
automatic Gate -> reconciliation -> Summary -> semantic-review chain with one
semantic model call.  Explicit remember remains delegated until its dedicated
B3 contract is validated.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Iterable, Mapping, Optional

from .admission import analyze_turn_evidence, partition_evidence_units, summary_evidence
from .evidence_policy import retain_tool_evidence
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
        # Explicit remember has different authorization semantics. Keep the
        # proven legacy route until the dedicated B3 explicit contract lands.
        if explicit:
            return super()._collect_turn_outputs(
                backend,
                turn,
                state,
                explicit=True,
                explicit_candidate=explicit_candidate,
                scope=scope,
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
                scopes = list(target_memory.scopes)
                scope_source = target_memory.scope_source
            else:
                memory_type = decision_context.get("type")
                scopes = list(decision_context.get("scopes", []))
                scope_source = decision_context.get("scope_source")

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
            if decision == "CREATE" and not _model_project_scope_is_source_grounded(
                candidate,
                planning_units,
                validation_scope_registry,
                authorized_project_scopes,
            ):
                raise ModelOutputError(
                    "B3 project scope is not grounded by claimed source",
                    validation_detail="scope_not_grounded",
                )

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
''', encoding="utf-8")

integration_test = ROOT / "tests/test_b3_single_pass_memory_planner.py"
integration_test.write_text(r'''from __future__ import annotations

import json
import unittest
from contextlib import nullcontext
from types import SimpleNamespace

from memleaf.inbox import InboxEvent, InboxTurn
from memleaf.models import Memory
from memleaf.single_pass_memory_planner import SinglePassMemoryPlanner
from memleaf.turn_audit import TurnAudit
from memleaf.turn_plan import TurnPlan, revision_digest
from memleaf.validation import ModelOutputError
from memleaf.single_pass_plan import PROTOCOL_VERSION


TURN_KEY = "a" * 64


def turn(user: str, assistant: str = "Noted.") -> InboxTurn:
    events = (
        InboxEvent("hermes", "s", TURN_KEY, 1, "user", "b" * 64, user, turn_id="u"),
        InboxEvent("hermes", "s", TURN_KEY, 1, "assistant", "c" * 64, assistant, turn_id="a"),
    )
    return InboxTurn("hermes", "s", TURN_KEY, 1, events)


def active(memory_id: str, body: str, *, scope="global", memory_type="fact") -> Memory:
    return Memory(
        memory_id=memory_id,
        title="Existing",
        body=body,
        tags=[],
        type=memory_type,
        scopes=[scope],
        aliases=[],
        keywords=[],
        scope_source="model",
        sources=[],
        created="2026-09-10T00:00:00Z",
        updated="2026-09-10T00:00:00Z",
    )


class Vault:
    processed_state_path = None
    def __init__(self, scopes=None): self.scopes = scopes or {}
    def config(self): return {"scopes": self.scopes}
    def lock(self): return nullcontext()


class Service:
    def __init__(self, scopes=None): self.vault = Vault(scopes)


class Inputs:
    def __init__(self, related=(), target=None, *, lookup_complete=True, scope_background=None):
        self.related = [dict(item) for item in related]
        self.target = target
        self.lookup_complete = lookup_complete
        self.scope_background = scope_background or []
        self._planned_related = []

    def _single_pass_related(self, *args, **kwargs):
        native_refs = [
            {"source_id": row.get("native_source_id", "native"), "native_id": row["native_id"]}
            for row in self.related if row.get("native") is True and isinstance(row.get("native_id"), str)
        ]
        return list(self.related), self.scope_background, native_refs, None, self.lookup_complete

    def _scope_registry_projection(self): return []
    def _conversation_title(self, turn): return "Conversation"
    def _active_memory_by_id(self, memory_id):
        if self.target is not None and isinstance(memory_id, str) and memory_id.casefold() == self.target.memory_id.casefold():
            return self.target
        return None


class Model:
    def __init__(self, response): self.response = response; self.calls = []
    def _complete_json_stage(self, backend, prompt, *, system, purpose, parser, diagnostic_context=None):
        self.calls.append((purpose, prompt, system))
        return parser(json.dumps(self.response, ensure_ascii=False))


def item_claim(prompt: str, text: str) -> dict:
    payload = json.loads(prompt.split("B3_INPUT\n", 1)[1].split("\nReturn", 1)[0])
    unit = next(row for row in payload["current_evidence"] if text in row["content"])
    return {"unit_id": unit["unit_id"], "quote": text, "role": "assertion"}


class DynamicModel:
    def __init__(self, build): self.build=build; self.calls=[]
    def _complete_json_stage(self, backend, prompt, *, system, purpose, parser, diagnostic_context=None):
        self.calls.append((purpose,prompt,system))
        return parser(json.dumps(self.build(prompt), ensure_ascii=False))


class B3SinglePassMemoryPlannerTests(unittest.TestCase):
    def planner(self, response_builder, *, related=(), target=None, lookup_complete=True, scope_background=None):
        audit = TurnAudit()
        audit._planned_related = []
        model = DynamicModel(response_builder)
        planner = SinglePassMemoryPlanner(
            Service(), audit, Inputs(related, target, lookup_complete=lookup_complete, scope_background=scope_background), model
        )
        return planner, audit, model

    def test_create_is_one_model_call_and_writer_compatible_request(self):
        def response(prompt):
            return {
                "protocol_version": PROTOCOL_VERSION,
                "items": [{
                    "candidate_id": "c1",
                    "decision": "CREATE",
                    "type": "fact",
                    "scopes": ["global"],
                    "scope_source": "model",
                    "evidence": [item_claim(prompt, "Alpha uses PostgreSQL.")],
                    "memory": {"title": "Alpha database", "body": "Alpha uses PostgreSQL."},
                }],
                "no_memory": [
                    {"unit_id": json.loads(prompt.split("B3_INPUT\n",1)[1].split("\nReturn",1)[0])["current_evidence"][1]["unit_id"], "reason": "assistant_restatement"}
                ],
            }
        planner, audit, model = self.planner(response)
        requests, scopes = planner._collect_turn_outputs("backend", turn("Alpha uses PostgreSQL."), {})
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0][0], "gate")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["summary"]["body"], "Alpha uses PostgreSQL.")
        self.assertEqual(requests[0]["summary"]["tags"], [])
        self.assertTrue(requests[0]["evidence_unit_ids"])
        self.assertEqual(len(TurnPlan.from_requests(requests).candidates), 1)
        self.assertIn("global", scopes)
        ref = ("hermes", "s", TURN_KEY)
        self.assertEqual(audit._dispositions_by_turn[ref][0]["disposition"], "CREATE")

    def test_update_is_one_call_and_freezes_revision(self):
        target = active("m-db", "Alpha uses MySQL.")
        related = [target.to_dict()]
        def response(prompt):
            payload = json.loads(prompt.split("B3_INPUT\n",1)[1].split("\nReturn",1)[0])
            assistant_uid = payload["current_evidence"][1]["unit_id"]
            return {
                "protocol_version": PROTOCOL_VERSION,
                "items": [{
                    "candidate_id": "c1",
                    "decision": "UPDATE",
                    "target_memory_id": "M-DB",
                    "evidence": [item_claim(prompt, "Alpha now uses PostgreSQL instead of MySQL.")],
                    "memory": {"title": "Existing", "body": "Alpha now uses PostgreSQL instead of MySQL."},
                }],
                "no_memory": [{"unit_id": assistant_uid, "reason": "assistant_restatement"}],
            }
        planner, audit, model = self.planner(response, related=related, target=target)
        requests, _ = planner._collect_turn_outputs("backend", turn("Alpha now uses PostgreSQL instead of MySQL."), {})
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(requests[0]["summary"]["update_memory_id"], "m-db")
        self.assertEqual(requests[0]["expected_revision"], revision_digest(target))
        self.assertEqual(requests[0]["summary"]["type"], "fact")
        self.assertEqual(requests[0]["summary"]["scopes"], ["global"])

    def test_no_change_and_query_make_no_requests(self):
        target = active("m-db", "Alpha uses PostgreSQL.")
        def response(prompt):
            payload = json.loads(prompt.split("B3_INPUT\n",1)[1].split("\nReturn",1)[0])
            user = payload["current_evidence"][0]
            assistant = payload["current_evidence"][1]
            return {
                "protocol_version": PROTOCOL_VERSION,
                "items": [{
                    "candidate_id": "c1",
                    "decision": "NO_CHANGE",
                    "target_memory_id": "m-db",
                    "evidence": [{"unit_id": user["unit_id"], "whole_unit": True, "role": "assertion"}],
                }],
                "no_memory": [{"unit_id": assistant["unit_id"], "reason": "assistant_restatement"}],
            }
        planner, audit, model = self.planner(response, related=[target.to_dict()], target=target)
        requests, _ = planner._collect_turn_outputs("backend", turn("Alpha uses PostgreSQL."), {})
        self.assertEqual(requests, [])
        self.assertEqual(len(model.calls), 1)
        ref=("hermes","s",TURN_KEY)
        self.assertEqual(audit._dispositions_by_turn[ref][0]["disposition"], "NO_CHANGE")

    def test_incomplete_lookup_cannot_write(self):
        def response(prompt):
            payload=json.loads(prompt.split("B3_INPUT\n",1)[1].split("\nReturn",1)[0])
            user=payload["current_evidence"][0]; assistant=payload["current_evidence"][1]
            return {
                "protocol_version": PROTOCOL_VERSION,
                "items": [{
                    "candidate_id":"c1","decision":"CREATE","type":"fact","scopes":["global"],"scope_source":"model",
                    "evidence":[{"unit_id":user["unit_id"],"whole_unit":True,"role":"assertion"}],
                    "memory":{"title":"x","body":"x"},
                }],
                "no_memory":[{"unit_id":assistant["unit_id"],"reason":"assistant_restatement"}],
            }
        planner, _, model = self.planner(response, lookup_complete=False)
        with self.assertRaises(ModelOutputError):
            planner._collect_turn_outputs("backend", turn("Remember x."), {})
        self.assertEqual(len(model.calls), 1)

    def test_deferred_keeps_turn_retryable_without_request(self):
        def response(prompt):
            payload=json.loads(prompt.split("B3_INPUT\n",1)[1].split("\nReturn",1)[0])
            user=payload["current_evidence"][0]; assistant=payload["current_evidence"][1]
            return {
                "protocol_version":PROTOCOL_VERSION,
                "items":[{
                    "candidate_id":"c1","decision":"DEFERRED","reason":"scope_ambiguous",
                    "evidence":[{"unit_id":user["unit_id"],"whole_unit":True,"role":"assertion"}],
                }],
                "no_memory":[{"unit_id":assistant["unit_id"],"reason":"assistant_restatement"}],
            }
        planner,audit,model=self.planner(response, scope_background=[])
        requests,_=planner._collect_turn_outputs("backend",turn("Project ownership is unclear."),{})
        self.assertEqual(requests,[]); self.assertEqual(len(model.calls),1)
        ref=("hermes","s",TURN_KEY)
        self.assertEqual(audit._dispositions_by_turn[ref][0]["disposition"],"DEFERRED")
        self.assertTrue(audit._deferred_by_turn[ref])
        self.assertTrue(any(row["decision"]=="DEFERRED" for row in audit._evidence_by_turn[ref]))


if __name__ == "__main__":
    unittest.main()
''', encoding="utf-8")
