"""Adapters for the isolated P4 responsibility-consolidation experiment.

The adapters translate already-computed Core lookup results into the explicit
P4 lookup-state protocol, delegate summary semantics to existing per-candidate
parsers, and compare P4 outcomes with a baseline disposition set. They do not
perform retrieval, call a model, mutate audit state, or write the Vault.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .maintenance_plan_protocol import MAINTENANCE_DECISIONS
from .validation import ModelOutputError


DEFAULT_MAX_LOCAL_TARGETS = 8
_TERMINAL_DECISIONS = frozenset({"CREATE", "UPDATE", "NO_CHANGE"})
_TARGET_DECISIONS = frozenset({"UPDATE", "NO_CHANGE"})


def _candidate_ids(candidates: Iterable[Mapping[str, Any]]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        if not isinstance(raw, Mapping):
            raise ModelOutputError("P4 adapter candidate must be an object", validation_detail="candidate_shape")
        candidate_id = raw.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in seen:
            raise ModelOutputError("P4 adapter requires unique candidate IDs", validation_detail="duplicate_candidate_id")
        seen.add(candidate_id)
        result.append(candidate_id)
    return result


def _local_active_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        if raw.get("native") is True or raw.get("active") is False or raw.get("area") == "history":
            continue
        memory_id = raw.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id or "/" in memory_id or "\\" in memory_id:
            continue
        key = memory_id.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(raw))
    return result


def _native_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping) or raw.get("native") is not True:
            continue
        native_id = raw.get("native_id")
        if not isinstance(native_id, str) or not native_id:
            continue
        key = native_id.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(raw))
    return result


def adapt_core_lookup_contexts(
    candidates: Iterable[Mapping[str, Any]],
    related_by_candidate: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    lookup_complete_by_candidate: Mapping[str, bool],
    search_error_candidate_ids: Iterable[str] = (),
    evidence_insufficient_candidate_ids: Iterable[str] = (),
    max_local_targets: int = DEFAULT_MAX_LOCAL_TARGETS,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
]:
    """Translate Core lookup outcomes into explicit P4 lookup states.

    `lookup_complete_by_candidate` is mandatory because the absence of returned
    rows is not proof that lookup was exhaustive. Search/evidence failures take
    precedence over completeness. More than `max_local_targets` is represented
    as `too_many_candidates`, never silently truncated and never CREATE-safe.
    """

    if type(max_local_targets) is not int or max_local_targets <= 0:
        raise ValueError("max_local_targets must be a positive integer")
    ids = _candidate_ids(candidates)
    if not isinstance(related_by_candidate, Mapping) or set(related_by_candidate) != set(ids):
        raise ModelOutputError("related lookup must cover every candidate", validation_detail="missing_fields")
    if not isinstance(lookup_complete_by_candidate, Mapping) or set(lookup_complete_by_candidate) != set(ids):
        raise ModelOutputError("lookup completeness must cover every candidate", validation_detail="missing_fields")
    if any(type(lookup_complete_by_candidate[candidate_id]) is not bool for candidate_id in ids):
        raise ModelOutputError("lookup completeness values must be boolean", validation_detail="other_schema_violation")

    search_errors = {value for value in search_error_candidate_ids if isinstance(value, str)}
    evidence_insufficient = {
        value for value in evidence_insufficient_candidate_ids if isinstance(value, str)
    }
    unknown_markers = (search_errors | evidence_insufficient) - set(ids)
    if unknown_markers:
        raise ModelOutputError("lookup failure markers contain unknown candidate", validation_detail="candidate_shape")

    states: dict[str, dict[str, Any]] = {}
    local_by_candidate: dict[str, list[dict[str, Any]]] = {}
    native_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for candidate_id in ids:
        rows = related_by_candidate[candidate_id]
        rows = list(rows) if not isinstance(rows, list) else rows
        local = _local_active_rows(rows)
        native = _native_rows(rows)
        local_by_candidate[candidate_id] = local
        native_by_candidate[candidate_id] = native
        target_ids = [row["memory_id"] for row in local]

        if candidate_id in search_errors:
            status = "search_error"
        elif candidate_id in evidence_insufficient:
            status = "evidence_insufficient"
        elif not lookup_complete_by_candidate[candidate_id] or len(target_ids) > max_local_targets:
            status = "too_many_candidates"
        elif target_ids:
            status = "complete_candidates"
        else:
            status = "complete_no_target"

        states[candidate_id] = {
            "status": status,
            # Never truncate a partial/large target set into a falsely complete
            # authorization set. In incomplete states these IDs are comparison
            # context only because the protocol forces DEFERRED.
            "allowed_target_memory_ids": list(target_ids),
        }
    return states, local_by_candidate, native_by_candidate


def make_existing_summary_validator(
    parser_factory: Callable[[str, str, str | None], Callable[[str], Mapping[str, Any]]],
) -> Callable[[str, str, str | None, Mapping[str, Any]], Mapping[str, Any]]:
    """Adapt existing per-candidate summary parsers to the P4 protocol hook.

    The factory receives `(candidate_id, decision, canonical_target)` so the
    caller can construct the same evidence/type/scope/date/target constraints
    it uses today. P4 serializes only the model-proposed summary and delegates
    all semantic validation to that parser.
    """

    if not callable(parser_factory):
        raise TypeError("parser_factory must be callable")

    def validate(
        candidate_id: str,
        decision: str,
        target: str | None,
        summary: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if decision not in {"CREATE", "UPDATE"}:
            raise ModelOutputError("only write decisions may validate a summary", validation_detail="candidate_shape")
        if decision == "CREATE" and target is not None:
            raise ModelOutputError("CREATE cannot carry a target", validation_detail="invalid_update_target")
        if decision == "UPDATE" and not isinstance(target, str):
            raise ModelOutputError("UPDATE requires a target", validation_detail="invalid_update_target")
        if decision == "UPDATE":
            summary_target = summary.get("update_memory_id")
            if (
                not isinstance(summary_target, str)
                or summary_target.casefold() != target.casefold()
            ):
                raise ModelOutputError(
                    "UPDATE summary must keep the canonical maintenance target",
                    validation_detail="invalid_update_target",
                )
        parser = parser_factory(candidate_id, decision, target)
        if not callable(parser):
            raise TypeError("parser_factory must return a callable parser")
        raw = json.dumps(dict(summary), ensure_ascii=False, separators=(",", ":"))
        parsed = parser(raw)
        if not isinstance(parsed, Mapping):
            raise ModelOutputError("existing summary parser returned invalid shape", validation_detail="candidate_shape")
        return dict(parsed)

    return validate


def _baseline_map(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ModelOutputError("baseline disposition must be an object", validation_detail="candidate_shape")
        candidate_id = raw.get("candidate_id")
        decision = raw.get("decision", raw.get("disposition"))
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in result:
            raise ModelOutputError("baseline candidate IDs must be unique", validation_detail="duplicate_candidate_id")
        if not isinstance(decision, str) or decision not in MAINTENANCE_DECISIONS:
            raise ModelOutputError("baseline decision is invalid", validation_detail="other_schema_violation")
        target = raw.get("target_memory_id", raw.get("memory_id")) if decision in _TARGET_DECISIONS else None
        if target is not None and not isinstance(target, str):
            raise ModelOutputError("baseline target is invalid", validation_detail="invalid_update_target")
        result[candidate_id] = {
            "candidate_id": candidate_id,
            "decision": decision,
            "target_memory_id": target,
        }
    return result


def compare_shadow_outcomes(
    baseline_rows: Iterable[Mapping[str, Any]],
    p4_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a no-write candidate/decision/target parity report."""

    baseline = _baseline_map(baseline_rows)
    p4: dict[str, dict[str, Any]] = {}
    for raw in p4_rows:
        if not isinstance(raw, Mapping):
            raise ModelOutputError("P4 outcome must be an object", validation_detail="candidate_shape")
        candidate_id = raw.get("candidate_id")
        decision = raw.get("decision")
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in p4:
            raise ModelOutputError("P4 outcome candidate IDs must be unique", validation_detail="duplicate_candidate_id")
        if not isinstance(decision, str) or decision not in MAINTENANCE_DECISIONS:
            raise ModelOutputError("P4 outcome decision is invalid", validation_detail="other_schema_violation")
        target = raw.get("target_memory_id") if decision in _TARGET_DECISIONS else None
        p4[candidate_id] = {
            "candidate_id": candidate_id,
            "decision": decision,
            "target_memory_id": target,
        }

    all_ids = sorted(set(baseline) | set(p4))
    differences: list[dict[str, Any]] = []
    for candidate_id in all_ids:
        left = baseline.get(candidate_id)
        right = p4.get(candidate_id)
        if left != right:
            differences.append({
                "candidate_id": candidate_id,
                "baseline": left,
                "p4": right,
            })
    return {
        "schema_version": 1,
        "baseline_candidate_count": len(baseline),
        "p4_candidate_count": len(p4),
        "candidate_set_equal": set(baseline) == set(p4),
        "decision_target_equal": not differences,
        "differences": differences,
        "note": "Shadow structural parity only; summaries, semantic truth and model quality require separate evaluation.",
    }


__all__ = [
    "DEFAULT_MAX_LOCAL_TARGETS",
    "adapt_core_lookup_contexts",
    "compare_shadow_outcomes",
    "make_existing_summary_validator",
]
