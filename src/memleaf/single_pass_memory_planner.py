"""Production-shaped automatic planner for the B3 single-pass experiment.

This class intentionally reuses the existing MemoryPlanner request identity,
retry ledger, Core validation and writer contracts while compiling a compact topic-selection and synthesis protocol into B3. Explicit remember remains delegated until its dedicated
B3 contract is validated.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Iterable, Mapping, Optional

from .admission import (
    _whole_unit_is_safe, analyze_turn_evidence, partition_evidence_units,
    summary_evidence,
)
from .evidence_policy import retain_tool_evidence
from .extraction_capability import supports_single_pass_protocol
from .memory_planner import (
    MemoryPlanner,
    _explicit_project_scope_authorizations,
)
from .process_common import (
    ProcessingError,
    _automatic_create_conflicts,
    _event_payload,
    _explicit_project_scope_labels,
    _grounded_deadline_dates,
    _deadline_evidence_text,
    _grounded_due_dates,
    _normalize_summary_dates,
    _parse_time,
    _summary_date_grounding_violations,
)
from .single_pass_plan import run_single_pass_stage
from .turn_plan import dedup_digest, revision_digest
from .validation import (
    ModelOutputError,
    calendar_tokens,
    normalize_relative_calendar_text,
    parse_summarize_output,
)
from .semantic_protocol import _independent_project_subjects
from .scope_state import project_scope_matches_text
from .llm import ModelUnavailable


_SCHEDULE_ACTION_RE = re.compile(
    r"^\s*(?:我(?:们)?\s*)?(?:(?:要|会|将|需(?:要)?|计划|准备|打算|必须)\s*)?"
    r"(?:给(?:出)?|提交|交付|发送|发出|回复|反馈|完成|提供|安排|处理|确认|发布|上线|交接)"
    r"(?!过|了|的|来)",
)


def _candidate_deadline_dates(evidence: Iterable[Mapping[str, Any]]) -> set[str]:
    """Exclude observation cutoffs unless they also schedule an action."""

    projected = []
    for event in evidence:
        anchor = _parse_time(event.get("timestamp"))
        content = str(event.get("content", ""))
        text = _deadline_evidence_text(content, anchor)
        for token in reversed(calendar_tokens(text, anchor)):
            if (re.search(r"(?:截至|截止)\s*[:：]?\s*$", text[:token.start])
                    and not _SCHEDULE_ACTION_RE.match(text[token.end:])):
                text = text[:token.start] + "[observation cutoff]" + text[token.end:]
        projected.append({**event, "content": text})
    return _grounded_deadline_dates(projected)


def _task_basis_deadline_dates(evidence: Iterable[Mapping[str, Any]]) -> set[str]:
    """Recognize dates attached to a task, not dates merely inside its quote."""

    events = tuple(evidence)
    deadlines = _candidate_deadline_dates(events)
    for event in events:
        if event.get("role") != "user":
            continue
        anchor = _parse_time(event.get("timestamp"))
        if anchor is None:
            continue
        text = normalize_relative_calendar_text(str(event.get("content", "")), anchor)
        if text is None:
            continue
        for token in calendar_tokens(text, anchor):
            if (token.canonical is not None and token.canonical >= anchor.date().isoformat()
                    and _SCHEDULE_ACTION_RE.match(text[token.end:])):
                deadlines.add(token.canonical)
    return deadlines


def _resolve_candidate_due_date(
    raw: Any,
    date_evidence: Iterable[Mapping[str, Any]],
    allowed: Iterable[str],
) -> Optional[str]:
    """Resolve one proposed todo deadline to ISO, or return None to drop it.

    The date must be established as a deadline in this candidate's evidence,
    not merely occur there, and be anchored to its own timestamp. Anything else loses the
    date, never the memory.
    """

    grounded = {value for value in allowed if isinstance(value, str) and value}
    if not isinstance(raw, str) or not raw.strip():
        return next(iter(grounded)) if len(grounded) == 1 else None
    value = raw.strip()
    if value in grounded:
        return value
    resolved = set()
    for event in date_evidence:
        if not isinstance(event, Mapping) or value not in str(event.get("content", "")):
            continue
        anchor = _parse_time(event.get("timestamp"))
        if anchor is None:
            continue
        rewritten = normalize_relative_calendar_text(value, anchor)
        if isinstance(rewritten, str):
            tokens = calendar_tokens(rewritten, anchor)
            # A proposed deadline may retain a qualifier such as 下班前.
            # Resolve it only when there is one unambiguous date and that
            # date already passed the candidate-local deadline semantics.
            canonical = {token.canonical for token in tokens}
            if len(canonical) == 1 and None not in canonical:
                resolved.update(canonical & grounded)
        for token in calendar_tokens(value, anchor):
            if token.raw == value and token.canonical in grounded:
                resolved.add(token.canonical)
    return next(iter(resolved)) if len(resolved) == 1 else None


def _claim_date_evidence(
    claims: Iterable[Mapping[str, Any]],
    by_unit: Mapping[str, Any],
    events: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project dates from this candidate's canonical validated exact quotes only."""

    event_by_key = {
        event.get("event_key"): event
        for event in events
        if isinstance(event, Mapping) and isinstance(event.get("event_key"), str)
    }
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        unit_id = claim.get("unit_id")
        quote = claim.get("quote")
        unit = by_unit.get(unit_id) if isinstance(unit_id, str) else None
        if not isinstance(quote, str) or not quote or unit is None:
            continue
        event = event_by_key.get(getattr(unit, "event_key", None))
        if event is None or getattr(unit, "source_role", None) not in {"user", "assistant"}:
            continue
        identity = (unit_id, quote)
        if identity in seen:
            continue
        seen.add(identity)
        result.append({
            "unit_id": unit_id,
            "event_key": getattr(unit, "event_key", None),
            "role": getattr(unit, "source_role", None),
            "timestamp": event.get("timestamp"),
            "content": quote,
        })
    return result


