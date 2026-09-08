"""Reconcile same-turn updates before freezing a single mutation per target.

Only the model decides whether admitted changes are compatible. This module
checks identities, evidence, revisions and complete group accounting; it never
concatenates proposed bodies into a memory or writes to the Vault.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from typing import Any, Callable, Mapping

from .admission import summary_evidence
from .llm import ModelError
from .prompts import UPDATE_GROUP_SYSTEM, summarize_prompt
from .process_common import ProcessingError, _grounded_due_dates, _normalize_summary_dates, _summary_date_grounding_violations
from .turn_plan import revision_digest
from .update_review import review_create, review_update
from .validation import ModelOutputError, parse_strict_json, parse_summarize_output

# Exceptional prompt safety guards, not a truncation policy. Over-budget groups
# remain in inbox; no admitted member is silently omitted or partially written.
_MAX_GROUP_MEMBERS = 32
_MAX_GROUP_BYTES = 128 * 1024


class UpdateCoordinator:
    def __init__(self, model: Any, audit: Any, read_target: Callable[[str], Any]):
        self.model = model
        self.audit = audit
        self.read_target = read_target

    def resolve(self, requests: list[dict[str, Any]], *, candidates: Mapping[str, Any],
                evidence_units: Any, events: list[dict[str, Any]], backend: Any,
                scope_registry: Any, validation_scope_registry: Any) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = OrderedDict()
        for request in requests:
            target = request.get('summary', {}).get('update_memory_id')
            if isinstance(target, str) and target:
                groups.setdefault(target.casefold(), []).append(request)
        replacements: dict[int, dict[str, Any] | None] = {}
        for group in groups.values():
            if len(group) < 2:
                continue
            resolved = self._resolve_group(group, candidates=candidates,
                evidence_units=evidence_units, events=events, backend=backend,
                scope_registry=scope_registry, validation_scope_registry=validation_scope_registry)
            replacements[id(group[0])] = resolved
            for request in group[1:]:
                replacements[id(request)] = None
        resolved_requests = [replacements.get(id(request), request) for request in requests
                             if replacements.get(id(request), request) is not None]
        reviewed_updates = self._review_final_updates(
            resolved_requests,
            candidates=candidates,
            evidence_units=evidence_units,
            events=events,
            backend=backend,
            scope_registry=scope_registry,
            validation_scope_registry=validation_scope_registry,
        )
        return self._review_final_creates(
            reviewed_updates,
            candidates=candidates,
            evidence_units=evidence_units,
            events=events,
            backend=backend,
            scope_registry=scope_registry,
            validation_scope_registry=validation_scope_registry,
        )

    @staticmethod
    def _request_candidate_ids(
        request: Mapping[str, Any],
    ) -> tuple[str, ...]:
        """Return original candidate IDs contributing to one frozen request."""

        values: list[str] = []
        contributors = request.get("contributing_candidates")
        if isinstance(contributors, list):
            for contributor in contributors:
                if isinstance(contributor, Mapping):
                    value = contributor.get("candidate_id")
                    if isinstance(value, str) and value and value not in values:
                        values.append(value)
        if not values:
            value = request.get("candidate_id")
            if isinstance(value, str) and value:
                values.append(value)
        return tuple(values)

    @staticmethod
    def _projected_request_evidence(
        request: Mapping[str, Any],
        *,
        candidates: Mapping[str, Any],
        evidence_units: Any,
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Project only admitted evidence for a final single or merged request."""

        member_candidates = [
            candidates[candidate_id]
            for candidate_id in UpdateCoordinator._request_candidate_ids(request)
            if candidate_id in candidates and isinstance(candidates[candidate_id], Mapping)
        ]
        projected: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        # Bound quotes are the authority for new content. Keep their own
        # current visible message as interpretation context so a quote cannot
        # hide a negation, qualification or reference in a neighboring clause.
        # Never take context from tool payloads or related stored memories.
        visible_context = {
            (event.get("event_key"), event.get("role")): event["content"]
            for event in events
            if event.get("role") in {"user", "assistant"}
            and isinstance(event.get("content"), str)
        }
        contextualized: set[tuple[str, str]] = set()
        for candidate in member_candidates:
            for event in summary_evidence(candidate, evidence_units, events=events):
                identity = (
                    str(event.get("event_key", "")),
                    str(event.get("unit_id", "")),
                    str(event.get("content", "")),
                )
                if identity in seen:
                    continue
                seen.add(identity)
                item = deepcopy(event)
                context_key = (item.get("event_key"), item.get("role"))
                context = visible_context.get(context_key)
                if context is not None and context_key not in contextualized:
                    if context != item.get("content"):
                        item["source_context"] = context
                    contextualized.add(context_key)
                projected.append(item)
        unit_order = {
            getattr(unit, "unit_id", ""): (index, getattr(unit, "text", ""))
            for index, unit in enumerate(evidence_units)
        }
        projected.sort(key=lambda event: (
            unit_order.get(event.get("unit_id"), (len(unit_order), ""))[0],
            unit_order.get(event.get("unit_id"), (len(unit_order), ""))[1].find(
                event.get("content", "")
            ),
        ))
        return projected

    @staticmethod
    def _is_reviewable_update(request: Mapping[str, Any]) -> bool:
        """Limit the extra semantic stage to ordinary automatic updates."""

        summary = request.get("summary")
        if not isinstance(summary, Mapping):
            return False
        target_id = summary.get("update_memory_id")
        if not isinstance(target_id, str) or not target_id:
            return False
        if request.get("explicit_remember") is True:
            return False
        if request.get("scope_correction") or request.get("duplicate_memory_id"):
            return False
        return True

    @staticmethod
    def _is_reviewable_create(request: Mapping[str, Any]) -> bool:
        """Limit CREATE review to ordinary automatic memory proposals."""

        summary = request.get("summary")
        if not isinstance(summary, Mapping):
            return False
        if summary.get("update_memory_id") or request.get("duplicate_memory_id"):
            return False
        if request.get("explicit_remember") is True:
            return False
        if request.get("scope_correction"):
            return False
        return True

    @staticmethod
    def _review_content(summary: Mapping[str, Any]) -> dict[str, Any]:
        """Separate already validated side effects from semantic body review."""
        return {key: value for key, value in summary.items()
                if key not in {"scope_operations", "shadow_native_ids"}}

    @staticmethod
    def _reviewed_summary(original: Mapping[str, Any], revised: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(revised)
        for key in ("scope_operations", "shadow_native_ids"):
            if key in original:
                result[key] = deepcopy(original[key])
        return result

    def _defer_request(
        self,
        request: Mapping[str, Any],
        *,
        candidates: Mapping[str, Any],
        reason: str,
    ) -> None:
        turn = request.get("turn")
        if turn is None:
            return
        turn_ref = (turn.source, turn.session_id, turn.turn_key)
        for candidate_id in self._request_candidate_ids(request):
            candidate = candidates.get(candidate_id)
            if isinstance(candidate, Mapping):
                self.audit._defer_candidate(turn_ref, candidate, reason)

    def _run_review_jobs(
        self,
        jobs: list[Callable[[], dict[str, Any]]],
        *,
        backend: Any,
    ) -> list[dict[str, Any]]:
        """Run independent model reviews concurrently while preserving result order."""

        if not jobs:
            return []
        workers = min(len(jobs), self.model.max_parallel_calls(backend))
        if workers <= 1:
            return [job() for job in jobs]
        # No Vault or audit mutation occurs in worker threads. All request/audit
        # effects are applied below in original request order after every model
        # result has been collected.
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="memleaf-review") as pool:
            futures = [pool.submit(job) for job in jobs]
            return [future.result() for future in futures]

    @staticmethod
    def _make_update_review_parser(
        *,
        turn: Any,
        candidate: Mapping[str, Any],
        keys: tuple[str, ...],
        target: Any,
        target_id: str,
        target_type: Any,
        target_scopes: Any,
        target_scope_source: Any,
        grounded_dates: Any,
        validation_scope_registry: Any,
        projected: list[dict[str, Any]],
    ) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
        """Freeze one UPDATE parser so concurrent reviews cannot share loop state."""

        def parse_review_summary(value: Mapping[str, Any]) -> Mapping[str, Any]:
            raw_summary = json.dumps(value, ensure_ascii=False)
            parsed = parse_summarize_output(
                _normalize_summary_dates(raw_summary, turn, candidate),
                current_event_keys=keys,
                related_native_ids=[],
                related_memory_ids=[target_id],
                scope_registry=validation_scope_registry,
                expected_scopes=target_scopes,
                expected_scope_source=target_scope_source,
                expected_type=target_type,
                expected_target_type=target_type,
                expected_update_memory_id=target_id,
                allowed_due_dates=grounded_dates,
                allow_no_change=False,
            )
            parsed_target = parsed.get("update_memory_id")
            if (
                not isinstance(parsed_target, str)
                or parsed_target.casefold() != target_id.casefold()
            ):
                raise ModelOutputError(
                    "review summary must retain its update target",
                    validation_detail="invalid_update_target",
                )
            parsed["update_memory_id"] = target_id
            if parsed.get("scope_operations") or parsed.get("shadow_native_ids"):
                raise ModelOutputError(
                    "review summary cannot extend authorization",
                    validation_detail="invalid_evidence",
                )
            if _summary_date_grounding_violations(
                parsed,
                grounded_dates=grounded_dates,
                source_texts=[
                    event.get("content", "")
                    for event in projected
                    if event.get("role") in {"user", "assistant"}
                ],
                preserved_texts=(
                    target.get("title"),
                    target.get("body"),
                    target.get("due_date"),
                ) if isinstance(target, Mapping) else (
                    target.title,
                    target.body,
                    target.due_date,
                ),
            ):
                raise ModelOutputError(
                    "review summary contains an ungrounded date",
                    validation_detail="relative_time",
                )
            cited = set(parsed.get("evidence_event_ids", []))
            for source in parsed.get("sources", []):
                if source.get("event_key"):
                    cited.add(source["event_key"])
                cited.update(source.get("evidence_event_ids", []))
            if not set(keys).issubset(cited):
                raise ModelOutputError(
                    "review summary omitted source evidence",
                    validation_detail="invalid_evidence",
                )
            return parsed

        return parse_review_summary

    @staticmethod
    def _make_create_review_parser(
        *,
        turn: Any,
        candidate: Mapping[str, Any],
        keys: tuple[str, ...],
        summary_type: Any,
        summary_scopes: Any,
        summary_scope_source: Any,
        grounded_dates: Any,
        validation_scope_registry: Any,
        projected: list[dict[str, Any]],
    ) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
        """Freeze one CREATE parser so concurrent reviews cannot share loop state."""

        def parse_create_summary(value: Mapping[str, Any]) -> Mapping[str, Any]:
            raw_summary = json.dumps(value, ensure_ascii=False)
            parsed = parse_summarize_output(
                _normalize_summary_dates(raw_summary, turn, candidate),
                current_event_keys=keys,
                related_native_ids=[],
                related_memory_ids=[],
                scope_registry=validation_scope_registry,
                expected_scopes=summary_scopes,
                expected_scope_source=summary_scope_source,
                expected_type=summary_type,
                expected_update_memory_id=None,
                allowed_due_dates=grounded_dates,
                allow_no_change=False,
                allow_update_target=False,
            )
            if parsed.get("memory_id") or parsed.get("update_memory_id"):
                raise ModelOutputError(
                    "CREATE review summary cannot carry a target",
                    validation_detail="invalid_update_target",
                )
            if parsed.get("scope_operations") or parsed.get("shadow_native_ids"):
                raise ModelOutputError(
                    "CREATE review summary cannot extend authorization",
                    validation_detail="invalid_evidence",
                )
            if _summary_date_grounding_violations(
                parsed,
                grounded_dates=grounded_dates,
                source_texts=[
                    event.get("content", "")
                    for event in projected
                    if event.get("role") in {"user", "assistant"}
                ],
                preserved_texts=(),
            ):
                raise ModelOutputError(
                    "CREATE review summary contains an ungrounded date",
                    validation_detail="relative_time",
                )
            cited = set(parsed.get("evidence_event_ids", []))
            for source in parsed.get("sources", []):
                if source.get("event_key"):
                    cited.add(source["event_key"])
                cited.update(source.get("evidence_event_ids", []))
            if not set(keys).issubset(cited):
                raise ModelOutputError(
                    "CREATE review summary omitted source evidence",
                    validation_detail="invalid_evidence",
                )
            return parsed

        return parse_create_summary

    def _review_final_updates(
        self,
        requests: list[dict[str, Any]],
        *,
        candidates: Mapping[str, Any],
        evidence_units: Any,
        events: list[dict[str, Any]],
        backend: Any,
        scope_registry: Any,
        validation_scope_registry: Any,
    ) -> list[dict[str, Any]]:
        """Review final automatic UPDATEs after same-target resolution."""

        slots: list[dict[str, Any]] = []
        jobs: list[Callable[[], dict[str, Any]]] = []
        for request in requests:
            if not self._is_reviewable_update(request):
                slots.append({"kind": "passthrough", "request": request})
                continue

            summary = request.get("summary")
            target_id = summary.get("update_memory_id") if isinstance(summary, Mapping) else None
            target = self.read_target(target_id) if isinstance(target_id, str) else None
            expected = request.get("expected_revision")
            if target is None:
                raise ProcessingError("update target disappeared before commit")
            if not isinstance(expected, str) or revision_digest(target) != expected:
                raise ProcessingError("update target changed before commit; no stale overwrite")

            projected = self._projected_request_evidence(
                request,
                candidates=candidates,
                evidence_units=evidence_units,
                events=events,
            )
            if not projected:
                slots.append({"kind": "defer", "request": request, "reason": "semantic_review_failed"})
                continue

            candidate_ids = self._request_candidate_ids(request)
            candidate = next(
                (
                    candidates[candidate_id]
                    for candidate_id in candidate_ids
                    if candidate_id in candidates and isinstance(candidates[candidate_id], Mapping)
                ),
                {},
            )
            turn = request.get("turn")
            if turn is None:
                slots.append({"kind": "defer", "request": request, "reason": "semantic_review_failed"})
                continue
            keys = tuple(dict.fromkeys(event["event_key"] for event in projected))
            target_type = target.get("type") if isinstance(target, Mapping) else getattr(target, "type", None)
            target_scopes = summary.get("scopes") if isinstance(summary, Mapping) else None
            if target_scopes is None:
                target_scopes = candidate.get("scopes")
            target_scope_source = summary.get("scope_source") if isinstance(summary, Mapping) else None
            if target_scope_source is None:
                target_scope_source = candidate.get("scope_source")
            if target_scope_source is None:
                target_scope_source = (
                    target.get("scope_source")
                    if isinstance(target, Mapping)
                    else getattr(target, "scope_source", None)
                )
            grounded_dates = _grounded_due_dates(turn, evidence_events=projected)
            parser = self._make_update_review_parser(
                turn=turn,
                candidate=candidate,
                keys=keys,
                target=target,
                target_id=target_id,
                target_type=target_type,
                target_scopes=target_scopes,
                target_scope_source=target_scope_source,
                grounded_dates=grounded_dates,
                validation_scope_registry=validation_scope_registry,
                projected=projected,
            )
            diagnostic_context = {
                "source": turn.source,
                "session_id": turn.session_id,
                "turn_index": turn.turn_index,
            }

            def run_review(
                *,
                target_value: Any = target,
                projected_value: list[dict[str, Any]] = projected,
                summary_value: Mapping[str, Any] = self._review_content(summary),
                parser_value: Callable[[Mapping[str, Any]], Mapping[str, Any]] = parser,
                diagnostic_value: Mapping[str, Any] = diagnostic_context,
            ) -> dict[str, Any]:
                return review_update(
                    self.model,
                    backend,
                    target=target_value,
                    admitted_source=projected_value,
                    proposed_summary=summary_value,
                    parse_summary=parser_value,
                    diagnostic_context=diagnostic_value,
                )

            job_index = len(jobs)
            jobs.append(run_review)
            slots.append({
                "kind": "review",
                "request": request,
                "summary": summary,
                "target_id": target_id,
                "job_index": job_index,
            })

        outcomes = self._run_review_jobs(jobs, backend=backend)
        reviewed: list[dict[str, Any]] = []
        for slot in slots:
            kind = slot["kind"]
            request = slot["request"]
            if kind == "passthrough":
                reviewed.append(request)
                continue
            if kind == "defer":
                self._defer_request(
                    request,
                    candidates=candidates,
                    reason=slot["reason"],
                )
                continue
            outcome = outcomes[slot["job_index"]]
            decision = outcome.get("decision")
            target_memory_id = slot["target_id"] if isinstance(slot["target_id"], str) else None
            if decision == "ACCEPT":
                reviewed.append(request)
                continue
            if decision == "REVISE" and isinstance(outcome.get("summary"), Mapping):
                revised = dict(request)
                revised["summary"] = self._reviewed_summary(slot["summary"], outcome["summary"])
                reviewed.append(revised)
                continue
            if decision == "NO_CHANGE":
                self.audit._record_request_disposition(
                    request,
                    "NO_CHANGE",
                    reason="update_semantic_review_no_change",
                    memory_id=target_memory_id,
                )
                continue
            self._defer_request(
                request,
                candidates=candidates,
                reason=(
                    outcome.get("reason")
                    if isinstance(outcome.get("reason"), str) and outcome.get("reason")
                    else "semantic_review_failed"
                ),
            )
        return reviewed

    def _review_final_creates(
        self,
        requests: list[dict[str, Any]],
        *,
        candidates: Mapping[str, Any],
        evidence_units: Any,
        events: list[dict[str, Any]],
        backend: Any,
        scope_registry: Any,
        validation_scope_registry: Any,
    ) -> list[dict[str, Any]]:
        """Review final ordinary automatic CREATEs before plan freeze."""

        slots: list[dict[str, Any]] = []
        jobs: list[Callable[[], dict[str, Any]]] = []
        for request in requests:
            if not self._is_reviewable_create(request):
                slots.append({"kind": "passthrough", "request": request})
                continue

            summary = request.get("summary")
            projected = self._projected_request_evidence(
                request,
                candidates=candidates,
                evidence_units=evidence_units,
                events=events,
            )
            if not projected:
                slots.append({"kind": "defer", "request": request, "reason": "semantic_review_failed"})
                continue
            candidate_ids = self._request_candidate_ids(request)
            candidate = next(
                (
                    candidates[candidate_id]
                    for candidate_id in candidate_ids
                    if candidate_id in candidates and isinstance(candidates[candidate_id], Mapping)
                ),
                {},
            )
            turn = request.get("turn")
            if turn is None or not isinstance(summary, Mapping):
                slots.append({"kind": "defer", "request": request, "reason": "semantic_review_failed"})
                continue
            keys = tuple(dict.fromkeys(event["event_key"] for event in projected))
            summary_type = summary.get("type")
            if not isinstance(summary_type, str):
                summary_type = candidate.get("type")
            summary_scopes = summary.get("scopes")
            if summary_scopes is None:
                summary_scopes = candidate.get("scopes")
            summary_scope_source = summary.get("scope_source")
            if summary_scope_source is None:
                summary_scope_source = candidate.get("scope_source")
            grounded_dates = _grounded_due_dates(turn, evidence_events=projected)
            parser = self._make_create_review_parser(
                turn=turn,
                candidate=candidate,
                keys=keys,
                summary_type=summary_type,
                summary_scopes=summary_scopes,
                summary_scope_source=summary_scope_source,
                grounded_dates=grounded_dates,
                validation_scope_registry=validation_scope_registry,
                projected=projected,
            )
            diagnostic_context = {
                "source": turn.source,
                "session_id": turn.session_id,
                "turn_index": turn.turn_index,
            }

            def run_review(
                *,
                projected_value: list[dict[str, Any]] = projected,
                summary_value: Mapping[str, Any] = self._review_content(summary),
                parser_value: Callable[[Mapping[str, Any]], Mapping[str, Any]] = parser,
                diagnostic_value: Mapping[str, Any] = diagnostic_context,
            ) -> dict[str, Any]:
                return review_create(
                    self.model,
                    backend,
                    admitted_source=projected_value,
                    proposed_summary=summary_value,
                    parse_summary=parser_value,
                    diagnostic_context=diagnostic_value,
                )

            job_index = len(jobs)
            jobs.append(run_review)
            slots.append({
                "kind": "review",
                "request": request,
                "summary": summary,
                "job_index": job_index,
            })

        outcomes = self._run_review_jobs(jobs, backend=backend)
        reviewed: list[dict[str, Any]] = []
        for slot in slots:
            kind = slot["kind"]
            request = slot["request"]
            if kind == "passthrough":
                reviewed.append(request)
                continue
            if kind == "defer":
                self._defer_request(
                    request,
                    candidates=candidates,
                    reason=slot["reason"],
                )
                continue
            outcome = outcomes[slot["job_index"]]
            decision = outcome.get("decision")
            if decision == "ACCEPT":
                reviewed.append(request)
                continue
            if decision == "REVISE" and isinstance(outcome.get("summary"), Mapping):
                revised = dict(request)
                revised["summary"] = self._reviewed_summary(slot["summary"], outcome["summary"])
                reviewed.append(revised)
                continue
            if decision == "NO_CHANGE":
                self.audit._record_request_disposition(
                    request,
                    "NO_CHANGE",
                    reason="create_semantic_review_no_change",
                    memory_id=(
                        request.get("memory_id")
                        if isinstance(request.get("memory_id"), str)
                        else None
                    ),
                )
                continue
            self._defer_request(
                request,
                candidates=candidates,
                reason=(
                    outcome.get("reason")
                    if isinstance(outcome.get("reason"), str) and outcome.get("reason")
                    else "semantic_review_failed"
                ),
            )
        return reviewed

    def _defer(self, group: list[dict[str, Any]], candidates: Mapping[str, Any], reason: str) -> None:
        for request in group:
            turn = request['turn']
            self.audit._defer_candidate((turn.source, turn.session_id, turn.turn_key),
                candidates[request['candidate_id']], reason)

    def _resolve_group(self, group: list[dict[str, Any]], *, candidates: Mapping[str, Any],
                       evidence_units: Any, events: list[dict[str, Any]], backend: Any,
                       scope_registry: Any, validation_scope_registry: Any) -> dict[str, Any] | None:
        first = group[0]
        turn = first['turn']
        target_id = first['summary']['update_memory_id']
        members = [candidates[r['candidate_id']] for r in group]
        ids = [r['candidate_id'] for r in group]
        expected = first.get('expected_revision')
        scopes = first['summary']['scopes']
        scope_source = first['summary'].get('scope_source')
        memory_type = first['summary']['type']
        # Special maintenance/retirement authorization cannot be inherited by a
        # normal update. Nor may compatible-looking text merge different scopes.
        if (len(group) > _MAX_GROUP_MEMBERS or not expected or any(
            r['turn'] != turn or r.get('explicit_remember') or r.get('scope_correction')
            or r.get('duplicate_memory_id') or r['summary'].get('scope_operations')
            or r['summary'].get('shadow_native_ids') or r.get('expected_revision') != expected
            or r['summary']['scopes'] != scopes or r['summary']['type'] != memory_type
            or r['summary'].get('scope_source') != scope_source for r in group)):
            self._defer(group, candidates, 'same_turn_target_conflict')
            return None
        target = self.read_target(target_id)
        if target is None or revision_digest(target) != expected:
            self._defer(group, candidates, 'target_revision_changed')
            return None

        # Preserve the original span order and section metadata. Multiple spans
        # may share an event key; that does not make them one synthetic quote.
        projected: list[dict[str, Any]] = []
        seen_spans: set[tuple[str, str, str]] = set()
        unit_order = {unit.unit_id: (i, unit.text) for i, unit in enumerate(evidence_units)}
        for member in members:
            for event in summary_evidence(member, evidence_units, events=events):
                identity = (event["event_key"], event["unit_id"], event["content"])
                if identity not in seen_spans:
                    seen_spans.add(identity)
                    projected.append(deepcopy(event))
        projected.sort(key=lambda event: (
            unit_order[event["unit_id"]][0],
            unit_order[event["unit_id"]][1].find(event["content"])))
        keys = tuple(dict.fromkeys(event["event_key"] for event in projected))
        if not keys:
            self._defer(group, candidates, 'evidence_not_supported')
            return None
        candidate = dict(members[0])
        candidate['memory'] = '\n'.join(m['memory'] for m in members)
        candidate['evidence_event_ids'] = list(keys)
        candidate['scopes'] = list(scopes)
        candidate['scope_source'] = scope_source
        candidate['update_memory_id'] = target_id
        prompt = summarize_prompt(candidate, projected,
            related_memories=[target.to_dict()], scope_registry=scope_registry)
        prompt += '\nSAME_TARGET_RECONCILIATION\n' + json.dumps({
            'candidate_ids': ids,
            'admitted_changes': [{'candidate_id': m['candidate_id'], 'memory': m['memory']} for m in members],
            'proposals': [{'candidate_id': r['candidate_id'], 'summary': r['summary']} for r in group],
        }, ensure_ascii=False, separators=(',', ':'))
        prompt += (
            '\nReconcile ALL admitted changes into ONE current memory for the supplied target. '
            'Proposed summaries are model output, not new source evidence. Preserve unaffected '
            'current facts. Do not concatenate contradictory proposals. A later explicit correction '
            'may supersede earlier evidence only when its order and meaning are clear. '
            'For unresolved contradictions return DEFERRED for the whole target group. '
            'This GROUP response contract replaces the single-summary response contract above. '
            'Return exactly one of: '
            '{"decision":"UPDATE","candidate_ids":[...],"summary":{...normal summary...}}, '
            '{"decision":"NO_CHANGE","candidate_ids":[...]}, or '
            '{"decision":"DEFERRED","candidate_ids":[...],"reason":"conflicting_changes"}. '
            'Account for every supplied candidate_id exactly once. An UPDATE must explicitly repeat '
            'update_memory_id and cite all supplied source event keys. Do not change scopes/type, '
            'introduce scope_operations, or add native-shadowing authorization.'
        )
        if len(prompt.encode('utf-8')) > _MAX_GROUP_BYTES:
            self._defer(group, candidates, 'same_turn_group_too_large')
            return None

        def parse(raw: str) -> dict[str, Any]:
            value = parse_strict_json(raw)
            if not isinstance(value, dict):
                raise ModelOutputError('group response must be an object', validation_detail='root_shape')
            selected = value.get('candidate_ids')
            if (not isinstance(selected, list) or any(not isinstance(v, str) for v in selected)
                or len(selected) != len(ids) or set(selected) != set(ids)):
                raise ModelOutputError('incomplete update group accounting', validation_detail='invalid_evidence')
            decision = value.get('decision')
            if decision == 'NO_CHANGE' and set(value) == {'decision', 'candidate_ids'}:
                return value
            if (decision == 'DEFERRED' and set(value) == {'decision', 'candidate_ids', 'reason'}
                and value['reason'] == 'conflicting_changes'):
                return value
            if decision != 'UPDATE' or set(value) != {'decision', 'candidate_ids', 'summary'}:
                raise ModelOutputError('invalid update group decision', validation_detail='unknown_fields')
            raw_summary = json.dumps(value['summary'], ensure_ascii=False)
            summary = parse_summarize_output(_normalize_summary_dates(raw_summary, turn, candidate),
                current_event_keys=keys, related_native_ids=[], related_memory_ids=[target_id],
                scope_registry=validation_scope_registry, expected_scopes=scopes,
                expected_scope_source=scope_source, expected_type=memory_type,
                expected_target_type=target.type, expected_update_memory_id=target_id,
                allowed_due_dates=_grounded_due_dates(
                    turn,
                    evidence_events=projected,
                ), allow_no_change=False)
            if _summary_date_grounding_violations(
                summary,
                grounded_dates=_grounded_due_dates(turn, evidence_events=projected),
                source_texts=[event.get('content', '') for event in projected if event.get('role') in {'user', 'tool'}],
                preserved_texts=(target.title, target.body, target.due_date),
            ):
                raise ModelOutputError('group summary contains an ungrounded date', validation_detail='relative_time')
            if summary.get('update_memory_id') != target_id:
                raise ModelOutputError('group must retain its target', validation_detail='invalid_update_target')
            if summary.get('scope_operations') or summary.get('shadow_native_ids'):
                raise ModelOutputError('group cannot extend authorization', validation_detail='invalid_evidence')
            cited = set(summary.get('evidence_event_ids', []))
            for source in summary['sources']:
                if source.get('event_key'):
                    cited.add(source['event_key'])
                cited.update(source.get('evidence_event_ids', []))
            if not set(keys).issubset(cited):
                raise ModelOutputError('group omitted source evidence', validation_detail='invalid_evidence')
            return {**value, 'summary': summary}

        try:
            outcome = self.model._complete_json_stage(backend, prompt, system=UPDATE_GROUP_SYSTEM,
                purpose='summarize', parser=parse,
                diagnostic_context={'source': turn.source, 'session_id': turn.session_id,
                                    'turn_index': turn.turn_index},
                metric_stage='target_reconciliation')
        except (ModelError, ModelOutputError):
            # No fragment wins after model failure, and unrelated targets remain
            # independently committable. The complete original turn is retained.
            self._defer(group, candidates, 'same_turn_reconciliation_failed')
            return None
        if outcome['decision'] == 'DEFERRED':
            self._defer(group, candidates, 'same_turn_target_conflict')
            return None
        if outcome['decision'] == 'NO_CHANGE':
            for member in members:
                self.audit._record_disposition((turn.source, turn.session_id, turn.turn_key),
                    member, 'NO_CHANGE', reason='group_no_change', memory_id=target_id)
            return None
        merged = dict(first)
        merged['summary'] = outcome['summary']
        merged['evidence_unit_ids'] = list(dict.fromkeys(
            uid for r in group for uid in r.get('evidence_unit_ids', [])))
        merged['contributing_candidates'] = [
            {'candidate_id': m['candidate_id'], 'evidence_unit_ids': list(m.get('evidence_unit_ids', []))}
            for m in members]
        self.audit._record_request_disposition(merged, 'UPDATE',
            reason='same_target_consolidated', memory_id=target_id)
        return merged
