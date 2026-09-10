"""Versioned P4 maintenance-plan protocol validation.

This module is intentionally side-effect free. It owns only the internal
association and decision envelope used by the P4 responsibility-consolidation
experiment. Vault lookup, evidence validation, summary semantics, audit, and
writing remain owned by their existing components.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .validation import ModelOutputError, parse_strict_json


PROTOCOL_VERSION = "p4-maintenance-plan-v1"
MAINTENANCE_DECISIONS = frozenset({"CREATE", "UPDATE", "NO_CHANGE", "DEFERRED"})
LOOKUP_STATUSES = frozenset({
    "complete_no_target",
    "complete_candidates",
    "too_many_candidates",
    "search_error",
    "evidence_insufficient",
})
INCOMPLETE_LOOKUP_STATUSES = frozenset({
    "too_many_candidates",
    "search_error",
    "evidence_insufficient",
})
DEFER_REASONS = frozenset({
    "target_ambiguous",
    "lookup_incomplete",
    "lookup_failed",
    "evidence_insufficient",
    "maintenance_uncertain",
})

_ENVELOPE_FIELDS = frozenset({"protocol_version", "items"})
_LOOKUP_FIELDS = frozenset({"status", "allowed_target_memory_ids"})
_DECISION_FIELDS = {
    "CREATE": frozenset({"candidate_id", "decision", "summary"}),
    "UPDATE": frozenset({"candidate_id", "decision", "target_memory_id", "summary"}),
    "NO_CHANGE": frozenset({"candidate_id", "decision", "target_memory_id"}),
    "DEFERRED": frozenset({"candidate_id", "decision", "reason"}),
}
_INCOMPLETE_DEFER_REASONS = {
    "too_many_candidates": frozenset({"target_ambiguous", "lookup_incomplete"}),
    "search_error": frozenset({"lookup_failed"}),
    "evidence_insufficient": frozenset({"evidence_insufficient"}),
}
_MAX_CANDIDATES = 256
_MAX_ID_LENGTH = 512

SummaryValidator = Callable[
    [str, str, str | None, Mapping[str, Any]],
    Mapping[str, Any],
]


def _id(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_ID_LENGTH:
        raise ModelOutputError(
            f"{field} must be a bounded non-empty string",
            validation_detail="other_schema_violation",
        )
    return value


def _candidate_order(candidate_ids: Iterable[Any]) -> list[str]:
    if isinstance(candidate_ids, (str, bytes)):
        raise ModelOutputError("candidate_ids must be an iterable of IDs", validation_detail="root_shape")
    result: list[str] = []
    seen: set[str] = set()
    for raw in candidate_ids:
        candidate_id = _id(raw, field="candidate_id")
        if candidate_id in seen:
            raise ModelOutputError("duplicate candidate_id", validation_detail="duplicate_candidate_id")
        seen.add(candidate_id)
        result.append(candidate_id)
    if len(result) > _MAX_CANDIDATES:
        raise ModelOutputError("too many maintenance candidates", validation_detail="other_schema_violation")
    return result


def validate_lookup_states(
    candidate_ids: Iterable[Any],
    lookup_states: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Validate Core-produced lookup state for every candidate.

    A complete lookup may have either no natural target or a bounded set of
    candidates. Incomplete lookup states deliberately authorize no terminal
    maintenance decision; the model must defer instead of guessing CREATE or a
    target mutation.
    """

    order = _candidate_order(candidate_ids)
    if not isinstance(lookup_states, Mapping) or set(lookup_states) != set(order):
        raise ModelOutputError("lookup state must cover every candidate exactly", validation_detail="missing_fields")

    normalized: dict[str, dict[str, Any]] = {}
    for candidate_id in order:
        raw = lookup_states[candidate_id]
        if not isinstance(raw, Mapping) or set(raw) != _LOOKUP_FIELDS:
            raise ModelOutputError("invalid lookup state shape", validation_detail="other_schema_violation")
        status = raw.get("status")
        if not isinstance(status, str) or status not in LOOKUP_STATUSES:
            raise ModelOutputError("invalid lookup status", validation_detail="other_schema_violation")
        targets = raw.get("allowed_target_memory_ids")
        if not isinstance(targets, list):
            raise ModelOutputError("allowed target IDs must be a list", validation_detail="invalid_update_target")
        canonical_targets: list[str] = []
        target_keys: set[str] = set()
        for target_raw in targets:
            target = _id(target_raw, field="target_memory_id")
            key = target.casefold()
            if key in target_keys:
                raise ModelOutputError("duplicate allowed target ID", validation_detail="invalid_update_target")
            target_keys.add(key)
            canonical_targets.append(target)
        if status == "complete_no_target" and canonical_targets:
            raise ModelOutputError("complete_no_target cannot expose targets", validation_detail="invalid_update_target")
        if status == "complete_candidates" and not canonical_targets:
            raise ModelOutputError("complete_candidates requires at least one target", validation_detail="invalid_update_target")
        normalized[candidate_id] = {
            "status": status,
            "allowed_target_memory_ids": canonical_targets,
        }
    return normalized