class SinglePassMemoryPlanner(MemoryPlanner):
    """Bounded topic selection and synthesis for an ordinary automatic turn."""

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
            # Failed candidates are proposals for the evidence being retried,
            # not additional outstanding work. Replace them on each attempt.
            self.audit._dispositions_by_turn[turn_ref] = deepcopy([
                row for row in retry_ledger.get("current_candidates", [])
                if row.get("disposition") != "DEFERRED"
            ])
            self.audit._deferred_by_turn[turn_ref] = []
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
        # Sentence splitting must not sever a pronoun from its paragraph's
        # subject. Keep source-local paragraph context without expanding claims.
        event_texts = {event["event_key"]: event.get("content", "") for event in events}
        paragraph_contexts = {}
        for unit in planning_units:
            source_text = event_texts.get(unit.event_key, "")
            if unit.source_role != "user" or not isinstance(source_text, str):
                continue
            start = source_text.rfind("\n", 0, unit.start) + 1
            end = source_text.find("\n", unit.end)
            paragraph_contexts[unit.unit_id] = source_text[start:end if end >= 0 else len(source_text)]
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

            # Ownership and intent are the model's judgement (the contract states
            # them).  Core no longer discards a candidate for being stated only
            # by the assistant: dropping the memory costs more than the risk.

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
            # Caller-selected projects bound the write destination. Source
            # mentions and model ownership decisions cannot expand that set.
            # This callback also validates repaired and reconciled updates.
            selected_projects = {value.casefold() for value in scopes if value.startswith("project:")}
            authorized_projects = {value.casefold() for value in authorized_project_scopes}
            if authorized_projects and not selected_projects <= authorized_projects:
                raise ModelOutputError(
                    "candidate project is outside the explicit process scope",
                    validation_detail="scope_drift",
                )
            # Uncertain ownership must stay uncertain. Mentioning a project
            # in the evidence does not establish that it owns the statement.
            if scopes == ["unscoped"]:
                scope_source = "insufficient_context"

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

            subjects = _independent_project_subjects(str(proposed.get("body", "")), validation_scope_registry)
            if len(subjects) > 1 and any(s.startswith("project:") for s in scopes):
                raise ModelOutputError("independent subjects require separate memories", validation_detail="scope_drift")
            candidate_evidence = _claim_date_evidence(claims, by_unit, events)
            # Ownership is a semantic judgement and belongs to the model, not
            # to Core.  Core used to require the project name to occur in the
            # candidate's evidence, which cannot work: "记录账单的项目" and
            # "记账" are the same project and share no substring, so a literal
            # test rejects a correct answer, and a name the model composed from
            # the user's own words ("记账小玩意儿" for "记账的小玩意儿") was
            # refused and cost the whole memory.  A project name is a label, not
            # a fact, so the model's choice is kept; the guard against inventing
            # ownership stays where it can be judged, in the contract.
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
                    raise ModelOutputError("ownership change lacks correction evidence", validation_detail="scope_drift")
                else:
                    scope_correction_plans[candidate_id] = dict(correction_plan)

            admitted_events = summary_evidence(candidate, planning_units, events=events)
            admitted_keys = tuple(dict.fromkeys(
                event["event_key"] for event in admitted_events
                if isinstance(event, Mapping) and isinstance(event.get("event_key"), str)
            ))
            candidate_date_evidence = candidate_evidence
            grounded_dates = _grounded_due_dates(turn, evidence_events=candidate_date_evidence)
            deadline_dates = _candidate_deadline_dates(candidate_date_evidence)
            basis = decision_context.get("_task_basis")
            task_dates: set[str] = set()
            if memory_type == "todo" and isinstance(basis, Mapping):
                basis_unit = by_unit.get(basis.get("unit_id"))
                if (basis_unit is not None and basis_unit.source_role == "user"
                        and basis_unit.origin == "user_assertion"
                        and any(c.get("unit_id") == basis.get("unit_id")
                                and c.get("quote") == basis.get("quote") for c in claims)):
                    basis_events = _claim_date_evidence([basis], by_unit, events)
                    task_dates = _task_basis_deadline_dates(basis_events)
                    deadline_dates.update(task_dates)
            grounded_dates.update(deadline_dates)
            summary = dict(proposed)
            if (memory_type == "todo" and summary.get("status", "active") == "active"
                    and not summary.get("due_date") and len(task_dates) == 1):
                summary["due_date"] = next(iter(task_dates))
            # 日期校验：只接受在本候选证据里出现过、且能锚定成 ISO 的日期。
            # 校验不通过时只丢掉日期字段，候选照常写入。
            if memory_type == "todo":
                resolved_due = _resolve_candidate_due_date(
                    summary.get("due_date"),
                    candidate_date_evidence,
                    deadline_dates,
                )
                if resolved_due is None:
                    summary.pop("due_date", None)
                else:
                    summary["due_date"] = resolved_due
            if decision == "UPDATE" and target_memory is not None:
                summary.setdefault("title", target_memory.title)
                summary.setdefault("tags", list(target_memory.tags))
                summary.setdefault("aliases", list(target_memory.aliases))
                summary.setdefault("keywords", list(target_memory.keywords))
                if target_memory.type == "todo" and "status" not in summary:
                    summary["status"] = target_memory.status or "active"
                if target_memory.type == "todo" and "due_date" not in summary:
                    # An update that stays silent about the deadline keeps the
                    # one already recorded instead of silently clearing it.
                    summary["due_date"] = target_memory.due_date
                summary["update_memory_id"] = target_memory.memory_id
            else:
                summary.setdefault("tags", [])
            summary["type"] = memory_type
            summary["scopes"] = scopes
            if isinstance(scope_source, str) and scope_source:
                summary["scope_source"] = scope_source
            if memory_type == "todo" and summary.get("status") == "completed" and not summary.get("completed_at"):
                # Completion is a state transition. Record when it was
                # observed, without asking the model to invent a finish time.
                observed = [event.get("timestamp") for event in candidate_date_evidence
                            if _parse_time(event.get("timestamp")) is not None]
                prior = target_memory.completed_at if target_memory is not None and target_memory.status == "completed" else None
                if prior or observed:
                    summary["completed_at"] = prior or max(observed, key=_parse_time)
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
                # The deadline was already resolved or dropped above; no second
                # grounding gate decides whether the memory survives.
                allowed_due_dates=None,
                allow_no_change=False,
                allow_update_target=target_memory is not None,
            )
            # 正文里的日期若无法锚定，只记录诊断，不再丢弃整条记忆。
            date_violations = _summary_date_grounding_violations(
                parsed,
                grounded_dates=grounded_dates,
                source_texts=[
                    event.get("content", "") for event in candidate_date_evidence
                ],
                preserved_texts=(
                    target_memory.title,
                    target_memory.body,
                    target_memory.due_date,
                ) if target_memory is not None else (),
            )
            if date_violations:
                # 正文里的日期无法用本轮证据锚定时只记账，不再丢弃整条记忆。
                event = getattr(self.model, "_record_metric_event", None)
                if callable(event):
                    try:
                        event({}, "unaligned_date_count", len(date_violations))
                    except Exception:
                        pass
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
            evidence_contexts=paragraph_contexts,
            evidence_timestamps={
                event["event_key"]: event.get("timestamp")
                for event in events
                if isinstance(event.get("event_key"), str)
            },
        )

        settled_candidates = self.audit._dispositions_by_turn.get(turn_ref, [])
        if retry_ledger.get("same_turn") and settled_candidates:
            # c1/r1 are per-response positions. A retry of remaining evidence
            # must not overwrite a settled sibling with the same position.
            namespace = hashlib.sha256(json.dumps(
                sorted(row["candidate_id"] for row in settled_candidates),
                separators=(",", ":"),
            ).encode()).hexdigest()[:10]
            renamed = {row["candidate_id"]: f"retry{namespace}_{row['candidate_id']}"
                       for row in result["items"]}
            for row in result["items"]:
                row["candidate_id"] = renamed[row["candidate_id"]]
            for field in ("_defer_details", "_defer_diagnostics", "_candidate_contexts"):
                if isinstance(result.get(field), Mapping):
                    result[field] = {renamed.get(key, key): value for key, value in result[field].items()}
            validated_candidates = {renamed.get(key, key): {**value, "candidate_id": renamed.get(key, key)}
                                    for key, value in validated_candidates.items()}
            scope_correction_plans = {renamed.get(key, key): value for key, value in scope_correction_plans.items()}
        raw_defer_details = result.get("_defer_details") if isinstance(result, Mapping) else None
        defer_details = (
            {key: value for key, value in raw_defer_details.items()
             if isinstance(key, str) and isinstance(value, str)}
            if isinstance(raw_defer_details, Mapping)
            else {}
        )
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
                # 归属缺失不再拦下候选：写入前统一落到可用的 scope。
                if summary.get("scopes") in (["unscoped"], [], None):
                    summary["scopes"] = list(candidate.get("scopes") or ["global"])
                    candidate["scopes"] = summary["scopes"]
                    if summary.get("scope_source") == "insufficient_context":
                        summary["scope_source"] = "model"
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

            context = result.get("_candidate_contexts", {}).get(candidate_id, {})
            target = row.get("target_memory_id", context.get("target_memory_id"))
            target_memory = self.inputs._active_memory_by_id(target) if isinstance(target, str) else None
            candidate_scopes = context.get("scopes")
            if not candidate_scopes:
                # Deferred evidence has no ownership verdict. Never attach the
                # last successful project's scope to an unrelated failed row.
                text = "\n".join(c.get("quote", "") for c in claims)
                candidate_scopes = project_scope_matches_text(text, {"scopes": validation_scope_registry})
            candidate_scopes = list(candidate_scopes or ["unscoped"])
            candidate = {
                "candidate_id": candidate_id,
                "memory": "",
                "duplicate": False,
                "worth": decision == "DEFERRED",
                "type": target_memory.type if target_memory is not None else context.get("type"),
                "scopes": list(target_memory.scopes) if target_memory is not None else candidate_scopes,
                "scope_source": target_memory.scope_source if target_memory is not None else ("insufficient_context" if candidate_scopes == ["unscoped"] else "model"),
                "evidence_unit_ids": unit_ids,
            }
            diagnostics = result.get("_defer_diagnostics", {}).get(candidate_id)
            if isinstance(diagnostics, Mapping):
                candidate["validation_diagnostics"] = dict(diagnostics)
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
                    validation_detail=defer_details.get(candidate_id),
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
