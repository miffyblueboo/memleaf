"""Build memory-change requests without committing Markdown."""
from __future__ import annotations
import hashlib
import json
from copy import deepcopy
from typing import Any, Iterable, Mapping, Optional
from .admission import analyze_turn_evidence, admission_reason, partition_evidence_units, read_only_turn, summary_evidence, evidence_prompt, gate_evidence_batches, parse_coverage, resolve_omitted_candidate_event_ids, split_gate_envelope, supporting_units, split_semantic_envelope, validate_bindings, validate_coverage_bindings
from .index import turn_key
from .inbox import InboxTurn
from .llm import ModelError
from .memory_writer import MemoryWriter
from .turn_plan import dedup_digest, revision_digest
from .create_coordinator import CreateCoordinator
from .update_coordinator import UpdateCoordinator
from .target_reconciliation import reconcile_candidate_target
from .evidence_policy import retain_tool_evidence
from .parallel_model import run_ordered_keyed_jobs
from .prompts import COVERAGE_ALREADY_COMPLETED_CORRECTION, COVERAGE_CORRECTION, GATE_COVERAGE_SYSTEM, GATE_SYSTEM, SUMMARIZE_SYSTEM, coverage_repair_prompt, gate_prompt, summarize_prompt
from .retrieval import normalize_term
from .scope_state import ScopeError, normalize_scopes
from .validation import ModelOutputError, NO_CHANGE_DECISION, _model_scope_grounding_evidence, parse_gate_output, parse_strict_json, parse_summarize_output
from .process_common import ProcessingError, _TARGET_NOT_RELATED, _TARGET_SAME_USE, _TARGET_UNKNOWN, _automatic_create_conflicts, _candidate_lookup_queries, _event_payload, _grounded_due_dates, _normalize_summary_dates, _summary_date_grounding_violations


def _explicit_project_scope_authorizations(scope: Any) -> tuple[str, ...]:
    """Normalize only the caller's explicit process scope authorization."""

    if scope is None:
        return ()
    try:
        values = normalize_scopes(scope, field="process scope")
    except (ScopeError, TypeError, ValueError):
        return ()
    return tuple(
        value for value in values
        if isinstance(value, str) and value.partition(":")[0] == "project"
    )



def _candidate_bound_source_text(
    candidate: Mapping[str, Any],
    batch_units: Iterable[Any],
) -> str:
    """Project only original source text from this candidate's batch claims.

    Explicit bindings identify the source units directly.  The compatibility
    path uses the exact-whole unit IDs frozen by ``supporting_units``.  In both
    cases the model's quote or memory text is excluded from this projection.
    """

    by_id = {unit.unit_id: unit for unit in batch_units}
    bindings = candidate.get("_evidence_bindings")
    if isinstance(bindings, list):
        unit_ids = [
            claim.get("unit_id")
            for claim in bindings
            if isinstance(claim, Mapping) and isinstance(claim.get("unit_id"), str)
        ]
    else:
        raw_ids = candidate.get("_evidence_unit_ids", ())
        unit_ids = (
            [value for value in raw_ids if isinstance(value, str)]
            if isinstance(raw_ids, Iterable) and not isinstance(raw_ids, (str, bytes))
            else []
        )
    texts: list[str] = []
    seen: set[str] = set()
    for unit_id in unit_ids:
        if unit_id in seen:
            continue
        unit = by_id.get(unit_id)
        if unit is None or not getattr(unit, "can_support", False):
            continue
        seen.add(unit_id)
        text = getattr(unit, "text", "")
        if isinstance(text, str) and text:
            texts.append(text)
    return "\n".join(texts)


def _model_project_scope_is_source_grounded(
    candidate: Mapping[str, Any],
    batch_units: Iterable[Any],
    scope_registry: Mapping[str, Any] | None,
    authorized_scopes: Iterable[str] = (),
) -> bool:
    """Validate every model-selected project Scope against exact bound source.

    Registered and newly named projects intentionally use the same rule. Core
    verifies that the selected project's canonical name or configured alias is
    present in this candidate's immutable bound source, unless the caller
    explicitly authorized that project Scope. Core does not infer whether some
    other mentioned name is an owner, implementation platform, product,
    notification source, or comparison context; semantic review owns that
    relationship judgment.
    """

    if candidate.get("scope_source") != "model" or not candidate.get("worth"):
        return True
    scopes = tuple(
        scope
        for scope in candidate.get("scopes", ())
        if isinstance(scope, str) and scope.partition(":")[0] == "project"
    )
    if not scopes:
        return True
    authorized = {
        value.casefold()
        for value in authorized_scopes
        if isinstance(value, str) and value.partition(":")[0] == "project"
    }
    source_text = _candidate_bound_source_text(candidate, batch_units)
    for scope in scopes:
        if scope.casefold() in authorized:
            continue
        if not source_text:
            return False
        owners, matches = _model_scope_grounding_evidence(
            source_text, (scope,), scope_registry
        )
        owner = owners.get(scope.casefold(), scope.casefold())
        if owner not in matches:
            return False
    return True