def _canonical_target(raw: Any, lookup: Mapping[str, Any]) -> str:
    target = _id(raw, field="target_memory_id")
    by_key = {
        item.casefold(): item
        for item in lookup.get("allowed_target_memory_ids", [])
        if isinstance(item, str)
    }
    canonical = by_key.get(target.casefold())
    if canonical is None:
        raise ModelOutputError("maintenance target is not authorized by lookup", validation_detail="invalid_update_target")
    return canonical


def _validate_deferred_reason(reason: Any, lookup_status: str) -> str:
    if not isinstance(reason, str) or reason not in DEFER_REASONS:
        raise ModelOutputError("invalid maintenance defer reason", validation_detail="reason_too_long")
    allowed = _INCOMPLETE_DEFER_REASONS.get(lookup_status)
    if allowed is not None and reason not in allowed:
        raise ModelOutputError("defer reason does not match lookup state", validation_detail="other_schema_violation")
    return reason


def parse_maintenance_plan_output(
    raw: str,
    *,
    candidate_ids: Iterable[Any],
    lookup_states: Mapping[str, Mapping[str, Any]],
    validate_summary: SummaryValidator,
) -> list[dict[str, Any]]:
    """Parse one complete P4 maintenance-plan envelope.

    Results are returned in the original candidate order, independent of model
    row order. `validate_summary` remains the existing summary/evidence
    authority and receives candidate ID, decision, canonical target (if any),
    and the raw summary object for that row.
    """

    if not callable(validate_summary):
        raise TypeError("validate_summary must be callable")
    order = _candidate_order(candidate_ids)
    lookups = validate_lookup_states(order, lookup_states)
    value = parse_strict_json(raw)
    if not isinstance(value, Mapping) or set(value) != _ENVELOPE_FIELDS:
        raise ModelOutputError("invalid maintenance-plan envelope", validation_detail="root_shape")
    if value.get("protocol_version") != PROTOCOL_VERSION:
        raise ModelOutputError("unsupported maintenance-plan protocol", validation_detail="other_schema_violation")
    rows = value.get("items")
    if not isinstance(rows, list):
        raise ModelOutputError("maintenance-plan items must be a list", validation_detail="root_shape")

    expected = set(order)
    resolved: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ModelOutputError("maintenance-plan item must be an object", validation_detail="candidate_shape")
        candidate_id = _id(row.get("candidate_id"), field="candidate_id")
        if candidate_id not in expected:
            raise ModelOutputError("unknown maintenance candidate", validation_detail="candidate_shape")
        if candidate_id in resolved:
            raise ModelOutputError("duplicate maintenance candidate", validation_detail="duplicate_candidate_id")
        decision = row.get("decision")
        if not isinstance(decision, str) or decision not in MAINTENANCE_DECISIONS:
            raise ModelOutputError("invalid maintenance decision", validation_detail="other_schema_violation")
        if set(row) != _DECISION_FIELDS[decision]:
            detail = "unknown_fields" if set(row) - _DECISION_FIELDS[decision] else "missing_fields"
            raise ModelOutputError("maintenance item fields do not match decision", validation_detail=detail)

        lookup = lookups[candidate_id]
        lookup_status = lookup["status"]
        if lookup_status in INCOMPLETE_LOOKUP_STATUSES and decision != "DEFERRED":
            raise ModelOutputError(
                "incomplete lookup cannot authorize a terminal maintenance decision",
                validation_detail="other_schema_violation",
            )
        if lookup_status == "complete_no_target" and decision in {"UPDATE", "NO_CHANGE"}:
            raise ModelOutputError("lookup found no authorized target", validation_detail="invalid_update_target")

        normalized: dict[str, Any] = {
            "candidate_id": candidate_id,
            "decision": decision,
        }
        target: str | None = None
        if decision in {"UPDATE", "NO_CHANGE"}:
            target = _canonical_target(row.get("target_memory_id"), lookup)
            normalized["target_memory_id"] = target
        if decision in {"CREATE", "UPDATE"}:
            summary = row.get("summary")
            if not isinstance(summary, Mapping):
                raise ModelOutputError("maintenance write requires a summary", validation_detail="candidate_shape")
            validated = validate_summary(candidate_id, decision, target, summary)
            if not isinstance(validated, Mapping):
                raise ModelOutputError("summary validator returned invalid shape", validation_detail="candidate_shape")
            normalized["summary"] = dict(validated)
        elif decision == "DEFERRED":
            normalized["reason"] = _validate_deferred_reason(row.get("reason"), lookup_status)
        resolved[candidate_id] = normalized

    if set(resolved) != expected or len(resolved) != len(order):
        raise ModelOutputError("maintenance plan did not cover every candidate", validation_detail="missing_fields")
    return [resolved[candidate_id] for candidate_id in order]


__all__ = [
    "DEFER_REASONS",
    "INCOMPLETE_LOOKUP_STATUSES",
    "LOOKUP_STATUSES",
    "MAINTENANCE_DECISIONS",
    "PROTOCOL_VERSION",
    "parse_maintenance_plan_output",
    "validate_lookup_states",
]
