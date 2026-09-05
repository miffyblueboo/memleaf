"""Reconcile same-turn updates before freezing a single mutation per target.

Only the model decides whether admitted changes are compatible. This module
checks identities, evidence, revisions and complete group accounting; it never
concatenates proposed bodies into a memory or writes to the Vault.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import json
from typing import Any, Callable, Mapping

from .admission import summary_evidence
from .llm import ModelError
from .prompts import UPDATE_GROUP_SYSTEM, summarize_prompt
from .process_common import _grounded_due_dates, _normalize_summary_dates
from .turn_plan import revision_digest
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
        return [replacements.get(id(request), request) for request in requests
                if replacements.get(id(request), request) is not None]

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
                allowed_due_dates=_grounded_due_dates(turn), allow_no_change=False)
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
                                    'turn_index': turn.turn_index})
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
