"""Reconcile same-turn CREATE proposals produced by independent Gate batches.

Gate batching protects model context capacity, but it can also produce two
different CREATE proposals for one future-use topic.  This coordinator asks the
model to classify only those bounded, same-scope CREATE proposals.  It never
uses string similarity to merge them and never writes to the Vault.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import json
from typing import Any, Mapping

from .admission import summary_evidence
from .llm import ModelError
from .process_common import _grounded_due_dates, _normalize_summary_dates
from .prompts import CREATE_GROUP_SYSTEM, create_group_prompt
from .validation import ModelOutputError, parse_strict_json, parse_summarize_output


_MAX_GROUP_MEMBERS = 32
_MAX_GROUP_BYTES = 128 * 1024
_CREATE_DEFER_REASON = "same_turn_create_reconciliation_failed"


class CreateCoordinator:
    """Resolve only cross-batch automatic CREATE request groups."""

    def __init__(self, model: Any, audit: Any):
        self.model = model
        self.audit = audit

    @staticmethod
    def _eligible(request: Mapping[str, Any]) -> bool:
        summary = request.get("summary")
        return (
            isinstance(summary, Mapping)
            and not request.get("explicit_remember")
            and not request.get("duplicate_memory_id")
            and not request.get("scope_correction")
            and not summary.get("scope_operations")
            and not summary.get("shadow_native_ids")
            and not isinstance(summary.get("update_memory_id"), str)
            and isinstance(request.get("_gate_batch_index"), int)
            and not isinstance(request.get("_gate_batch_index"), bool)
            and bool(request.get("evidence_unit_ids"))
            and isinstance(summary.get("type"), str)
            and isinstance(summary.get("scopes"), list)
            and isinstance(summary.get("scope_source"), str)
        )

    @staticmethod
    def _group_key(request: Mapping[str, Any]) -> tuple[Any, ...] | None:
        if not CreateCoordinator._eligible(request):
            return None
        summary = request["summary"]
        turn = request.get("turn")
        return (
            (
                turn.source,
                turn.session_id,
                turn.turn_key,
            ),
            summary.get("type"),
            tuple(summary.get("scopes", ())),
        )

    def resolve(
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
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = OrderedDict()
        for request in requests:
            key = self._group_key(request)
            if key is not None:
                groups.setdefault(key, []).append(request)

        replacements: dict[int, list[dict[str, Any]] | None] = {}
        for group in groups.values():
            batch_indexes = {
                request.get("_gate_batch_index")
                for request in group
                if isinstance(request.get("_gate_batch_index"), int)
                and not isinstance(request.get("_gate_batch_index"), bool)
            }
            # A single Gate call already had complete context and must not pay
            # for a second semantic reconciliation call.
            if len(group) < 2 or len(batch_indexes) < 2:
                continue
            resolved = self._resolve_group(
                group,
                candidates=candidates,
                evidence_units=evidence_units,
                events=events,
                backend=backend,
                scope_registry=scope_registry,
                validation_scope_registry=validation_scope_registry,
            )
            replacements[id(group[0])] = resolved
            for request in group[1:]:
                replacements[id(request)] = None

        result: list[dict[str, Any]] = []
        for request in requests:
            replacement = replacements.get(id(request), request)
            if replacement is None:
                continue
            if isinstance(replacement, list):
                result.extend(replacement)
            else:
                result.append(replacement)
        for request in result:
            request.pop("_gate_batch_index", None)
        return result

    def _defer(self, group: list[dict[str, Any]], candidates: Mapping[str, Any], reason: str) -> None:
        for request in group:
            candidate = candidates.get(request.get("candidate_id"))
            if isinstance(candidate, Mapping):
                self.audit._defer_candidate(
                    (
                        request["turn"].source,
                        request["turn"].session_id,
                        request["turn"].turn_key,
                    ),
                    candidate,
                    reason,
                    scopes=request.get("summary", {}).get("scopes"),
                )

    @staticmethod
    def _candidate_payload(
        request: Mapping[str, Any],
        candidate: Mapping[str, Any],
        evidence_units: Any,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "candidate_id": request["candidate_id"],
            "batch_index": request.get("_gate_batch_index"),
            "candidate": {
                key: candidate.get(key)
                for key in (
                    "memory",
                    "type",
                    "scopes",
                    "scope_source",
                    "evidence_event_ids",
                )
                if key in candidate
            },
            "summary": dict(request["summary"]),
            "evidence_unit_ids": list(request.get("evidence_unit_ids", ())),
            "evidence": summary_evidence(candidate, evidence_units, events=events),
        }

    def _resolve_group(
        self,
        group: list[dict[str, Any]],
        *,
        candidates: Mapping[str, Any],
        evidence_units: Any,
        events: list[dict[str, Any]],
        backend: Any,
        scope_registry: Any,
        validation_scope_registry: Any,
    ) -> list[dict[str, Any]] | None:
        first = group[0]
        turn = first["turn"]
        ids = [request["candidate_id"] for request in group]
        if len(group) > _MAX_GROUP_MEMBERS or any(
            request.get("turn") != turn or request.get("summary", {}).get("type") != first["summary"].get("type")
            or request.get("summary", {}).get("scopes") != first["summary"].get("scopes")
            for request in group
        ):
            self._defer(group, candidates, "same_turn_create_group_too_large")
            return None

        payloads = []
        for request in group:
            candidate = candidates.get(request["candidate_id"])
            if not isinstance(candidate, Mapping):
                self._defer(group, candidates, _CREATE_DEFER_REASON)
                return None
            payloads.append(self._candidate_payload(request, candidate, evidence_units, events))
        prompt = create_group_prompt(
            payloads,
            scope_registry=scope_registry,
        )
        if len(prompt.encode("utf-8")) > _MAX_GROUP_BYTES:
            self._defer(group, candidates, "same_turn_create_group_too_large")
            return None

        expected_type = first["summary"]["type"]
        expected_scopes = list(first["summary"]["scopes"])
        member_scope_sources = {
            request["summary"].get("scope_source")
            for request in group
            if isinstance(request.get("summary", {}).get("scope_source"), str)
        }
        payload_by_id = {payload["candidate_id"]: payload for payload in payloads}

        def parse(raw: str) -> dict[str, Any]:
            value = parse_strict_json(raw)
            if not isinstance(value, dict) or set(value) != {"groups"} or not isinstance(value["groups"], list):
                raise ModelOutputError("invalid CREATE group response", validation_detail="root_shape")
            seen: list[str] = []
            parsed_groups: list[dict[str, Any]] = []
            for row in value["groups"]:
                if not isinstance(row, dict):
                    raise ModelOutputError("invalid CREATE group row", validation_detail="schema_violation")
                decision = row.get("decision")
                member_ids = row.get("candidate_ids")
                if (
                    decision not in {"MERGE", "KEEP_DISTINCT", "DEFERRED"}
                    or not isinstance(member_ids, list)
                    or not member_ids
                    or any(not isinstance(member_id, str) or member_id not in ids for member_id in member_ids)
                    or len(set(member_ids)) != len(member_ids)
                    or any(member_id in seen for member_id in member_ids)
                ):
                    raise ModelOutputError("invalid CREATE group membership", validation_detail="invalid_evidence")
                seen.extend(member_ids)
                if decision == "MERGE":
                    if len(member_ids) < 2 or set(row) != {"decision", "candidate_ids", "summary"}:
                        raise ModelOutputError("invalid CREATE merge group", validation_detail="schema_violation")
                    source_keys = tuple(dict.fromkeys(
                        event["event_key"]
                        for member_id in member_ids
                        for event in payload_by_id[member_id]["evidence"]
                        if isinstance(event, Mapping) and isinstance(event.get("event_key"), str)
                    ))
                    if not source_keys:
                        raise ModelOutputError("CREATE merge has no source evidence", validation_detail="invalid_evidence")
                    admitted_group_evidence = [
                        event
                        for member_id in member_ids
                        for event in payload_by_id[member_id]["evidence"]
                    ]
                    summary = parse_summarize_output(
                        _normalize_summary_dates(
                            json.dumps(row["summary"], ensure_ascii=False),
                            turn,
                            candidates[member_ids[0]],
                        ),
                        current_event_keys=source_keys,
                        related_native_ids=[],
                        related_memory_ids=[],
                        scope_registry=validation_scope_registry,
                        expected_type=expected_type,
                        expected_scopes=expected_scopes,
                        expected_scope_source=None,
                        allowed_due_dates=_grounded_due_dates(
                            turn,
                            evidence_events=admitted_group_evidence,
                        ),
                        allow_no_change=False,
                        allow_update_target=False,
                    )
                    if not _summary_cites_all(summary, source_keys):
                        raise ModelOutputError("CREATE merge omitted source evidence", validation_detail="invalid_evidence")
                    summary_scope_source = summary.get("scope_source")
                    if summary_scope_source is None:
                        summary = dict(summary)
                        summary["scope_source"] = first["summary"]["scope_source"]
                        summary_scope_source = summary["scope_source"]
                    if summary_scope_source not in member_scope_sources:
                        raise ModelOutputError(
                            "CREATE merge changed scope source",
                            validation_detail="scope_drift",
                        )
                    if summary.get("scope_operations") or summary.get("shadow_native_ids"):
                        raise ModelOutputError(
                            "CREATE merge cannot extend authorization",
                            validation_detail="invalid_evidence",
                        )
                    parsed_groups.append({"decision": decision, "candidate_ids": member_ids, "summary": summary})
                elif decision == "KEEP_DISTINCT":
                    if set(row) != {"decision", "candidate_ids"}:
                        raise ModelOutputError("invalid KEEP_DISTINCT group", validation_detail="schema_violation")
                    parsed_groups.append({"decision": decision, "candidate_ids": member_ids})
                else:
                    if set(row) != {"decision", "candidate_ids", "reason"} or row["reason"] != "ambiguous_create_group":
                        raise ModelOutputError("invalid deferred CREATE group", validation_detail="schema_violation")
                    parsed_groups.append({"decision": decision, "candidate_ids": member_ids, "reason": row["reason"]})
            if len(seen) != len(ids) or set(seen) != set(ids):
                raise ModelOutputError("CREATE group does not cover every candidate", validation_detail="invalid_evidence")
            return {"groups": parsed_groups}

        try:
            outcome = self.model._complete_json_stage(
                backend,
                prompt,
                system=CREATE_GROUP_SYSTEM,
                purpose="summarize",
                parser=parse,
                diagnostic_context={
                    "source": turn.source,
                    "session_id": turn.session_id,
                    "turn_index": turn.turn_index,
                },
            )
        except (ModelError, ModelOutputError):
            self._defer(group, candidates, _CREATE_DEFER_REASON)
            return None

        deferred_groups = [row for row in outcome["groups"] if row["decision"] == "DEFERRED"]
        for row in deferred_groups:
            selected = [request for request in group if request["candidate_id"] in row["candidate_ids"]]
            self._defer(selected, candidates, "same_turn_create_group_deferred")

        resolved: list[dict[str, Any]] = []
        for row in outcome["groups"]:
            if row["decision"] == "KEEP_DISTINCT":
                resolved.extend(
                    request
                    for request in group
                    if request["candidate_id"] in row["candidate_ids"]
                )
                continue
            if row["decision"] != "MERGE":
                continue
            merged_ids = set(row["candidate_ids"])
            merged_requests = [
                request for request in group if request["candidate_id"] in merged_ids
            ]
            if len(merged_requests) != len(row["candidate_ids"]):
                self._defer(group, candidates, _CREATE_DEFER_REASON)
                return None
            merged = deepcopy(merged_requests[0])
            merged["summary"] = row["summary"]
            merged["evidence_unit_ids"] = list(dict.fromkeys(
                unit_id
                for request in merged_requests
                for unit_id in request.get("evidence_unit_ids", ())
            ))
            merged["contributing_candidates"] = [
                {
                    "candidate_id": request["candidate_id"],
                    "evidence_unit_ids": list(request.get("evidence_unit_ids", ())),
                }
                for request in merged_requests
            ]
            merged.pop("_gate_batch_index", None)
            self.audit._record_request_disposition(
                merged,
                "CREATE",
                reason="same_turn_create_consolidated",
                memory_id=merged.get("memory_id"),
            )
            resolved.append(merged)
        return resolved


def _summary_cites_all(summary: Mapping[str, Any], event_keys: tuple[str, ...]) -> bool:
    cited: set[str] = set()
    for source in summary.get("sources", ()):
        if not isinstance(source, Mapping):
            continue
        event_key = source.get("event_key")
        if isinstance(event_key, str):
            cited.add(event_key)
        for value in source.get("evidence_event_ids", ()):
            if isinstance(value, str):
                cited.add(value)
    for value in summary.get("evidence_event_ids", ()):
        if isinstance(value, str):
            cited.add(value)
    return set(event_keys).issubset(cited)


__all__ = ["CreateCoordinator"]
