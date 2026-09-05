"""Build memory-change requests without committing Markdown."""
from __future__ import annotations
import hashlib
import json
from copy import deepcopy
from typing import Any, Iterable, Mapping, Optional
from .admission import analyze_turn_evidence, admission_reason, read_only_turn, summary_evidence, evidence_prompt, parse_coverage, split_gate_envelope, supporting_units, split_semantic_envelope, validate_bindings, validate_coverage_bindings
from .index import turn_key
from .inbox import InboxTurn
from .llm import ModelError
from .memory_writer import MemoryWriter
from .turn_plan import dedup_digest, revision_digest
from .update_coordinator import UpdateCoordinator
from .evidence_policy import retain_tool_evidence
from .prompts import GATE_SYSTEM, SUMMARIZE_SYSTEM, gate_prompt, summarize_prompt
from .retrieval import normalize_term
from .validation import ModelOutputError, NO_CHANGE_DECISION, _model_scope_grounding_evidence, parse_gate_output, parse_strict_json, parse_summarize_output
from .process_common import ProcessingError, _TARGET_NOT_RELATED, _TARGET_SAME_USE, _TARGET_UNKNOWN, _automatic_create_conflicts, _candidate_lookup_queries, _event_payload, _grounded_due_dates, _normalize_summary_dates


