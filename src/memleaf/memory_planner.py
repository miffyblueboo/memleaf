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
from .prompts import GATE_SYSTEM, SUMMARIZE_SYSTEM, gate_prompt, summarize_prompt
from .retrieval import normalize_term
from .validation import ModelOutputError, NO_CHANGE_DECISION, _model_scope_grounding_evidence, parse_gate_output, parse_strict_json, parse_summarize_output, is_aggregate_operational_text, is_attachment_followup_only_text, is_actionable_todo_text, is_mixed_future_use_text, split_mixed_future_use_text
from .process_common import ProcessingError, _REWORK_ACTION_MARKERS, _TARGET_NOT_RELATED, _TARGET_SAME_USE, _TARGET_UNKNOWN, _automatic_create_conflicts, _automatic_transient_memory, _candidate_lookup_queries, _completion_rework_candidate, _event_payload, _grounded_due_dates, _normalize_final_gate_raw, _normalize_summary_dates, _split_model_project_candidate, _todo_state_recovery_candidate


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
        detached_update_target_ids: dict[str, str] = {}
        deferred_type_mismatch_ids: set[str] = set()
        target_relations: dict[str, str] = {}
        unknown_target_ids: set[str] = set()
        candidate_level_target_ids: set[str] = set()
        scope_correction_plans: dict[str, dict[str, Any]] = {}

        def parse_gate(raw: str) -> dict[str, Any]:
            nonlocal gate_attempt_count, coverage_rows
            gate_attempt_count += 1
            detached_update_target_ids.clear()
            deferred_type_mismatch_ids.clear()
            target_relations.clear()
            unknown_target_ids.clear()
            candidate_level_target_ids.clear()
            scope_correction_plans.clear()
            raw, binding_value = split_semantic_envelope(raw)
            raw, coverage_value = split_gate_envelope(raw)
            raw_for_parse = (
                _normalize_final_gate_raw(raw, validation_scope_registry)
                if gate_attempt_count >= 3
                else raw
            )
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
                enforce_model_scope_grounding=gate_attempt_count < 3,
                allow_mixed_future_use=gate_attempt_count >= 3,
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

            if gate_attempt_count >= 3:
                candidates = []
                for candidate in parsed["candidates"]:
                    candidate_scopes = [
                        item
                        for item in candidate.get("scopes", [])
                        if isinstance(item, str)
                    ]
                    if (
                        candidate.get("scope_source") == "model"
                        and candidate.get("worth") is True
                        and any(item.partition(":")[0] == "project" for item in candidate_scopes)
                    ):
                        selected_owners, matches = _model_scope_grounding_evidence(
                            str(candidate.get("memory", "")),
                            candidate_scopes,
                            validation_scope_registry,
                        )
                        candidate = dict(candidate)
                        if not selected_owners:
                            candidates.append(candidate)
                            continue
                        if len(matches) == 1:
                            grounded_scope = next(iter(matches.values()))
                            if grounded_scope.casefold() not in set(selected_owners.values()):
                                non_project_scopes = [
                                    item
                                    for item in candidate_scopes
                                    if item.partition(":")[0] != "project"
                                ]
                                if all(
                                    item.casefold() != grounded_scope.casefold()
                                    for item in non_project_scopes
                                ):
                                    non_project_scopes.append(grounded_scope)
                                candidate["scopes"] = non_project_scopes
                        else:
                            candidate["scopes"] = ["unscoped"]
                            candidate["scope_source"] = "insufficient_context"
                    candidates.append(candidate)
                parsed = dict(parsed)
                parsed["candidates"] = candidates

            # A model may explicitly mark an aggregate as insufficiently
            # grounded on its first response.  It is already valid gate JSON,
            # so waiting for a retry would defer the whole turn unnecessarily.
            # Split only on project names found in this candidate's own text;
            # unknown or ambiguous fragments remain candidate-local deferred
            # work below.
            if gate_attempt_count >= 3 or any(
                candidate.get("scope_source") == "insufficient_context"
                for candidate in parsed["candidates"]
                if isinstance(candidate, Mapping)
            ):
                split_candidates: list[dict[str, Any]] = []
                for candidate in parsed["candidates"]:
                    split_candidates.extend(
                        _split_model_project_candidate(
                            candidate,
                            validation_scope_registry,
                        )
                    )
                parsed = dict(parsed)
                parsed["candidates"] = split_candidates

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
                    if not (
                        target_fields == {"duplicate_memory_id"}
                        and self.inputs._model_scope_is_elliptical(
                            candidate,
                            turn,
                            validation_scope_registry,
                        )
                    ):
                        invalid_targets[candidate_id] = target_fields
                    else:
                        # Let the candidate-local scope query reject this
                        # duplicate target once, preserving the original
                        # one-response safety boundary for inherited scopes.
                        candidate_level_target_ids.add(candidate_id)
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
            # ordering matters for aggregate turns: an unrelated target must
            # reach the existing final-retry detach path instead of consuming
            # all three attempts at the schema boundary.
            if type_mismatches and gate_attempt_count < 3:
                raise ModelOutputError(
                    "candidate type does not match update target",
                    validation_detail="update_target_type_mismatch",
                )

            if invalid_targets or type_mismatches:
                # A persistently non-converging model must not hold an entire
                # inbox turn hostage.  An unrelated update target can safely
                # become an independent CREATE candidate; an unrelated
                # duplicate has no independent fact to write and is dropped.
                candidates: list[dict[str, Any]] = []
                for candidate in parsed["candidates"]:
                    candidate_key = candidate["candidate_id"].casefold()
                    fields = invalid_targets.get(candidate_key)
                    mismatch = candidate_key in type_mismatches
                    if candidate_key in scope_correction_plans:
                        fields = None
                        mismatch = False
                    if mismatch:
                        # A type mismatch is never repaired by changing the
                        # target type.  If the target still serves the same
                        # future use, retain the candidate for an explicit
                        # scoped retry; detaching it would create a sibling
                        # for the same topic.  A clearly different durable
                        # topic may safely continue as CREATE.
                        candidate_copy = dict(candidate)
                        relation = target_relations.get(
                            candidate["candidate_id"].casefold(),
                            _TARGET_UNKNOWN,
                        )
                        if relation == _TARGET_NOT_RELATED and candidate.get("worth") is True:
                            wrong_target_id = candidate_copy.get("update_memory_id")
                            if isinstance(wrong_target_id, str):
                                detached_update_target_ids[candidate["candidate_id"].casefold()] = wrong_target_id
                            candidate_copy.pop("update_memory_id", None)
                            candidates.append(candidate_copy)
                        else:
                            deferred_type_mismatch_ids.add(candidate["candidate_id"].casefold())
                            candidates.append(candidate_copy)
                        continue
                    if candidate["candidate_id"].casefold() in unknown_target_ids:
                        candidates.append(candidate)
                        continue
                    if not fields:
                        candidates.append(candidate)
                        continue
                    if "duplicate_memory_id" in fields:
                        continue
                    if "update_memory_id" in fields and candidate.get("worth"):
                        independent = dict(candidate)
                        wrong_target_id = independent.get("update_memory_id")
                        if isinstance(wrong_target_id, str):
                            detached_update_target_ids[candidate["candidate_id"].casefold()] = wrong_target_id
                        independent.pop("update_memory_id", None)
                        candidates.append(independent)
                parsed = dict(parsed)
                parsed["candidates"] = candidates
            if gate_attempt_count >= 3:
                marked_candidates: list[dict[str, Any]] = []
                for candidate in parsed["candidates"]:
                    item = dict(candidate)
                    if item.get("worth") and is_mixed_future_use_text(item.get("memory")):
                        split = split_mixed_future_use_text(item.get("memory"))
                        if split is None:
                            item["_defer_reason"] = "mixed_future_use"
                            marked_candidates.append(item)
                            continue
                        base_id = str(item.get("candidate_id", "candidate"))
                        for index, (fragment, fragment_type) in enumerate(split, start=1):
                            child = dict(item)
                            digest = hashlib.sha256(fragment.encode("utf-8")).hexdigest()[:8]
                            child["candidate_id"] = f"{base_id}:future-{index}-{digest}"
                            child["memory"] = fragment
                            child["type"] = fragment_type
                            child["duplicate"] = False
                            child.pop("duplicate_memory_id", None)
                            child.pop("update_memory_id", None)
                            child.pop("_defer_reason", None)
                            marked_candidates.append(child)
                        continue
                    marked_candidates.append(item)
                parsed = dict(parsed)
                parsed["candidates"] = marked_candidates
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
            saved_maps = [deepcopy(value) for value in (detached_update_target_ids,
                deferred_type_mismatch_ids, target_relations, unknown_target_ids,
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
                    related_memory_ids=gate_related_memory_ids, scope_registry=validation_scope_registry)
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
                for current, old in zip((detached_update_target_ids, deferred_type_mismatch_ids,
                    target_relations, unknown_target_ids, candidate_level_target_ids, scope_correction_plans), saved_maps):
                    current.clear()
                    current.update(old)

        # A model may correctly treat the rest of a mixed turn as a query and
        # still miss the user's explicit completion update. Recover only one
        # uniquely related active todo; all other gate decisions stay intact.
        todo_state_recovery = _todo_state_recovery_candidate(events, related)
        recovery_by_candidate: dict[str, tuple[str, str, str]] = {}
        recovery_target_id: str | None = None
        recovery_candidate_value: dict[str, Any] | None = None
        if todo_state_recovery is not None:
            recovery_candidate, recovery_state, recovery_target_id, recovery_completed_at = todo_state_recovery
            recovery_candidate_value = dict(recovery_candidate)
            state_bindings = []
            for unit in evidence_units:
                if unit.source_role == "user" and unit.event_key in recovery_candidate["evidence_event_ids"] and unit.eligible:
                    state_bindings.append({"unit_id": unit.unit_id, "start": 0, "end": len(unit.text),
                        "quote": unit.text, "role": "assertion"})
            if state_bindings:
                recovery_candidate_value["_evidence_bindings"] = state_bindings
            recovery_candidate_id = recovery_candidate["candidate_id"].casefold()
            recovery_by_candidate[recovery_candidate_id] = (
                recovery_state,
                recovery_target_id,
                recovery_completed_at,
            )

            # Always use the deterministic recovery candidate for the old
            # target.  A model candidate that points at that target may be a
            # project/fact or an aggregate containing the new customer
            # revision; retaining it would either trigger a type mismatch or
            # overwrite the completion with the rework.
            recovery_key = recovery_target_id.casefold()
            gate["candidates"] = [
                item
                for item in gate["candidates"]
                if not (
                    isinstance(item, Mapping)
                    and any(
                        isinstance(item.get(field), str)
                        and item[field].casefold() == recovery_key
                        for field in ("update_memory_id", "duplicate_memory_id")
                    )
                )
            ]
            gate["candidates"].insert(0, recovery_candidate_value)

        force_create_candidate_ids: set[str] = set()
        completion_rework = (
            _completion_rework_candidate(events, validation_scope_registry)
            if todo_state_recovery is not None
            else None
        )
        if completion_rework is not None:
            # This existing state-recovery path copies a literal post-completion
            # tail. Bind only those copied clauses, never the completion clause
            # or unrelated assistant text, to the common admission boundary.
            rework_bindings = []
            copied = str(completion_rework.get("memory", ""))
            for unit in evidence_units:
                quote = unit.text.strip(" \t\r\n，,；;。.!！?？:：")
                if (unit.source_role == "user"
                    and unit.event_key in completion_rework["evidence_event_ids"]
                    and quote and quote in copied):
                    start = unit.text.index(quote)
                    rework_bindings.append({"unit_id": unit.unit_id, "start": start,
                        "end": start + len(quote), "quote": quote, "role": "assertion"})
            if rework_bindings:
                completion_rework["_evidence_bindings"] = rework_bindings
            rework_text = normalize_term(completion_rework.get("memory", ""))
            rework_scope_keys = {
                value.casefold()
                for value in completion_rework.get("scopes", [])
                if isinstance(value, str) and value.partition(":")[0] == "project"
            }
            rework_evidence_keys = {
                value.casefold()
                for value in completion_rework.get("evidence_event_ids", [])
                if isinstance(value, str)
            }
            exact_matches: list[dict[str, Any]] = []
            fallback_matches: list[dict[str, Any]] = []
            for item in gate["candidates"]:
                if not isinstance(item, Mapping) or item.get("worth") is not True:
                    continue
                if str(item.get("candidate_id", "")).casefold() in recovery_by_candidate:
                    continue
                candidate_text = normalize_term(item.get("memory", ""))
                candidate_evidence = {
                    value.casefold()
                    for value in item.get("evidence_event_ids", [])
                    if isinstance(value, str)
                }
                candidate_scope_keys = {
                    value.casefold()
                    for value in item.get("scopes", [])
                    if isinstance(value, str) and value.partition(":")[0] == "project"
                }
                if (
                    not rework_text
                    or not rework_evidence_keys.intersection(candidate_evidence)
                    or candidate_scope_keys != rework_scope_keys
                ):
                    continue
                full_text_match = (
                    rework_text in candidate_text or candidate_text in rework_text
                )
                if full_text_match:
                    exact_matches.append(dict(item))
                elif (
                    item.get("type") == "todo"
                    or is_actionable_todo_text(item.get("memory"))
                ) and any(marker in candidate_text for marker in _REWORK_ACTION_MARKERS):
                    fallback_matches.append(dict(item))

            # Prefer one model candidate that clearly covers the deterministic
            # tail.  Otherwise accept one same-event/same-project actionable
            # todo; multiple ambiguous candidates are replaced by the
            # deterministic full tail so they cannot coexist as duplicates.
            matched_candidates = exact_matches or fallback_matches
            exact_ids = {
                str(value.get("candidate_id", "")).casefold()
                for value in exact_matches
            }
            fallback_ids = {
                str(value.get("candidate_id", "")).casefold()
                for value in fallback_matches
            }
            discard_ids = set(exact_ids)
            if len(exact_matches) == 1:
                selected_text = normalize_term(exact_matches[0].get("memory", ""))
                discard_ids.update(
                    str(value.get("candidate_id", "")).casefold()
                    for value in fallback_matches
                    if (
                        selected_text
                        and normalize_term(value.get("memory", ""))
                        and (
                            normalize_term(value.get("memory", "")) in selected_text
                            or selected_text in normalize_term(value.get("memory", ""))
                        )
                    )
                )
            elif len(exact_matches) > 1 or len(fallback_matches) > 1:
                discard_ids.update(fallback_ids)
            rework_candidates: list[dict[str, Any]] = []
            found_rework = len(matched_candidates) == 1
            selected_id = (
                str(matched_candidates[0].get("candidate_id", "")).casefold()
                if found_rework
                else ""
            )
            for item in gate["candidates"]:
                if not isinstance(item, Mapping):
                    continue
                candidate_item = dict(item)
                candidate_id_key = str(candidate_item.get("candidate_id", "")).casefold()
                if candidate_id_key in discard_ids:
                    if found_rework and candidate_id_key == selected_id:
                        candidate_item.pop("update_memory_id", None)
                        candidate_item.pop("duplicate_memory_id", None)
                        candidate_item["duplicate"] = False
                        candidate_item["type"] = "todo"
                        candidate_item["scopes"] = list(completion_rework["scopes"])
                        candidate_item["scope_source"] = completion_rework["scope_source"]
                        candidate_item.pop("_defer_reason", None)
                        candidate_item["_force_create"] = True
                        force_create_candidate_ids.add(candidate_id_key)
                        rework_candidates.append(candidate_item)
                    # Ambiguous model matches are discarded in favor of the
                    # deterministic fallback appended below.
                    continue
                rework_candidates.append(candidate_item)
            if not found_rework:
                rework_candidates.append(dict(completion_rework))
                force_create_candidate_ids.add(completion_rework["candidate_id"].casefold())
            # Keep the deterministic completion first, then any independent
            # rework candidate(s), followed by unrelated gate decisions.
            recovery_ids = set(recovery_by_candidate)
            gate["candidates"] = [
                item
                for item in rework_candidates
                if str(item.get("candidate_id", "")).casefold() not in recovery_ids
            ]
            if recovery_candidate_value is not None:
                gate["candidates"].insert(0, recovery_candidate_value)
        requests: list[dict[str, Any]] = []
        observed_scopes: list[str] = []
        current_turn_request_ids: set[str] = set()
        read_only_query = read_only_turn(evidence_units)
        covered_unit_ids: set[str] = set()
        covered_by_unit: dict[str, list[str]] = {}
        seen_candidates: set[tuple[Any, ...]] = set()
        conflicting_targets: set[str] = set()
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
            # Recheck every origin here, including deterministic state recovery.
            if self.inputs._scope_evidence_conflict(candidate, turn, validation_scope_registry):
                self.audit._defer_candidate(turn_ref, candidate, "scope_conflict", scopes=candidate.get("scopes", []))
                continue
            candidate_id_key = str(candidate.get("candidate_id", "")).casefold()
            force_create = (
                candidate_id_key in force_create_candidate_ids
                or candidate.get("_force_create") is True
            )
            if force_create:
                candidate = dict(candidate)
                candidate.pop("_force_create", None)
            recovery = recovery_by_candidate.get(candidate_id_key)
            correction_plan = scope_correction_plans.get(candidate_id_key)
            detached_update_target_id = detached_update_target_ids.get(candidate_id_key)
            defer_reason = candidate.get("_defer_reason")
            # A pure existing-memory query must not leave even a deferred
            # candidate behind when the model mislabels its recap.  Explicit
            # todo state recovery is the only write-eligible exception.
            if read_only_query and recovery is None and not candidate.get("_evidence_bindings"):
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
            if candidate.get("worth") and (
                _automatic_transient_memory(candidate.get("memory"))
            ):
                self.audit._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="transient",
                )
                continue
            # A combined mailbox/daily digest is not an atomic memory.  If a
            # concrete action was worth retaining, the gate must emit it as
            # its own candidate; the aggregate shell itself is NO_CHANGE.
            if candidate.get("worth") and is_aggregate_operational_text(candidate.get("memory")):
                self.audit._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="aggregate",
                )
                continue
            if candidate.get("worth") and is_attachment_followup_only_text(candidate.get("memory")):
                self.audit._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="attachment_followup",
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
                and detached_update_target_id is None
                and correction_plan is None
                and (
                    candidate["worth"] or has_target
                )
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
                and detached_update_target_id is None
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

            if candidate_id_key in deferred_type_mismatch_ids:
                self.audit._defer_candidate(
                    turn_ref,
                    candidate,
                    "update_target_type_mismatch",
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
                priority_only=(scope_directory is not None and detached_update_target_id is None),
                scope_records=(
                    scoped_records
                    if scope_directory is not None and detached_update_target_id is None
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
            if recovery is not None:
                recovery_target = self.inputs._active_memory_by_id(recovery[1])
                if recovery_target is not None and not any(
                    isinstance(item, Mapping)
                    and isinstance(item.get("memory_id"), str)
                    and item["memory_id"].casefold() == recovery_target.memory_id.casefold()
                    for item in candidate_related
                ):
                    candidate_related = [recovery_target.to_dict(), *candidate_related]
            if detached_update_target_id is not None:
                candidate_related = [
                    item
                    for item in candidate_related
                    if not (
                        isinstance(item.get("memory_id"), str)
                        and item["memory_id"].casefold() == detached_update_target_id.casefold()
                    )
                ]
            if not force_create and correction_plan is None:
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
                            scope_directory if detached_update_target_id is None else None
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
                    if recovery is not None and isinstance(raw, str):
                        # Complete/cancel state is deterministic evidence from
                        # the user event. Inject it before strict validation so
                        # a model that omits status/completed_at cannot leave
                        # the update active or fail the required-field check.
                        parsed_raw = parse_strict_json(raw)
                        if isinstance(parsed_raw, Mapping) and "decision" not in parsed_raw:
                            state_change, target_id, completed_at = recovery
                            parsed_raw = dict(parsed_raw)
                            parsed_raw["update_memory_id"] = target_id
                            parsed_raw["status"] = state_change
                            if state_change == "completed":
                                parsed_raw["completed_at"] = completed_at
                            else:
                                parsed_raw.pop("completed_at", None)
                            raw = json.dumps(parsed_raw, ensure_ascii=False, separators=(",", ":"))
                    parsed = parse_summarize_output(
                        _normalize_summary_dates(raw, turn, candidate),
                        current_event_keys=admitted_summary_keys,
                        related_native_ids=candidate_native_ids,
                        related_memory_ids=same_type_update_memory_ids,
                        scope_registry=validation_scope_registry,
                        expected_scopes=candidate["scopes"],
                        expected_scope_source=candidate["scope_source"],
                        allowed_due_dates=_grounded_due_dates(turn),
                        allow_no_change=recovery is None,
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
            if is_attachment_followup_only_text(
                f"{summary.get('title', '')}\n{summary.get('body', '')}"
            ):
                self.audit._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="attachment_followup",
                )
                continue
            if _automatic_transient_memory(
                f"{summary.get('title', '')}\n{summary.get('body', '')}"
            ):
                self.audit._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="transient",
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
                summary = self.inputs._merge_additive_project_plan_update(candidate, summary, target)
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
                if dedup_digest(r.get("summary", {})) == dedup_digest(summary)), None)
            if same_request is not None:
                self.audit._record_disposition(turn_ref, candidate, "NO_CHANGE", reason="same_turn_duplicate")
                continue
            competing = [r for r in requests if summary_update_target and
                         r.get("summary", {}).get("update_memory_id") == summary_update_target]
            if summary_update_target and (competing or summary_update_target in conflicting_targets):
                conflicting_targets.add(summary_update_target)
                for previous in competing:
                    requests.remove(previous)
                    self.audit._defer_candidate(turn_ref, {"candidate_id": previous["candidate_id"],
                        "scopes": previous["summary"]["scopes"], "scope_source": previous["summary"].get("scope_source")},
                        "same_turn_target_conflict")
                self.audit._defer_candidate(turn_ref, candidate, "same_turn_target_conflict", scopes=candidate_scopes)
                continue
            pending_request["evidence_unit_ids"] = list(candidate.get("evidence_unit_ids", []))
            if summary_update_target:
                pending_request["expected_revision"] = target_revisions.get(summary_update_target)
            requests.append(pending_request)
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
        if conflicting_targets and not requests:
            raise ModelOutputError("no non-conflicting writes in turn", validation_detail="duplicate_update_target")
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