class MemoryPlanner:
    def __init__(self, service: Any, audit: Any, inputs: Any, model: Any):
        self.service = service
        self.audit = audit
        self.inputs = inputs
        self.model = model

    @staticmethod
    def _evidence_source_identity(
        unit: Any,
        *,
        source: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        """Return a stable identity for one exact source fragment.

        ``EvidenceUnit.unit_id`` deliberately includes the enclosing event key,
        which changes when the same external observation is captured again in a
        later visible turn.  Retry accounting needs the source identity as well
        as the per-turn unit identity, so use immutable provenance, exact span,
        source/session scope, and an exact body digest.  This is an identity
        comparison, never a business-text similarity check, and avoids putting
        source text in the durable ledger.  Conversation fragments retain their
        event key so this helper cannot turn identical user text from another
        turn into the same source.
        """

        origin = str(getattr(unit, "origin", ""))
        external = origin == "external_observation"
        value = {
            "kind": "external" if external else "conversation",
            "source": source,
            "session_id": session_id,
            "source_role": str(getattr(unit, "source_role", "")),
            "tool_name": getattr(unit, "tool_name", None) if external else None,
            "call_id": getattr(unit, "call_id", None) if external else None,
            "record_id": getattr(unit, "record_id", None) if external else None,
            "domain": getattr(unit, "domain", None),
            "event_key": getattr(unit, "event_key", None) if not external else None,
            "start": getattr(unit, "start", 0),
            "end": getattr(unit, "end", 0),
            "text_digest": hashlib.sha256(
                str(getattr(unit, "text", "")).encode("utf-8")
            ).hexdigest(),
        }
        return "source-" + hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]

    @classmethod
    def _retry_ledger(cls, state: Mapping[str, Any], turn: InboxTurn) -> dict[str, Any]:
        """Read settled source identities from prior journal entries.

        A processed turn can remain in the inbox while unresolved evidence is
        retried.  The ledger is therefore the authority for which units may be
        sent through the model again; a turn watermark would hide its pending
        siblings.  The helper also returns the exact current-turn audit rows so
        a partial retry can preserve prior CREATE/UPDATE/NO_CHANGE outcomes.
        """

        entries = state.get("processed_turns") if isinstance(state, Mapping) else None
        if not isinstance(entries, list):
            entries = []
        current_entry: Optional[Mapping[str, Any]] = None
        for raw_entry in entries:
            if (
                isinstance(raw_entry, Mapping)
                and raw_entry.get("turn_key") == turn.turn_key
            ):
                current_entry = raw_entry
                break

        settled_unit_ids: set[str] = set()
        settled_source_ids: set[str] = set()
        settled_rows_by_unit: dict[str, dict[str, Any]] = {}
        settled_rows_by_source: dict[str, dict[str, Any]] = {}
        settled_memory_by_source: dict[str, str] = {}

        for raw_entry in entries:
            if not isinstance(raw_entry, Mapping):
                continue
            raw_candidates = raw_entry.get("candidate_dispositions", [])
            candidates = {
                row.get("candidate_id").casefold(): row
                for row in raw_candidates
                if isinstance(row, Mapping) and isinstance(row.get("candidate_id"), str)
            } if isinstance(raw_candidates, list) else {}
            raw_evidence = raw_entry.get("evidence_dispositions", [])
            if not isinstance(raw_evidence, list):
                continue
            for raw_row in raw_evidence:
                if not isinstance(raw_row, Mapping):
                    continue
                unit_id = raw_row.get("unit_id")
                source_id = raw_row.get("source_identity")
                decision = raw_row.get("decision")
                candidate_ids = raw_row.get("candidate_ids", [])
                if not isinstance(candidate_ids, list):
                    candidate_ids = []
                candidate_dispositions = []
                missing_candidate_disposition = False
                for candidate_id in candidate_ids:
                    if not isinstance(candidate_id, str):
                        missing_candidate_disposition = True
                        continue
                    candidate = candidates.get(candidate_id.casefold())
                    if not isinstance(candidate, Mapping):
                        missing_candidate_disposition = True
                        continue
                    candidate_dispositions.append(candidate.get("disposition"))
                candidate_terminal = bool(candidate_ids) and not missing_candidate_disposition and all(
                    disposition in {"CREATE", "UPDATE", "NO_CHANGE"}
                    for disposition in candidate_dispositions
                )
                settled = (
                    decision == "NO_CHANGE"
                    and raw_row.get("reason") not in {
                        "coverage_unresolved",
                        "incomplete_tool_evidence",
                    }
                ) or (
                    decision == "CANDIDATE" and candidate_terminal
                )
                if not settled:
                    continue
                row = dict(raw_row)
                if isinstance(unit_id, str) and unit_id:
                    settled_unit_ids.add(unit_id)
                    settled_rows_by_unit.setdefault(unit_id, row)
                # Conversation rows can only be replayed by their exact
                # unit_id.  Their text/span digest is retained for audit, but
                # must never suppress the same wording in a later turn.
                source_kind = raw_row.get("source_kind")
                if source_kind == "external" and isinstance(source_id, str) and source_id:
                    settled_source_ids.add(source_id)
                    settled_rows_by_source.setdefault(source_id, row)
                    memory_id = row.get("memory_id")
                    if isinstance(memory_id, str) and memory_id:
                        settled_memory_by_source.setdefault(source_id, memory_id)
                    for candidate_id in candidate_ids:
                        candidate = (
                            candidates.get(candidate_id.casefold())
                            if isinstance(candidate_id, str)
                            else None
                        )
                        if isinstance(candidate, Mapping) and isinstance(candidate.get("memory_id"), str):
                            settled_memory_by_source.setdefault(source_id, candidate["memory_id"])

        current_candidates = []
        current_deferred = []
        if isinstance(current_entry, Mapping):
            raw_candidates = current_entry.get("candidate_dispositions", [])
            if isinstance(raw_candidates, list):
                current_candidates = [dict(row) for row in raw_candidates if isinstance(row, Mapping)]
            raw_deferred = current_entry.get("deferred_candidates", [])
            if isinstance(raw_deferred, list):
                current_deferred = [dict(row) for row in raw_deferred if isinstance(row, Mapping)]

        return {
            "current_entry": current_entry,
            "same_turn": isinstance(current_entry, Mapping),
            "settled_unit_ids": settled_unit_ids,
            "settled_source_ids": settled_source_ids,
            "settled_rows_by_unit": settled_rows_by_unit,
            "settled_rows_by_source": settled_rows_by_source,
            "settled_memory_by_source": settled_memory_by_source,
            "current_candidates": current_candidates,
            "current_deferred": current_deferred,
        }

    @staticmethod
    def _planned_memory(request: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        """Project a not-yet-committed request into the next turn's lookup."""

        summary = request.get("summary")
        if not isinstance(summary, Mapping) or request.get("duplicate_memory_id") is not None:
            return None
        target_id = summary.get("update_memory_id")
        memory_id = target_id if isinstance(target_id, str) else request.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id:
            return None
        body = summary.get("body")
        title = summary.get("title")
        scopes = summary.get("scopes")
        if not isinstance(body, str) or not isinstance(title, str) or not isinstance(scopes, list):
            return None
        value: dict[str, Any] = {
            "memory_id": memory_id,
            "title": title,
            "body": body,
            "tags": list(summary.get("tags", [])) if isinstance(summary.get("tags", []), list) else [],
            "type": summary.get("type", "other"),
            "scopes": list(scopes),
            "aliases": list(summary.get("aliases", [])) if isinstance(summary.get("aliases", []), list) else [],
            "keywords": list(summary.get("keywords", [])) if isinstance(summary.get("keywords", []), list) else [],
            "status": summary.get("status"),
            "completed_at": summary.get("completed_at"),
            "due_date": summary.get("due_date"),
        }
        return value

    def _todo_witnesses(self, memory_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Project only current knowledge todo state for coverage witnesses."""

        witnesses: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for memory_id in memory_ids:
            if not isinstance(memory_id, str) or not memory_id:
                continue
            key = memory_id.casefold()
            if key in seen:
                continue
            seen.add(key)
            memory = self.inputs._active_memory_by_id(memory_id)
            if memory is None or memory.type != "todo":
                continue
            witnesses[memory.memory_id] = {
                "type": "todo",
                "status": memory.status,
            }
        return witnesses


    def _request(
        self,
        summary: Mapping[str, Any],
        turn: InboxTurn,
        *,
        candidate_id: str,
        conversation_title: str,
        event_key_value: Optional[str] = None,
        turn_id: str = "",
        explicit_remember: bool = False,
        native_refs: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        evidence = summary.get("evidence_event_ids", [])
        if not isinstance(evidence, list) or not evidence:
            evidence = list(turn.event_keys)
        memory_id = MemoryWriter.deterministic_memory_id(
            source=turn.source,
            session_id=turn.session_id,
            turn_key=turn.turn_key or turn_key(turn_id or candidate_id),
            candidate_id=candidate_id,
            evidence_event_ids=evidence,
        )
        return {
            "summary": dict(summary),
            "turn": turn,
            "candidate_id": candidate_id,
            "memory_id": memory_id,
            "event_key": event_key_value or (turn.event_keys[0] if turn.event_keys else ""),
            "turn_id": turn_id,
            "conversation_title": conversation_title,
            "explicit_remember": explicit_remember,
            "native_refs": [dict(item) for item in native_refs if isinstance(item, Mapping)],
        }


    def _duplicate_request(
        self,
        candidate: Mapping[str, Any],
        turn: InboxTurn,
        *,
        conversation_title: str,
        native_refs: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Turn a validated duplicate candidate into a metadata-only write."""

        duplicate_id = candidate.get("duplicate_memory_id")
        if not isinstance(duplicate_id, str) or not duplicate_id:
            raise ProcessingError("invalid duplicate memory target")
        summary = {
            "title": "",
            "body": "",
            "tags": [],
            "type": candidate.get("type"),
            "scopes": list(candidate.get("scopes", [])),
            "scope_source": candidate.get("scope_source"),
            "sources": [],
            "scope_operations": [],
        }
        return {
            "summary": summary,
            "turn": turn,
            "candidate_id": str(candidate["candidate_id"]),
            "memory_id": duplicate_id,
            "duplicate_memory_id": duplicate_id,
            "event_key": turn.event_keys[0] if turn.event_keys else "",
            "turn_id": "",
            "conversation_title": conversation_title,
            "explicit_remember": False,
            "native_refs": [dict(item) for item in native_refs if isinstance(item, Mapping)],
        }


    def _collect_turn_outputs(
        self,
        backend: Any,
        turn: InboxTurn,
        state: Mapping[str, Any],
        *,
        explicit: bool = False,
        explicit_candidate: Optional[Mapping[str, Any]] = None,
        scope: Any = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        authorized_project_scopes = _explicit_project_scope_authorizations(scope)
        events = _event_payload(turn)
        # Capture policy also applies to unprocessed legacy inbox evidence.
        # Keep the immutable turn untouched for input-digest/replay validation.
        # Explicit remember supplies its own visible user text; it does not
        # revive previously excluded tool observations.
        policy_config = self.service.vault.config()
        for event in events:
            event["tool_evidence"] = retain_tool_evidence(event["tool_evidence"], policy_config)
        evidence_units = analyze_turn_evidence(events)
        evidence_partition = partition_evidence_units(evidence_units)
        retry_ledger = (
            self._retry_ledger(state, turn)
            if not explicit
            else {
                "same_turn": False,
                "settled_unit_ids": set(),
                "settled_source_ids": set(),
                "settled_rows_by_unit": {},
                "settled_rows_by_source": {},
                "settled_memory_by_source": {},
                "current_candidates": [],
                "current_deferred": [],
            }
        )
        settled_unit_ids = set(retry_ledger["settled_unit_ids"])
        settled_source_ids = set(retry_ledger["settled_source_ids"])
        # Multiple snapshots from one process call are planned before the
        # shared commit boundary.  ``Processor.process`` clears this set at
        # the start of each process call; source/session are also part of each
        # key so a cache entry cannot cross session boundaries.
        planned_settled_sources = getattr(self.audit, "_planned_settled_sources", None)
        if not isinstance(planned_settled_sources, set):
            planned_settled_sources = set()
            self.audit._planned_settled_sources = planned_settled_sources
        planned_source_ids = {
            value[2]
            for value in planned_settled_sources
            if isinstance(value, tuple)
            and len(value) == 3
            and value[0] == turn.source
            and value[1] == turn.session_id
            and isinstance(value[2], str)
        }
        settled_source_ids.update(planned_source_ids)
        source_identity_by_unit = {
            unit.unit_id: self._evidence_source_identity(
                unit, source=turn.source, session_id=turn.session_id
            )
            for unit in evidence_partition.physical
        }
        planning_evidence_units = tuple(
            unit
            for unit in evidence_partition.physical
            if unit.unit_id not in settled_unit_ids
            and source_identity_by_unit[unit.unit_id] not in settled_source_ids
        )
        # Keep the complete retained event inventory for replay/audit and for
        # exact turn digests.  Gate receives conversation text for context, but
        # tool record identities are deliberately projected out because the
        # retained bodies already appear in physical evidence units.
        gate_events = []
        for event in events:
            projected = dict(event)
            projected.pop("tool_evidence", None)
            gate_events.append(projected)
        model_evidence_units = planning_evidence_units
        coverage_rows: dict[str, dict[str, Any]] = {}
        turn_ref = (turn.source, turn.session_id, turn.turn_key)
        if retry_ledger.get("same_turn"):
            self.audit._dispositions_by_turn[turn_ref] = deepcopy(
                retry_ledger.get("current_candidates", [])
            )
            self.audit._deferred_by_turn[turn_ref] = deepcopy(
                retry_ledger.get("current_deferred", [])
            )
        else:
            self.audit._deferred_by_turn.setdefault(turn_ref, [])
        related, scope_background, native_refs, scope_fallback = self.inputs._related(
            turn,
            state,
            scope,
            overlay=self.audit._planned_related,
            physical_units=model_evidence_units,
        )
        # When a compressed turn has no lexical hit but inherits one concrete
        # scope containing several active memories, expose only a bounded
        # metadata directory to the gate.  Bodies stay out of this first
        # decision; the selected ID is re-read for summarize below.
        scope_directory: Optional[list[dict[str, Any]]] = None
        scope_directory_complete = True
        scope_ambiguous = False
        scoped_records: Optional[list[Any]] = None
        gate_related = related
        related_native_ids = [item["native_id"] for item in native_refs]
        related_memory_ids = [
            item["memory_id"]
            for item in related
            if item.get("native") is not True and isinstance(item.get("memory_id"), str)
        ]
        gate_related_memory_ids = list(related_memory_ids)
        if scope_fallback is not None:
            scoped_records, scope_ambiguous = scope_fallback
            if scope_ambiguous and self.inputs._has_specific_scope(scope_background):
                scope_directory, scope_directory_complete = self.inputs._scope_directory(scoped_records)
                gate_related = []
                for entry in scope_directory:
                    memory_id = entry.get("memory_id")
                    if isinstance(memory_id, str) and memory_id.casefold() not in {
                        value.casefold() for value in gate_related_memory_ids
                    }:
                        gate_related_memory_ids.append(memory_id)
        todo_witnesses = self._todo_witnesses(gate_related_memory_ids)
        scope_registry = self.inputs._scope_registry_projection()
        with self.service.vault.lock():
            validation_scope_registry = self.service.vault.config().get("scopes", {})
        title = self.inputs._conversation_title(turn)
        if explicit:
            candidate = dict(explicit_candidate or {})
            target_revisions = {}
            for memory_id in related_memory_ids:
                memory = self.inputs._active_memory_by_id(memory_id)
                if memory is not None:
                    target_revisions[memory_id] = revision_digest(memory)
            summary = self.model._complete_json_stage(
                backend,
                summarize_prompt(
                    candidate,
                    events,
                    explicit=True,
                    related_memories=related,
                    scope_background=scope_background,
                    scope_registry=scope_registry,
                ),
                system=SUMMARIZE_SYSTEM,
                purpose="summarize",
                parser=lambda raw: parse_summarize_output(
                    _normalize_summary_dates(raw, turn, candidate),
                    current_event_keys=turn.event_keys,
                    related_native_ids=related_native_ids,
                    related_memory_ids=related_memory_ids,
                    scope_registry=validation_scope_registry,
                    expected_scopes=candidate["scopes"] if scope is not None else None,
                    expected_scope_source="user" if scope is not None else None,
                    allowed_due_dates=_grounded_due_dates(turn),
                    allow_no_change=False,
                ),
                diagnostic_context={
                    "source": turn.source,
                    "session_id": turn.session_id,
                    "turn_index": turn.turn_index,
                },
            )
            request = self._request(
                summary, turn, candidate_id=str(candidate["candidate_id"]),
                conversation_title=title, explicit_remember=True, native_refs=native_refs,
            )
            target = summary.get("update_memory_id")
            if target:
                expected = target_revisions.get(target)
                if not expected:
                    raise ProcessingError("explicit update target has no planning revision")
                request["expected_revision"] = expected
            return [request], list(summary["scopes"])

        target_relations: dict[str, str] = {}
        unknown_target_ids: set[str] = set()
        candidate_level_target_ids: set[str] = set()
        scope_correction_plans: dict[str, dict[str, Any]] = {}

        def parse_gate(
            raw: str,
            batch_units: tuple[Any, ...],
            batch_state: dict[str, Any],
        ) -> dict[str, Any]:
            batch_state["attempt_count"] = batch_state.get("attempt_count", 0) + 1
            gate_attempt_count = batch_state["attempt_count"]
            coverage_rows: dict[str, dict[str, Any]] = {}
            batch_target_relations: dict[str, str] = batch_state["target_relations"]
            batch_unknown_target_ids: set[str] = batch_state["unknown_target_ids"]
            batch_candidate_level_target_ids: set[str] = batch_state["candidate_level_target_ids"]
            batch_scope_correction_plans: dict[str, dict[str, Any]] = batch_state["scope_correction_plans"]
            batch_target_relations.clear()
            batch_unknown_target_ids.clear()
            batch_candidate_level_target_ids.clear()
            batch_scope_correction_plans.clear()
            raw, binding_value = split_semantic_envelope(raw)
            raw, coverage_value = split_gate_envelope(raw)
            raw_for_parse = raw
            # An explicit scope correction can legitimately name an old-scope
            # target absent from the ordinary new-scope directory. Authorize
            # that one candidate/target pair before strict ID validation, not
            # every cross-project reference appearing in the same batch.
            allowed_corrections: dict[str, str] = {}
            ordinary_ids = {value.casefold() for value in gate_related_memory_ids}
            envelope = parse_strict_json(raw_for_parse)
            raw_candidates = envelope.get("candidates") if isinstance(envelope, dict) else None
            if isinstance(raw_candidates, list):
                user_keys = {event.event_key for event in turn.events if event.role == "user"}
                for raw_candidate in raw_candidates:
                    if not isinstance(raw_candidate, dict):
                        continue
                    cid = raw_candidate.get("candidate_id")
                    target_id = raw_candidate.get("update_memory_id")
                    evidence = raw_candidate.get("evidence_event_ids")
                    if (not isinstance(cid, str) or not isinstance(target_id, str)
                        or target_id.casefold() in ordinary_ids
                        or not isinstance(raw_candidate.get("memory"), str)
                        or not isinstance(raw_candidate.get("scopes"), list)
                        or not isinstance(evidence, list)
                        or not any(isinstance(key, str) and key in user_keys for key in evidence)):
                        continue
                    correction = self.inputs._scope_correction_plan(raw_candidate, turn, validation_scope_registry)
                    if (correction and not correction.get("ambiguous")
                        and correction.get("target_memory_id") == target_id):
                        allowed_corrections[cid.casefold()] = target_id
            parsed = parse_gate_output(
                raw_for_parse,
                current_event_keys=turn.event_keys,
                related_memory_ids=[*gate_related_memory_ids, *allowed_corrections.values()],
                scope_registry=validation_scope_registry,
                defer_semantic_errors=gate_attempt_count >= 3,
                allow_shared_update_targets=True,
                allow_omitted_evidence_event_ids=True,
            )
            for item in parsed["candidates"]:
                for field in ("duplicate_memory_id", "update_memory_id"):
                    target_id = item.get(field)
                    if isinstance(target_id, str) and target_id.casefold() not in ordinary_ids:
                        if (field != "update_memory_id"
                            or allowed_corrections.get(item["candidate_id"].casefold()) != target_id):
                            raise ModelOutputError("target is not authorized for this candidate",
                                                   validation_detail="invalid_update_target")
            coverage_rows = (parse_coverage(coverage_value, batch_units, parsed["candidates"], require_complete=False,
                                            todo_witnesses=todo_witnesses)
                             if coverage_value is not None else {})
            if binding_value is not None:
                bindings = validate_bindings(binding_value, batch_units, parsed["candidates"])
                for item in parsed["candidates"]:
                    if item["candidate_id"] in bindings:
                        claims = bindings[item["candidate_id"]]
                        for claim in claims:
                            row = coverage_rows.get(claim["unit_id"])
                            if row is not None and (row["decision"] != "CANDIDATE"
                                or item["candidate_id"] not in row["candidate_ids"]):
                                raise ModelOutputError("binding contradicts coverage", validation_detail="invalid_evidence",
                                                       evidence_check="coverage_binding_conflict")
                        item["_evidence_bindings"] = claims
            resolve_omitted_candidate_event_ids(
                parsed["candidates"],
                bindings if binding_value is not None else {},
                batch_units,
            )
            validate_coverage_bindings(coverage_rows, batch_units, parsed["candidates"])

            # Freeze legacy exact-text support at the batch boundary.  Without
            # this marker, a candidate emitted by an earlier Gate call could
            # be re-matched against an identical unit from a later batch when
            # final admission scans the complete turn.  Explicit bindings are
            # already validated against this batch; recording their unit IDs
            # keeps the same source boundary for audit and summary projection.
            for item in parsed["candidates"]:
                supporting = supporting_units(item, batch_units)
                item["_evidence_unit_ids"] = [unit.unit_id for unit in supporting]
                if (
                    supporting
                    and
                    item.get("_defer_reason") is None
                    and not _model_project_scope_is_source_grounded(
                        item,
                        batch_units,
                        validation_scope_registry,
                        authorized_project_scopes,
                    )
                ):
                    if gate_attempt_count < 3:
                        raise ModelOutputError(
                            "model project scope is not grounded by this candidate's bound source",
                            validation_detail="scope_not_grounded",
                        )
                    # Keep the candidate auditable and let unrelated siblings
                    # continue through admission after the bounded retries.
                    item["_defer_reason"] = "scope_conflict"

            prepared_candidates: list[dict[str, Any]] = []
            for candidate in parsed["candidates"]:
                item = dict(candidate)
                # Exact candidate-bound grounding was already validated above.
                # Do not reinterpret a second mentioned project/product/platform name
                # as contradictory ownership with a registry-only name scan.
                plan = self.inputs._scope_correction_plan(item, turn, validation_scope_registry)
                if plan is not None:
                    item.pop("duplicate_memory_id", None)
                    item["duplicate"] = False
                    if isinstance(plan.get("target_memory_id"), str):
                        item["update_memory_id"] = plan["target_memory_id"]
                    else:
                        item.pop("update_memory_id", None)
                    batch_scope_correction_plans[str(item["candidate_id"]).casefold()] = plan
                prepared_candidates.append(item)
            parsed = dict(parsed)
            parsed["candidates"] = prepared_candidates

            invalid_targets: dict[str, set[str]] = {}
            type_mismatches: set[str] = set()
            for candidate in parsed["candidates"]:
                target_fields = {
                    field
                    for field in ("duplicate_memory_id", "update_memory_id")
                    if isinstance(candidate.get(field), str) and candidate.get(field)
                }
                if not target_fields:
                    continue
                candidate_id = candidate["candidate_id"].casefold()
                correction = batch_scope_correction_plans.get(candidate_id)
                relation = (
                    _TARGET_SAME_USE
                    if correction is not None and not correction.get("ambiguous")
                    else self.inputs._target_relation(
                        candidate,
                        turn=turn,
                        scope_directory=scope_directory,
                        scope_directory_complete=scope_directory_complete,
                    )
                )
                batch_target_relations[candidate_id] = relation
                if relation == _TARGET_NOT_RELATED:
                    invalid_targets[candidate_id] = target_fields
                elif relation == _TARGET_UNKNOWN:
                    batch_unknown_target_ids.add(candidate_id)
                if "update_memory_id" in target_fields:
                    target = self.inputs._active_memory_by_id(candidate["update_memory_id"])
                    if target is None:
                        batch_unknown_target_ids.add(candidate_id)
                    elif target.type != candidate.get("type"):
                        type_mismatches.add(candidate_id)

            if invalid_targets and gate_attempt_count < 3:
                raise ModelOutputError(
                    "selected target is not relevant to the candidate topic",
                    validation_detail="target_not_relevant",
                )

            # Check topic relevance before surfacing a type mismatch.  This
            # ordering gives the model the most useful correction first.
            # After bounded correction, reject only the invalid candidate.
            if type_mismatches and gate_attempt_count < 3:
                raise ModelOutputError(
                    "candidate type does not match update target",
                    validation_detail="update_target_type_mismatch",
                )

            if invalid_targets or type_mismatches:
                # Validation may reject a model proposal, not change UPDATE
                # into CREATE or erase a duplicate decision. A persistently
                # invalid target is retained for review; valid siblings proceed.
                candidates = []
                for candidate in parsed["candidates"]:
                    item = dict(candidate)
                    cid = item["candidate_id"].casefold()
                    if cid not in batch_scope_correction_plans:
                        if cid in type_mismatches:
                            item["_defer_reason"] = "update_target_type_mismatch"
                        elif cid in invalid_targets:
                            item["_defer_reason"] = "target_not_relevant"
                    candidates.append(item)
                parsed = {**parsed, "candidates": candidates}
            batch_state["coverage_rows"] = coverage_rows
            return parsed

        def _namespace_batch(
            parsed_gate: dict[str, Any],
            batch_state: dict[str, Any],
            batch_index: int,
            batch_count: int,
        ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, str], set[str], set[str], dict[str, dict[str, Any]]]:
            """Keep candidate/audit identities unique across independent Gate calls."""

            batch_prefix = f"b{batch_index}-" if batch_count > 1 else ""
            prefix = retry_candidate_prefix + batch_prefix
            id_map = {
                str(item["candidate_id"]): prefix + str(item["candidate_id"])
                for item in parsed_gate["candidates"]
            }
            candidates = []
            for item in parsed_gate["candidates"]:
                value = dict(item)
                value["_model_candidate_id"] = str(item["candidate_id"])
                value["candidate_id"] = id_map[str(item["candidate_id"])]
                value["_gate_batch_index"] = batch_index
                candidates.append(value)
            coverage: dict[str, dict[str, Any]] = {}
            for unit_id, row in batch_state.get("coverage_rows", {}).items():
                value = dict(row)
                value["candidate_ids"] = [
                    id_map.get(str(candidate_id), str(candidate_id))
                    for candidate_id in row.get("candidate_ids", [])
                ]
                coverage[unit_id] = value
            relations = {
                (prefix + candidate_id if prefix else candidate_id): relation
                for candidate_id, relation in batch_state["target_relations"].items()
            }
            unknown = {
                prefix + candidate_id if prefix else candidate_id
                for candidate_id in batch_state["unknown_target_ids"]
            }
            candidate_level = {
                prefix + candidate_id if prefix else candidate_id
                for candidate_id in batch_state["candidate_level_target_ids"]
            }
            corrections = {
                (prefix + candidate_id if prefix else candidate_id): value
                for candidate_id, value in batch_state["scope_correction_plans"].items()
            }
            return ({"candidates": candidates}, coverage, relations, unknown, candidate_level, corrections)

        # A repeated turn can have no unresolved physical units left.  There
        # is no semantic work for the model in that case; the ledger rows below
        # still record the no-change reuse for this turn.
        gate_batches = gate_evidence_batches(model_evidence_units) if model_evidence_units else ()
        batch_count = len(gate_batches)
        retry_candidate_prefix = ""
        if retry_ledger.get("same_turn") and model_evidence_units:
            pending_digest = hashlib.sha256(
                json.dumps(
                    [unit.unit_id for unit in model_evidence_units],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:12]
            retry_candidate_prefix = f"retry-{pending_digest}-"
        all_candidates: list[dict[str, Any]] = []
        for batch_index, batch_units in enumerate(gate_batches):
            batch_state: dict[str, Any] = {
                "attempt_count": 0,
                "coverage_rows": {},
                "target_relations": {},
                "unknown_target_ids": set(),
                "candidate_level_target_ids": set(),
                "scope_correction_plans": {},
            }
            batch_gate = self.model._complete_json_stage(
                backend,
                    gate_prompt(
                    gate_events,
                    related_memories=gate_related,
                    scope_directory=scope_directory,
                    scope_directory_complete=scope_directory_complete,
                    scope_background=scope_background,
                    scope_registry=scope_registry,
                ) + evidence_prompt(batch_units, batch_index=batch_index, batch_count=batch_count,
                                     todo_witnesses=todo_witnesses),
                system=GATE_SYSTEM,
                purpose="gate",
                parser=lambda raw, units=batch_units, state=batch_state: parse_gate(raw, units, state),
                diagnostic_context={
                    "source": turn.source,
                    "session_id": turn.session_id,
                    "turn_index": turn.turn_index,
                },
            )
            # One bounded, source-neutral coverage repair per batch. This is
            # NOT a writer: every returned candidate is parsed again by the
            # same Gate boundary. A failed repair leaves only validated
            # siblings from this batch, while any hard model failure aborts the
            # complete turn before admission or commit.
            accounted = set(batch_state.get("coverage_rows", {}))
            for initial in batch_gate["candidates"]:
                accounted.update(unit.unit_id for unit in supporting_units(initial, batch_units))
            missing = tuple(unit for unit in batch_units if unit.unit_id not in accounted)
            if missing:
                saved_gate = deepcopy(batch_gate)
                saved_coverage = deepcopy(batch_state.get("coverage_rows", {}))
                saved_maps = [deepcopy(batch_state[key]) for key in (
                    "target_relations", "unknown_target_ids", "candidate_level_target_ids",
                    "scope_correction_plans")]
                correction_metric_context: dict[str, Any] = {}
                try:
                    correction_raw = self.model._complete(
                        backend,
                        coverage_repair_prompt(
                            evidence_prompt(
                                missing,
                                batch_index=batch_index,
                                batch_count=batch_count,
                                todo_witnesses=todo_witnesses,
                            ),
                            related_memories=gate_related,
                            scope_background=scope_background,
                            scope_registry=scope_registry,
                            already_handled_candidate_ids=[
                                item["candidate_id"] for item in batch_gate["candidates"]
                            ],
                        ),
                        system=GATE_COVERAGE_SYSTEM,
                        purpose="gate",
                        metric_stage="gate",
                        metric_operation="gate_coverage_repair",
                        metric_context=correction_metric_context,
                    )
                    correction_raw, correction_bindings = split_semantic_envelope(correction_raw)
                    correction_raw, correction_coverage = split_gate_envelope(correction_raw)
                    correction_gate = parse_gate_output(correction_raw,
                        current_event_keys=tuple(dict.fromkeys(unit.event_key for unit in missing)),
                        related_memory_ids=gate_related_memory_ids, scope_registry=validation_scope_registry,
                        allow_shared_update_targets=True,
                        allow_omitted_evidence_event_ids=True)
                    new_ids = {item["candidate_id"] for item in correction_gate["candidates"]}
                    if new_ids.intersection(item["candidate_id"] for item in batch_gate["candidates"]):
                        raise ModelOutputError("coverage correction reused a candidate id", validation_detail="duplicate_candidate_id")
                    correction_binding_map = (
                        validate_bindings(correction_bindings, missing, correction_gate["candidates"])
                        if correction_bindings is not None else {}
                    )
                    resolve_omitted_candidate_event_ids(
                        correction_gate["candidates"],
                        correction_binding_map,
                        missing,
                    )
                    new_coverage = (parse_coverage(correction_coverage, missing, correction_gate["candidates"],
                                    require_complete=False, todo_witnesses=todo_witnesses)
                                    if correction_coverage is not None else {})
                    public_candidates = [{key: value for key, value in item.items()
                        if not key.startswith("_") and key != "evidence_unit_ids"} for item in batch_gate["candidates"]]
                    old_bindings = [{"candidate_id": item["candidate_id"], "claims": item["_evidence_bindings"]}
                        for item in batch_gate["candidates"] if item.get("_evidence_bindings")]
                    merged = {"candidates": public_candidates + correction_gate["candidates"],
                              "coverage": list(saved_coverage.values()) + list(new_coverage.values()),
                              "evidence_bindings": old_bindings + (correction_bindings or [])}
                    batch_gate = parse_gate(json.dumps(merged, ensure_ascii=False), batch_units, batch_state)
                except (ModelError, ModelOutputError) as error:
                    if isinstance(error, ModelOutputError):
                        self.model._record_invalid_output(correction_metric_context)
                    # A failed correction cannot invalidate already validated
                    # siblings, but the unresolved units remain retryable.
                    batch_gate = saved_gate
                    batch_state["coverage_rows"] = saved_coverage
                    for key, old in zip(("target_relations", "unknown_target_ids",
                        "candidate_level_target_ids", "scope_correction_plans"), saved_maps):
                        batch_state[key].clear()
                        batch_state[key].update(old)

            (namespaced_gate, batch_coverage, batch_relations, batch_unknown,
             batch_candidate_level, batch_corrections) = _namespace_batch(
                batch_gate, batch_state, batch_index, batch_count)
            all_candidates.extend(namespaced_gate["candidates"])
            coverage_rows.update(batch_coverage)
            target_relations.update(batch_relations)
            unknown_target_ids.update(batch_unknown)
            candidate_level_target_ids.update(batch_candidate_level)
            scope_correction_plans.update(batch_corrections)
        gate = {"candidates": all_candidates}

        requests: list[dict[str, Any]] = []
        observed_scopes: list[str] = []
        current_turn_request_ids: set[str] = set()
        read_only_query = read_only_turn(evidence_units)
        covered_unit_ids: set[str] = set()
        covered_by_unit: dict[str, list[str]] = {}
        seen_candidates: set[tuple[Any, ...]] = set()
        seen_duplicate_targets: set[str] = set()
        admitted_candidates: dict[str, dict[str, Any]] = {}
        summary_jobs: list[dict[str, Any]] = []
        request_slots: list[dict[str, Any]] = []

        def observe_scopes(values: Iterable[Any]) -> None:
            for observed_scope in values:
                if (
                    isinstance(observed_scope, str)
                    and observed_scope != "unscoped"
                    and observed_scope not in observed_scopes
                ):
                    observed_scopes.append(observed_scope)

        for candidate in gate["candidates"]:
            candidate = dict(candidate)
            duplicate_target = candidate.get("duplicate_memory_id")
            if isinstance(duplicate_target, str) and duplicate_target:
                duplicate_key = duplicate_target.casefold()
                if duplicate_key in seen_duplicate_targets:
                    self.audit._record_disposition(
                        turn_ref,
                        candidate,
                        "NO_CHANGE",
                        reason="same_target_duplicate",
                        memory_id=duplicate_target,
                    )
                    continue
                seen_duplicate_targets.add(duplicate_key)
            unit_ids = [uid for uid, row in coverage_rows.items()
                        if candidate.get("candidate_id") in row.get("candidate_ids", [])]
            if unit_ids and "_evidence_unit_ids" not in candidate:
                candidate["_evidence_unit_ids"] = unit_ids
            reason, support = admission_reason(candidate, planning_evidence_units)
            candidate["evidence_unit_ids"] = [u.unit_id for u in support]
            covered_unit_ids.update(candidate["evidence_unit_ids"])
            for uid in candidate["evidence_unit_ids"]:
                covered_by_unit.setdefault(uid, []).append(candidate["candidate_id"])
            if reason is not None and candidate.get("worth"):
                if reason in {"read_only_query", "quoted_or_example"}:
                    self.audit._record_disposition(turn_ref, candidate, "NO_CHANGE", reason=reason)
                else:
                    self.audit._defer_candidate(turn_ref, candidate, reason, scopes=candidate.get("scopes", []))
                continue
            fingerprint = (str(candidate.get("memory", "")).strip(),
                           candidate.get("type"), tuple(sorted(candidate.get("scopes", []))),
                           candidate.get("update_memory_id"), candidate.get("duplicate_memory_id"))
            if fingerprint in seen_candidates:
                self.audit._record_disposition(turn_ref, candidate, "NO_CHANGE", reason="same_turn_duplicate")
                continue
            seen_candidates.add(fingerprint)
            # Exact candidate-bound model project grounding was already
            # validated by parse_gate. Do not run a registry-name conflict scan
            # again here; semantic ownership/implementation roles were reviewed
            # by the model boundary above.
            candidate_id_key = str(candidate.get("candidate_id", "")).casefold()
            correction_plan = scope_correction_plans.get(candidate_id_key)
            defer_reason = candidate.get("_defer_reason")
            # A pure existing-memory query must not leave even a deferred
            # candidate behind when the model mislabels its recap.
            if read_only_query and not candidate.get("_evidence_bindings"):
                self.audit._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="read_only_query",
                )
                continue
            if isinstance(defer_reason, str) and defer_reason in {"mixed_future_use", "scope_conflict"}:
                self.audit._defer_candidate(
                    turn_ref,
                    candidate,
                    defer_reason,
                    scopes=candidate.get("scopes"),
                )
                continue
            if correction_plan is not None and correction_plan.get("ambiguous"):
                self.audit._defer_candidate(
                    turn_ref,
                    candidate,
                    (
                        "scope_correction_unresolved"
                        if correction_plan.get("unresolved")
                        else "scope_correction_ambiguous"
                    ),
                    scopes=candidate.get("scopes"),
                )
                continue
            candidate_scopes = list(candidate["scopes"])
            # An automatic candidate with no reliable project attribution is
            # retained as a retryable inbox turn, never silently promoted to
            # global knowledge.  The processed ledger records only a compact
            # marker; the complete candidate remains in the inbox.
            if candidate["worth"] and (
                candidate_scopes == ["unscoped"]
                or candidate.get("scope_source") == "insufficient_context"
            ):
                self.audit._defer_candidate(turn_ref, candidate, "scope_required", scopes=candidate_scopes)
                continue

            has_target = any(
                isinstance(candidate.get(field), str) and candidate.get(field)
                for field in ("duplicate_memory_id", "update_memory_id")
            )
            if (
                scope_directory is not None
                and not scope_directory_complete
                and correction_plan is None
            ):
                # An incomplete metadata directory cannot safely authorize a
                # new target or prove that no duplicate exists.  However, an
                # explicit model-selected target may still proceed when the
                # same source-neutral target relation check already verified
                # it from the bounded related-memory context.
                target_relation = target_relations.get(candidate_id_key)
                if (not has_target and candidate["worth"]) or (
                    has_target and target_relation != _TARGET_SAME_USE
                ):
                    self.audit._defer_candidate(
                        turn_ref,
                        candidate,
                        "scope_directory_incomplete",
                        scopes=candidate_scopes,
                    )
                    continue

            if (
                candidate["worth"]
                and scope_ambiguous
                and not has_target
                and correction_plan is None
            ):
                self.audit._defer_candidate(
                    turn_ref,
                    candidate,
                    "related_ambiguous",
                    scopes=candidate_scopes,
                )
                continue

            if candidate_id_key in unknown_target_ids:
                self.audit._defer_candidate(
                    turn_ref,
                    candidate,
                    "target_unknown",
                    scopes=candidate_scopes,
                )
                continue

            candidate_related, candidate_scope_background, candidate_native_refs, _ = self.inputs._related_query(
                turn,
                state,
                _candidate_lookup_queries(candidate.get("memory")),
                candidate_scopes,
                overlay=self.audit._planned_related,
                priority_memory_ids=[
                    candidate.get("duplicate_memory_id"),
                    candidate.get("update_memory_id"),
                ],
                priority_only=(scope_directory is not None),
                scope_records=(
                    scoped_records
                    if scope_directory is not None
                    else None
                ),
            )
            if correction_plan is not None:
                priority_ids = [
                    correction_plan.get("target_memory_id"),
                    correction_plan.get("survivor_memory_id"),
                ]
                for priority_id in reversed(priority_ids):
                    memory = self.inputs._active_memory_by_id(priority_id)
                    if memory is None:
                        continue
                    if not any(
                        isinstance(item, Mapping)
                        and isinstance(item.get("memory_id"), str)
                        and item["memory_id"].casefold() == memory.memory_id.casefold()
                        for item in candidate_related
                    ):
                        candidate_related.insert(0, memory.to_dict())
            if correction_plan is None:
                candidate = self.inputs._infer_update_target(candidate, candidate_related)
                # Candidate-specific retrieval can discover targets absent from
                # the initial Gate. Reopen that decision with current local
                # records before a targetless proposal reaches summarize.
                initial_ids = {value.casefold() for value in gate_related_memory_ids}
                active_related = []
                for item in candidate_related:
                    if not isinstance(item, Mapping) or item.get("native") is True:
                        continue
                    active = self.inputs._active_memory_by_id(item.get("memory_id"))
                    if active is not None:
                        active_related.append(active.to_dict())
                if any(item["memory_id"].casefold() not in initial_ids for item in active_related):
                    candidate = reconcile_candidate_target(
                        self.model,
                        backend,
                        candidate,
                        related_memories=active_related,
                        validated_bindings=candidate.get("_evidence_bindings"),
                        summary_evidence=summary_evidence(candidate, planning_evidence_units, events=events),
                        diagnostic_context={
                            "source": turn.source,
                            "session_id": turn.session_id,
                            "turn_index": turn.turn_index,
                        },
                    )
            defer_reason = candidate.pop("_defer_reason", None)
            if defer_reason:
                self.audit._defer_candidate(turn_ref, candidate, defer_reason)
                continue
            target_field = next(
                (
                    field
                    for field in ("duplicate_memory_id", "update_memory_id")
                    if isinstance(candidate.get(field), str) and candidate.get(field)
                ),
                None,
            )
            if target_field is not None:
                relation = (
                    _TARGET_SAME_USE
                    if correction_plan is not None and not correction_plan.get("ambiguous")
                    else self.inputs._target_relation(
                        candidate,
                        turn=turn,
                        scope_directory=(
                            scope_directory
                        ),
                        scope_directory_complete=scope_directory_complete,
                    )
                )
                if relation == _TARGET_UNKNOWN:
                    self.audit._defer_candidate(
                        turn_ref,
                        candidate,
                        "target_unknown",
                        scopes=candidate_scopes,
                    )
                    continue
                if relation == _TARGET_NOT_RELATED and not (
                    target_field == "duplicate_memory_id"
                    and candidate_id_key in candidate_level_target_ids
                ):
                    self.audit._defer_candidate(
                        turn_ref,
                        candidate,
                        "target_not_relevant",
                        scopes=candidate_scopes,
                    )
                    continue
                if target_field == "update_memory_id":
                    active_target = self.inputs._active_memory_by_id(candidate[target_field])
                    if active_target is None:
                        self.audit._defer_candidate(
                            turn_ref,
                            candidate,
                            "target_unknown",
                            scopes=candidate_scopes,
                        )
                        continue
                    if active_target.type != candidate.get("type"):
                        self.audit._defer_candidate(
                            turn_ref,
                            candidate,
                            "update_target_type_mismatch",
                            scopes=candidate_scopes,
                        )
                        continue
            if correction_plan is not None and correction_plan.get("survivor_memory_id"):
                request_slots.append({
                    "kind": "request",
                    "request": self.inputs._scope_correction_request(
                        candidate,
                        turn,
                        correction_plan,
                        conversation_title=title,
                        native_refs=candidate_native_refs,
                    ),
                    "scopes": list(candidate.get("scopes", [])),
                })
                continue
            candidate_native_ids = [item["native_id"] for item in candidate_native_refs]
            all_candidate_memory_ids: list[str] = []
            same_type_update_memory_ids: list[str] = []
            for item in candidate_related:
                if item.get("native") is True or not isinstance(item.get("memory_id"), str):
                    continue
                active_memory = self.inputs._active_memory_by_id(item["memory_id"])
                if active_memory is None:
                    continue
                all_candidate_memory_ids.append(active_memory.memory_id)
                if active_memory.type == candidate.get("type"):
                    same_type_update_memory_ids.append(active_memory.memory_id)
            all_candidate_id_set = {item.casefold() for item in all_candidate_memory_ids}
            same_type_update_id_set = {
                item.casefold() for item in same_type_update_memory_ids
            }
            duplicate_target = candidate.get("duplicate_memory_id")
            if duplicate_target is not None and (
                not isinstance(duplicate_target, str)
                or duplicate_target.casefold() not in all_candidate_id_set
            ):
                raise ModelOutputError(
                    "duplicate_memory_id is not a related active memory for this candidate",
                    validation_detail="invalid_duplicate_target",
                )
            update_target = candidate.get("update_memory_id")
            if update_target is not None and (
                not isinstance(update_target, str)
                or update_target.casefold() not in same_type_update_id_set
            ):
                raise ModelOutputError(
                    "update_memory_id is not a related active memory of the candidate type",
                    validation_detail="invalid_update_target",
                )

            if candidate["duplicate"] or not candidate["worth"]:
                duplicate_memory_id = candidate.get("duplicate_memory_id")
                if duplicate_memory_id is not None:
                    # Automatic duplicate observations are metadata no-ops;
                    # keep them out of the mutation batch entirely.  This is
                    # also required when another candidate updates the same
                    # active target in this turn: a no-op duplicate request
                    # would collide with that update during writer preflight.
                    # Only the already-active target's scopes are trustworthy
                    # session context, never a transient model-provided scope.
                    duplicate_scopes = next(
                        (
                            item.get("scopes")
                            for item in candidate_related
                            if isinstance(item, Mapping)
                            and isinstance(item.get("memory_id"), str)
                            and item["memory_id"].casefold() == duplicate_memory_id.casefold()
                            and isinstance(item.get("scopes"), list)
                        ),
                        [],
                    )
                    request_slots.append({"kind": "observe", "scopes": list(duplicate_scopes)})
                    self.audit._record_disposition(
                        turn_ref,
                        candidate,
                        "NO_CHANGE",
                        reason="duplicate",
                        memory_id=duplicate_memory_id,
                    )
                else:
                    self.audit._record_disposition(
                        turn_ref,
                        candidate,
                        "NO_CHANGE",
                        reason="not_worthy",
                    )
                continue

            gate_update_target = candidate.get("update_memory_id")
            gate_target_type = None
            if isinstance(gate_update_target, str):
                gate_target_key = gate_update_target.casefold()
                gate_target_type = next(
                    (
                        item.get("type")
                        for item in candidate_related
                        if isinstance(item, Mapping)
                        and isinstance(item.get("memory_id"), str)
                        and item["memory_id"].casefold() == gate_target_key
                        and isinstance(item.get("type"), str)
                    ),
                    None,
                )

            admitted_summary_events = summary_evidence(candidate, planning_evidence_units, events=events)
            admitted_summary_keys = tuple(dict.fromkeys(event["event_key"] for event in admitted_summary_events))
            grounded_summary_dates = _grounded_due_dates(turn, evidence_events=admitted_summary_events)
            summary_target = self.inputs._active_memory_by_id(gate_update_target) if gate_update_target else None

            def parse_summary(
                raw: str,
                *,
                candidate_value: Mapping[str, Any] = candidate,
                admitted_keys_value: tuple[str, ...] = admitted_summary_keys,
                native_ids_value: list[str] = candidate_native_ids,
                update_ids_value: list[str] = same_type_update_memory_ids,
                grounded_dates_value: set[str] = grounded_summary_dates,
                admitted_events_value: list[dict[str, Any]] = admitted_summary_events,
                summary_target_value: Any = summary_target,
                gate_update_target_value: Any = gate_update_target,
                gate_target_type_value: Any = gate_target_type,
            ) -> dict[str, Any]:
                parsed = parse_summarize_output(
                    _normalize_summary_dates(raw, turn, candidate_value),
                    current_event_keys=admitted_keys_value,
                    related_native_ids=native_ids_value,
                    related_memory_ids=update_ids_value,
                    scope_registry=validation_scope_registry,
                    expected_scopes=candidate_value["scopes"],
                    expected_scope_source=candidate_value["scope_source"],
                    allowed_due_dates=grounded_dates_value,
                    allow_no_change=True,
                    # The summarize stage may not reinterpret a gate
                    # candidate, including CREATE candidates. Updates
                    # additionally retain the active target's immutable
                    # type below.
                    expected_type=candidate_value.get("type"),
                    allow_update_target=gate_update_target_value is not None,
                    expected_update_memory_id=gate_update_target_value,
                    expected_target_type=gate_target_type_value,
                )
                if _summary_date_grounding_violations(
                    parsed,
                    grounded_dates=grounded_dates_value,
                    source_texts=[
                        event.get("content", "")
                        for event in admitted_events_value
                        if event.get("role") in {"user", "assistant"}
                    ],
                    preserved_texts=(
                        summary_target_value.title,
                        summary_target_value.body,
                        summary_target_value.due_date,
                    ) if summary_target_value else (),
                ):
                    raise ModelOutputError(
                        "summary contains a date absent from its admitted evidence",
                        validation_detail="relative_time",
                    )
                return parsed

            target_revisions = {}
            for related_item in candidate_related:
                if isinstance(related_item.get("memory_id"), str):
                    target_memory = self.inputs._active_memory_by_id(related_item["memory_id"])
                    if target_memory is not None:
                        target_revisions[target_memory.memory_id] = revision_digest(target_memory)
            summary_candidate = dict(candidate)
            model_candidate_id = summary_candidate.pop("_model_candidate_id", None)
            if isinstance(model_candidate_id, str) and model_candidate_id:
                summary_candidate["candidate_id"] = model_candidate_id
            summary_prompt_value = summarize_prompt(
                summary_candidate,
                admitted_summary_events,
                related_memories=candidate_related,
                scope_background=candidate_scope_background,
                scope_registry=scope_registry,
            )
            diagnostic_context = {
                "source": turn.source,
                "session_id": turn.session_id,
                "turn_index": turn.turn_index,
            }

            def run_summary(
                *,
                prompt_value: str = summary_prompt_value,
                parser_value: Any = parse_summary,
                diagnostic_value: Mapping[str, Any] = diagnostic_context,
            ) -> dict[str, Any]:
                try:
                    return {
                        "status": "ok",
                        "summary": self.model._complete_json_stage(
                            backend,
                            prompt_value,
                            system=SUMMARIZE_SYSTEM,
                            purpose="summarize",
                            parser=parser_value,
                            diagnostic_context=diagnostic_value,
                        ),
                    }
                except ModelOutputError as error:
                    if getattr(error, "validation_detail", None) not in {
                        "relative_time",
                        "due_date_not_grounded",
                    }:
                        raise
                    return {"status": "relative_time"}

            target_key = (
                f"update:{gate_update_target.casefold()}"
                if isinstance(gate_update_target, str) and gate_update_target
                else f"create:{str(candidate['candidate_id']).casefold()}"
            )
            job_index = len(summary_jobs)
            summary_jobs.append({
                "key": target_key,
                "call": run_summary,
                "candidate": dict(candidate),
                "candidate_related": candidate_related,
                "candidate_native_refs": candidate_native_refs,
                "correction_plan": correction_plan,
                "gate_update_target": gate_update_target,
                "target_revisions": target_revisions,
            })
            request_slots.append({"kind": "summary", "job_index": job_index})

        summary_outcomes = run_ordered_keyed_jobs(
            self.model,
            backend,
            [(job["key"], job["call"]) for job in summary_jobs],
        )

        for slot in request_slots:
            kind = slot.get("kind")
            if kind == "observe":
                observe_scopes(slot.get("scopes", []))
                continue
            if kind == "request":
                request = slot.get("request")
                if isinstance(request, dict):
                    requests.append(request)
                observe_scopes(slot.get("scopes", []))
                continue
            if kind != "summary":
                continue
            job_index = slot.get("job_index")
            if not isinstance(job_index, int) or isinstance(job_index, bool):
                raise ProcessingError("invalid prepared summary job")
            job = summary_jobs[job_index]
            outcome = summary_outcomes[job_index]
            candidate = job["candidate"]
            candidate_related = job["candidate_related"]
            candidate_native_refs = job["candidate_native_refs"]
            correction_plan = job["correction_plan"]
            gate_update_target = job["gate_update_target"]
            target_revisions = job["target_revisions"]
            if outcome.get("status") == "relative_time":
                # The candidate's source turn remains in inbox for an
                # explicit retry. Other candidates from this same turn may
                # still commit safely in the same transaction.
                self.audit._defer_candidate(
                    turn_ref,
                    candidate,
                    "relative_time",
                    scopes=candidate["scopes"],
                )
                continue
            summary = outcome.get("summary")
            if not isinstance(summary, Mapping):
                raise ProcessingError("invalid prepared summary result")
            summary = dict(summary)
            if summary.get("decision") == NO_CHANGE_DECISION:
                self.audit._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="summary_no_change",
                    memory_id=(
                        summary.get("update_memory_id")
                        if isinstance(summary.get("update_memory_id"), str)
                        else None
                    ),
                )
                continue
            if gate_update_target is not None:
                summary_update_target = summary.get("update_memory_id")
                if summary_update_target is None:
                    summary["update_memory_id"] = gate_update_target
                elif (
                    not isinstance(summary_update_target, str)
                    or summary_update_target.casefold() != gate_update_target.casefold()
                ):
                    raise ModelOutputError(
                        "summary update target differs from gate target",
                        validation_detail="invalid_update_target",
                    )
                else:
                    summary["update_memory_id"] = gate_update_target
                self.service.read(gate_update_target, include_history=False)
                # The summary is the complete model-proposed current value.
                # Do not concatenate old/new bodies using business keywords.
            if summary["scopes"] == ["unscoped"] or summary.get("scope_source") == "insufficient_context":
                self.audit._defer_candidate(
                    turn_ref,
                    candidate,
                    "scope_required",
                    scopes=summary["scopes"],
                    scope_source=summary.get("scope_source"),
                )
                continue
            pending_request = self._request(
                summary,
                turn,
                candidate_id=str(candidate["candidate_id"]),
                conversation_title=title,
                native_refs=candidate_native_refs,
            )
            if correction_plan is not None:
                pending_request["scope_correction"] = dict(correction_plan)
            gate_batch_index = candidate.get("_gate_batch_index")
            if isinstance(gate_batch_index, int) and not isinstance(gate_batch_index, bool):
                pending_request["_gate_batch_index"] = gate_batch_index
            current_turn_request_ids.add(pending_request["memory_id"].casefold())
            summary_update_target = summary.get("update_memory_id")
            final_is_create = not (
                isinstance(summary_update_target, str) and summary_update_target
            )
            if final_is_create and _automatic_create_conflicts(
                candidate,
                summary,
                candidate_related,
                ignore_memory_ids=current_turn_request_ids,
            ):
                self.audit._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="already_covered",
                )
                continue
            same_request = next((r for r in requests
                if r.get("summary", {}).get("update_memory_id") == summary.get("update_memory_id")
                and dedup_digest(r.get("summary", {})) == dedup_digest(summary)), None)
            if same_request is not None:
                self.audit._record_disposition(turn_ref, candidate, "NO_CHANGE", reason="same_turn_duplicate")
                continue
            pending_request["evidence_unit_ids"] = list(candidate.get("evidence_unit_ids", []))
            if summary_update_target:
                pending_request["expected_revision"] = target_revisions.get(summary_update_target)
            requests.append(pending_request)
            admitted_candidates[pending_request["candidate_id"]] = dict(candidate)
            self.audit._record_disposition(
                turn_ref,
                candidate,
                "UPDATE" if not final_is_create else "CREATE",
                memory_id=(
                    summary_update_target
                    if not final_is_create
                    else pending_request["memory_id"]
                ),
            )
            observe_scopes(summary["scopes"])

        requests = CreateCoordinator(self.model, self.audit).resolve(
            requests, candidates=admitted_candidates, evidence_units=planning_evidence_units, events=events,
            backend=backend, scope_registry=scope_registry, validation_scope_registry=validation_scope_registry)
        requests = UpdateCoordinator(self.model, self.audit, self.inputs._active_memory_by_id).resolve(
            requests, candidates=admitted_candidates, evidence_units=planning_evidence_units, events=events,
            backend=backend, scope_registry=scope_registry, validation_scope_registry=validation_scope_registry)
        evidence_dispositions = []
        for unit in evidence_units:
            source_identity = self._evidence_source_identity(
                unit, source=turn.source, session_id=turn.session_id
            )
            settled = (
                unit.unit_id in settled_unit_ids
                or source_identity in settled_source_ids
            )
            if settled:
                previous = retry_ledger["settled_rows_by_unit"].get(unit.unit_id)
                if previous is None:
                    previous = retry_ledger["settled_rows_by_source"].get(source_identity)
                if retry_ledger.get("same_turn") and isinstance(previous, Mapping):
                    evidence_disposition = dict(previous)
                    evidence_disposition["unit_id"] = unit.unit_id
                    evidence_disposition["event_key"] = unit.event_key
                    evidence_disposition["source_identity"] = source_identity
                else:
                    memory_id = retry_ledger["settled_memory_by_source"].get(source_identity)
                    if memory_id is None and isinstance(previous, Mapping):
                        value = previous.get("memory_id")
                        if isinstance(value, str) and value:
                            memory_id = value
                    evidence_disposition = {
                        "unit_id": unit.unit_id,
                        "event_key": unit.event_key,
                        "decision": "NO_CHANGE",
                        "reason": "already_processed",
                        "candidate_ids": [],
                        "source_identity": source_identity,
                    }
                    if memory_id is not None:
                        evidence_disposition["memory_id"] = memory_id
                evidence_disposition["source_kind"] = (
                    "external" if unit.origin == "external_observation" else "conversation"
                )
                evidence_dispositions.append(evidence_disposition)
                continue
            row = coverage_rows.get(unit.unit_id)
            if unit.origin == "unknown":
                # Completeness is a host fact, not a model semantic verdict.
                # Even an explicit model NO_CHANGE cannot erase missing data.
                decision, reason = "DEFERRED", "incomplete_tool_evidence"
            elif unit.unit_id in covered_unit_ids:
                decision, reason = "CANDIDATE", "candidate_checked"
            elif unit.can_support and row is None:
                # A physical unit with neither explicit coverage nor validated
                # candidate support remains unresolved; never clean it up.
                decision, reason = "DEFERRED", "coverage_unresolved"
            elif row is not None:
                decision, reason = row["decision"], row.get("reason", "coverage_unresolved")
            elif unit.can_support:
                decision, reason = "DEFERRED", "coverage_unresolved"
            else:
                decision, reason = "NO_CHANGE", unit.origin
            evidence_disposition = {
                "unit_id": unit.unit_id,
                "event_key": unit.event_key,
                "decision": decision,
                "reason": reason,
                "candidate_ids": list(dict.fromkeys(covered_by_unit.get(unit.unit_id, [])
                    or (row.get("candidate_ids", []) if row else []))),
                "source_identity": source_identity,
                "source_kind": (
                    "external" if unit.origin == "external_observation" else "conversation"
                ),
            }
            if (
                row is not None
                and row.get("reason") == "already_completed"
                and isinstance(row.get("memory_id"), str)
            ):
                evidence_disposition["memory_id"] = row["memory_id"]
            evidence_dispositions.append(evidence_disposition)
        # A retry may resolve a previously deferred candidate.  Keep the
        # unresolved rows observable, but remove stale duplicate entries from
        # the same turn's deferred ledger after a successful disposition.
        deferred_rows = self.audit._deferred_by_turn.get(turn_ref, [])
        disposition_by_candidate = {
            row.get("candidate_id").casefold(): row.get("disposition")
            for row in self.audit._dispositions_by_turn.get(turn_ref, [])
            if isinstance(row, Mapping) and isinstance(row.get("candidate_id"), str)
        }
        resolved_unit_ids: set[str] = set()
        for evidence_row in evidence_dispositions:
            if not isinstance(evidence_row, Mapping):
                continue
            source_identity = evidence_row.get("source_identity")
            if not isinstance(source_identity, str) or not source_identity:
                continue
            decision = evidence_row.get("decision")
            reason = evidence_row.get("reason")
            candidate_ids = evidence_row.get("candidate_ids", [])
            if not isinstance(candidate_ids, list):
                candidate_ids = []
            candidate_statuses = [
                disposition_by_candidate.get(candidate_id.casefold())
                for candidate_id in candidate_ids
                if isinstance(candidate_id, str)
            ]
            candidate_is_settled = bool(candidate_ids) and len(candidate_statuses) == len(candidate_ids) and all(
                status in {"CREATE", "UPDATE", "NO_CHANGE"}
                for status in candidate_statuses
            )
            if (
                decision == "CANDIDATE" and candidate_is_settled
            ) or (
                decision == "NO_CHANGE"
                and reason not in {"coverage_unresolved", "incomplete_tool_evidence"}
            ):
                unit_id = evidence_row.get("unit_id")
                if isinstance(unit_id, str) and unit_id:
                    resolved_unit_ids.add(unit_id)
                if evidence_row.get("source_kind") == "external":
                    planned_settled_sources.add((turn.source, turn.session_id, source_identity))
        retry_resolved_deferred: set[str] = set()
        if retry_ledger.get("same_turn"):
            for previous in retry_ledger.get("current_candidates", []):
                if not isinstance(previous, Mapping) or previous.get("disposition") != "DEFERRED":
                    continue
                candidate_id = previous.get("candidate_id")
                evidence_ids = previous.get("evidence_unit_ids", [])
                if (
                    isinstance(candidate_id, str)
                    and candidate_id
                    and isinstance(evidence_ids, list)
                    and evidence_ids
                    and all(
                        isinstance(unit_id, str) and unit_id in resolved_unit_ids
                        for unit_id in evidence_ids
                    )
                ):
                    retry_resolved_deferred.add(candidate_id.casefold())
        unique_deferred: dict[str, dict[str, Any]] = {}
        for raw_row in deferred_rows:
            if not isinstance(raw_row, Mapping) or not isinstance(raw_row.get("candidate_id"), str):
                continue
            candidate_id = raw_row["candidate_id"]
            if candidate_id.casefold() in retry_resolved_deferred:
                continue
            if disposition_by_candidate.get(candidate_id.casefold()) not in {None, "DEFERRED"}:
                continue
            unique_deferred.setdefault(candidate_id.casefold(), dict(raw_row))
        self.audit._deferred_by_turn[turn_ref] = list(unique_deferred.values())
        self.audit._evidence_by_turn[turn_ref] = evidence_dispositions
        return requests, observed_scopes