class MemoryPlanner:
    def __init__(self, service: Any, audit: Any, inputs: Any, model: Any):
        self.service = service
        self.audit = audit
        self.inputs = inputs
        self.model = model

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
        events = _event_payload(turn)
        # Capture policy also applies to unprocessed legacy inbox evidence.
        # Keep the immutable turn untouched for input-digest/replay validation.
        # Explicit remember supplies its own visible user text; it does not
        # revive previously excluded tool observations.
        policy_config = self.service.vault.config()
        for event in events:
            event["tool_evidence"] = retain_tool_evidence(event["tool_evidence"], policy_config)
        evidence_units = analyze_turn_evidence(events)
        coverage_rows: dict[str, dict[str, Any]] = {}
        turn_ref = (turn.source, turn.session_id, turn.turn_key)
        self.audit._deferred_by_turn.setdefault(turn_ref, [])
        related, scope_background, native_refs, scope_fallback = self.inputs._related(
            turn,
            state,
            scope,
            overlay=self.audit._planned_related,
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
            if scope_ambiguous and self.inputs._single_specific_scope(scope_background):
                scope_directory, scope_directory_complete = self.inputs._scope_directory(scoped_records)
                gate_related = []
                for entry in scope_directory:
                    memory_id = entry.get("memory_id")
                    if isinstance(memory_id, str) and memory_id.casefold() not in {
                        value.casefold() for value in gate_related_memory_ids
                    }:
                        gate_related_memory_ids.append(memory_id)
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

        gate_attempt_count = 0
        target_relations: dict[str, str] = {}
        unknown_target_ids: set[str] = set()
        candidate_level_target_ids: set[str] = set()
        scope_correction_plans: dict[str, dict[str, Any]] = {}

        def parse_gate(raw: str) -> dict[str, Any]:
            nonlocal gate_attempt_count, coverage_rows
            gate_attempt_count += 1
            target_relations.clear()
            unknown_target_ids.clear()
            candidate_level_target_ids.clear()
            scope_correction_plans.clear()
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
            )
            for item in parsed["candidates"]:
                for field in ("duplicate_memory_id", "update_memory_id"):
                    target_id = item.get(field)
                    if isinstance(target_id, str) and target_id.casefold() not in ordinary_ids:
                        if (field != "update_memory_id"
                            or allowed_corrections.get(item["candidate_id"].casefold()) != target_id):
                            raise ModelOutputError("target is not authorized for this candidate",
                                                   validation_detail="invalid_update_target")
            coverage_rows = (parse_coverage(coverage_value, evidence_units, parsed["candidates"], require_complete=False)
                             if coverage_value is not None else {})
            if binding_value is not None:
                bindings = validate_bindings(binding_value, evidence_units, parsed["candidates"])
                for item in parsed["candidates"]:
                    if item["candidate_id"] in bindings:
                        claims = bindings[item["candidate_id"]]
                        for claim in claims:
                            row = coverage_rows.get(claim["unit_id"])
                            if row is not None and (row["decision"] != "CANDIDATE"
                                or item["candidate_id"] not in row["candidate_ids"]):
                                raise ModelOutputError("binding contradicts coverage", validation_detail="invalid_evidence")
                        item["_evidence_bindings"] = claims
            validate_coverage_bindings(coverage_rows, evidence_units, parsed["candidates"])

            prepared_candidates: list[dict[str, Any]] = []
            for candidate in parsed["candidates"]:
                item = dict(candidate)
                if self.inputs._scope_evidence_conflict(item, turn, validation_scope_registry):
                    item["_defer_reason"] = "scope_conflict"
                plan = self.inputs._scope_correction_plan(item, turn, validation_scope_registry)
                if plan is not None:
                    item.pop("duplicate_memory_id", None)
                    item["duplicate"] = False
                    if isinstance(plan.get("target_memory_id"), str):
                        item["update_memory_id"] = plan["target_memory_id"]
                    else:
                        item.pop("update_memory_id", None)
                    scope_correction_plans[str(item["candidate_id"]).casefold()] = plan
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
                correction = scope_correction_plans.get(candidate_id)
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
                target_relations[candidate_id] = relation
                if relation == _TARGET_NOT_RELATED:
                    invalid_targets[candidate_id] = target_fields
                elif relation == _TARGET_UNKNOWN:
                    unknown_target_ids.add(candidate_id)
                if "update_memory_id" in target_fields:
                    target = self.inputs._active_memory_by_id(candidate["update_memory_id"])
                    if target is None:
                        unknown_target_ids.add(candidate_id)
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
                    if cid not in scope_correction_plans:
                        if cid in type_mismatches:
                            item["_defer_reason"] = "update_target_type_mismatch"
                        elif cid in invalid_targets:
                            item["_defer_reason"] = "target_not_relevant"
                    candidates.append(item)
                parsed = {**parsed, "candidates": candidates}
            return parsed

        gate = self.model._complete_json_stage(
            backend,
            gate_prompt(
                events,
                related_memories=gate_related,
                scope_directory=scope_directory,
                scope_directory_complete=scope_directory_complete,
                scope_background=scope_background,
                scope_registry=scope_registry,
            ) + evidence_prompt(evidence_units),
            system=GATE_SYSTEM,
            purpose="gate",
            parser=parse_gate,
            diagnostic_context={
                "source": turn.source,
                "session_id": turn.session_id,
                "turn_index": turn.turn_index,
            },
        )
        # One bounded, source-neutral coverage repair. This is NOT a writer:
        # every returned candidate is parsed again by the same Gate boundary.
        accounted = set(coverage_rows)
        for initial in gate["candidates"]:
            accounted.update(unit.unit_id for unit in supporting_units(initial, evidence_units))
        missing = tuple(unit for unit in evidence_units if (unit.eligible or unit.origin == "user_document") and unit.unit_id not in accounted)
        if missing:
            saved_gate = deepcopy(gate)
            saved_coverage = deepcopy(coverage_rows)
            saved_maps = [deepcopy(value) for value in (target_relations, unknown_target_ids,
                candidate_level_target_ids, scope_correction_plans)]
            try:
                correction_raw = self.model._complete(backend,
                    "Coverage correction: classify ONLY the supplied unresolved evidence units. "
                    "Do not re-emit already handled items. Return the same Gate JSON contract.\n"
                    + gate_prompt([], related_memories=gate_related, scope_background=scope_background,
                                  scope_registry=scope_registry)
                    + evidence_prompt(missing)
                    + "\nAlready handled candidate IDs: "
                    + json.dumps([item["candidate_id"] for item in gate["candidates"]]),
                    system=GATE_SYSTEM, purpose="gate")
                correction_raw, correction_bindings = split_semantic_envelope(correction_raw)
                correction_raw, correction_coverage = split_gate_envelope(correction_raw)
                correction_gate = parse_gate_output(correction_raw,
                    current_event_keys=tuple(dict.fromkeys(unit.event_key for unit in missing)),
                    related_memory_ids=gate_related_memory_ids, scope_registry=validation_scope_registry,
                    allow_shared_update_targets=True)
                new_ids = {item["candidate_id"] for item in correction_gate["candidates"]}
                if new_ids.intersection(item["candidate_id"] for item in gate["candidates"]):
                    raise ModelOutputError("coverage correction reused a candidate id", validation_detail="duplicate_candidate_id")
                if correction_bindings is not None:
                    validate_bindings(correction_bindings, missing, correction_gate["candidates"])
                new_coverage = (parse_coverage(correction_coverage, missing, correction_gate["candidates"],
                                require_complete=False) if correction_coverage is not None else {})
                public_candidates = [{key: value for key, value in item.items()
                    if not key.startswith("_") and key != "evidence_unit_ids"} for item in gate["candidates"]]
                old_bindings = [{"candidate_id": item["candidate_id"], "claims": item["_evidence_bindings"]}
                    for item in gate["candidates"] if item.get("_evidence_bindings")]
                merged = {"candidates": public_candidates + correction_gate["candidates"],
                          "coverage": list(saved_coverage.values()) + list(new_coverage.values()),
                          "evidence_bindings": old_bindings + (correction_bindings or [])}
                gate = parse_gate(json.dumps(merged, ensure_ascii=False))
            except (ModelError, ModelOutputError):
                # A failed correction cannot invalidate already validated siblings.
                gate = saved_gate
                coverage_rows = saved_coverage
                for current, old in zip((target_relations, unknown_target_ids,
                    candidate_level_target_ids, scope_correction_plans), saved_maps):
                    current.clear()
                    current.update(old)

        requests: list[dict[str, Any]] = []
        observed_scopes: list[str] = []
        current_turn_request_ids: set[str] = set()
        read_only_query = read_only_turn(evidence_units)
        covered_unit_ids: set[str] = set()
        covered_by_unit: dict[str, list[str]] = {}
        seen_candidates: set[tuple[Any, ...]] = set()
        admitted_candidates: dict[str, dict[str, Any]] = {}
        for candidate in gate["candidates"]:
            candidate = dict(candidate)
            unit_ids = [uid for uid, row in coverage_rows.items()
                        if candidate.get("candidate_id") in row.get("candidate_ids", [])]
            if unit_ids:
                candidate["_evidence_unit_ids"] = unit_ids
            reason, support = admission_reason(candidate, evidence_units)
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
            # All model candidates pass the same evidence and Scope boundary.
            if self.inputs._scope_evidence_conflict(candidate, turn, validation_scope_registry):
                self.audit._defer_candidate(turn_ref, candidate, "scope_conflict", scopes=candidate.get("scopes", []))
                continue
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
                requests.append(
                    self.inputs._scope_correction_request(
                        candidate,
                        turn,
                        correction_plan,
                        conversation_title=title,
                        native_refs=candidate_native_refs,
                    )
                )
                for observed_scope in candidate.get("scopes", []):
                    if isinstance(observed_scope, str) and observed_scope != "unscoped" and observed_scope not in observed_scopes:
                        observed_scopes.append(observed_scope)
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
                    requests.append(
                        self._duplicate_request(
                            candidate,
                            turn,
                            conversation_title=title,
                            native_refs=candidate_native_refs,
                        )
                    )
                    # Automatic duplicate observations are metadata no-ops;
                    # only the already-active target's scopes are trustworthy
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
                    for observed_scope in duplicate_scopes:
                        if (
                            isinstance(observed_scope, str)
                            and observed_scope != "unscoped"
                            and observed_scope not in observed_scopes
                        ):
                            observed_scopes.append(observed_scope)
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

            admitted_summary_events = summary_evidence(candidate, evidence_units, events=events)
            admitted_summary_keys = tuple(dict.fromkeys(event["event_key"] for event in admitted_summary_events))
            try:
                def parse_summary(raw: str) -> dict[str, Any]:
                    parsed = parse_summarize_output(
                        _normalize_summary_dates(raw, turn, candidate),
                        current_event_keys=admitted_summary_keys,
                        related_native_ids=candidate_native_ids,
                        related_memory_ids=same_type_update_memory_ids,
                        scope_registry=validation_scope_registry,
                        expected_scopes=candidate["scopes"],
                        expected_scope_source=candidate["scope_source"],
                        allowed_due_dates=_grounded_due_dates(turn),
                        allow_no_change=True,
                        # The summarize stage may not reinterpret a gate
                        # candidate, including CREATE candidates. Updates
                        # additionally retain the active target's immutable
                        # type below.
                        expected_type=candidate.get("type"),
                        expected_update_memory_id=gate_update_target,
                        expected_target_type=gate_target_type,
                    )
                    return parsed

                target_revisions = {}
                for related_item in candidate_related:
                    if isinstance(related_item.get("memory_id"), str):
                        target_memory = self.inputs._active_memory_by_id(related_item["memory_id"])
                        if target_memory is not None:
                            target_revisions[target_memory.memory_id] = revision_digest(target_memory)
                summary = self.model._complete_json_stage(
                    backend,
                    summarize_prompt(
                        candidate,
                        admitted_summary_events,
                        related_memories=candidate_related,
                        scope_background=candidate_scope_background,
                        scope_registry=scope_registry,
                    ),
                    system=SUMMARIZE_SYSTEM,
                    purpose="summarize",
                    parser=parse_summary,
                    diagnostic_context={
                        "source": turn.source,
                        "session_id": turn.session_id,
                        "turn_index": turn.turn_index,
                    },
                )
            except ModelOutputError as error:
                if getattr(error, "validation_detail", None) != "relative_time":
                    raise
                # The candidate's source turn remains in inbox for an
                # explicit retry.  Other candidates from this same turn may
                # still commit safely in the same transaction.
                self.audit._defer_candidate(
                    turn_ref,
                    candidate,
                    "relative_time",
                    scopes=candidate["scopes"],
                )
                continue
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
                    summary = dict(summary)
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
                    summary = dict(summary)
                    summary["update_memory_id"] = gate_update_target
                target = self.service.read(gate_update_target, include_history=False)
                # The summary is the complete model-proposed current value.
                # Do not concatenate old/new bodies using business keywords.
                pass
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
            for observed_scope in summary["scopes"]:
                if (
                    isinstance(observed_scope, str)
                    and observed_scope != "unscoped"
                    and observed_scope not in observed_scopes
                ):
                    observed_scopes.append(observed_scope)
        requests = UpdateCoordinator(self.model, self.audit, self.inputs._active_memory_by_id).resolve(
            requests, candidates=admitted_candidates, evidence_units=evidence_units, events=events,
            backend=backend, scope_registry=scope_registry, validation_scope_registry=validation_scope_registry)
        evidence_dispositions = []
        for unit in evidence_units:
            row = coverage_rows.get(unit.unit_id)
            if unit.origin == "unknown":
                # Completeness is a host fact, not a model semantic verdict.
                # Even an explicit model NO_CHANGE cannot erase missing data.
                decision, reason = "DEFERRED", "incomplete_tool_evidence"
            elif unit.unit_id in covered_unit_ids:
                decision, reason = "CANDIDATE", "candidate_checked"
            elif row is not None:
                decision, reason = row["decision"], row.get("reason", "coverage_unresolved")
            elif unit.eligible or unit.origin == "user_document":
                decision, reason = "DEFERRED", "coverage_unresolved"
            else:
                decision, reason = "NO_CHANGE", unit.origin
            evidence_dispositions.append({"unit_id": unit.unit_id, "event_key": unit.event_key,
                                          "decision": decision, "reason": reason,
                "candidate_ids": list(dict.fromkeys(covered_by_unit.get(unit.unit_id, [])
                    or (row.get("candidate_ids", []) if row else [])))})
        self.audit._evidence_by_turn[turn_ref] = evidence_dispositions
        return requests, observed_scopes
