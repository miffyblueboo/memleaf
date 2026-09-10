"""Single-pass memory planning for the B3 experiment.

The model receives current admitted evidence plus bounded existing-memory
context once and returns final CREATE/UPDATE/NO_CHANGE/DEFERRED actions.  Core
keeps ownership of source binding, target authorization, schema validation and
writing.  This module has no Vault/filesystem side effects.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .admission import validate_bindings
from .validation import MEMORY_TYPES, SCOPE_SOURCES, ModelOutputError, parse_strict_json


PROTOCOL_VERSION = "b3-single-pass-v1"
MAX_PROMPT_BYTES = 192 * 1024

_DECISIONS = frozenset({"CREATE", "UPDATE", "NO_CHANGE", "DEFERRED"})
_NO_MEMORY_REASONS = frozenset({
    "query_only",
    "assistant_restatement",
    "retrieved_memory_only",
    "no_future_value",
    "quoted_or_example",
    "negated",
})
_DEFER_REASONS = frozenset({
    "target_ambiguous",
    "scope_ambiguous",
    "ownership_ambiguous",
    "evidence_insufficient",
    "lookup_incomplete",
    "maintenance_uncertain",
})
_MEMORY_FIELDS = frozenset({
    "title", "body", "tags", "aliases", "keywords", "status", "completed_at", "due_date"
})
_LOCAL_FIELDS = (
    "memory_id", "title", "body", "type", "scopes", "status", "completed_at", "due_date"
)
_NATIVE_FIELDS = ("native_id", "source", "title", "body", "content", "scopes")
_EVIDENCE_FIELDS = (
    "unit_id", "event_key", "timestamp", "role", "content", "origin", "section_path"
)

SINGLE_PASS_SYSTEM = """You are memleaf's single-pass memory planner. Apply the rules directly and return strict JSON only; never output reasoning, commentary, markdown, or intermediate analysis.

INPUT AUTHORITY
Only CURRENT_EVIDENCE authorizes new facts or state changes. LOCAL_MEMORY_CATALOG and NATIVE_MEMORY_CATALOG are old comparison context, never new evidence. Native memory can never be an UPDATE/NO_CHANGE target. Do not invent facts, ownership, dates, status, obligations, numeric meaning, project relationships, IDs, or source text.

ONE-PASS JOB
In this one response: identify every independently useful long-term memory in the current evidence, decide CREATE/UPDATE/NO_CHANGE/DEFERRED, and write final content for CREATE/UPDATE. Do not create a separate admission result, candidate proposal, summary draft, or review. One item is one independently retrievable/updateable future-use topic. Preserve subject/entity, condition, polarity, uncertainty, ownership, state, and important numbers/codes when required for meaning.

DECISIONS
CREATE: only for durable information not already represented by a supplied local memory, and only when input create_allowed=true. UPDATE: one supplied local memory represents the same evolving future use and current evidence establishes a real change. NO_CHANGE: current evidence is a duplicate/restatement or establishes no semantic change to that supplied target. DEFERRED: a durable candidate exists but target/scope/ownership/evidence is unsafe to decide. UPDATE/NO_CHANGE target_memory_id must be copied from LOCAL_MEMORY_CATALOG. One target may be used by at most one item.

CONTENT
CREATE supplies type, scopes, scope_source and memory. UPDATE supplies target_memory_id and memory; Core inherits immutable type/scopes from the target. memory contains title, body, tags and only optional aliases, keywords, status, completed_at, due_date. Do not emit type/scopes/sources/update_memory_id inside memory; Core supplies deterministic metadata and source references after validating evidence. Omission from current evidence is not retraction or completion. For an UPDATE, preserve still-valid target content unless current evidence supersedes it.

EVIDENCE
Every item must carry one or more exact evidence claims. A claim is exactly {unit_id,quote,role}, {unit_id,whole_unit:true,role}, or the legacy exact-offset form adding start/end. role is assertion, source_excerpt, or user_confirmation. Claims must use CURRENT_EVIDENCE only. Every current evidence unit must be either claimed by at least one item or appear exactly once in no_memory; never both. no_memory is only for evidence with no durable memory action and uses one of the supplied no-memory reasons.

SCOPE/DATES
CREATE scopes must be global, domain:name, portfolio:name, project:name, or unscoped using the existing memleaf semantics. Project ownership must be grounded by the claimed current evidence or explicit supplied scope; implementation platform/system names alone do not establish project ownership. Do not invent dates; preserve supported date meaning. Core performs final scope/date/target/revision validation.

OUTPUT
Return exactly {protocol_version,items,no_memory}. protocol_version is b3-single-pass-v1. Cover all CURRENT_EVIDENCE units. Return JSON only."""


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 24:
        raise ModelOutputError("B3 input is too deeply nested", validation_detail="other_schema_violation")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ModelOutputError("B3 input contains non-finite number", validation_detail="other_schema_violation")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, (str, int)) for key in value):
            raise ModelOutputError("B3 input contains invalid key", validation_detail="other_schema_violation")
        return {str(key): _json_safe(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in value]
    raise ModelOutputError("B3 input contains unsupported value", validation_detail="other_schema_violation")


def _evidence_projection(units: Iterable[Any]) -> tuple[list[dict[str, Any]], tuple[Any, ...]]:
    source_units = tuple(units)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for unit in source_units:
        if isinstance(unit, Mapping):
            raw = unit
            can_support = raw.get("role") in {"user", "assistant"}
        else:
            raw = {
                "unit_id": getattr(unit, "unit_id", None),
                "event_key": getattr(unit, "event_key", None),
                "timestamp": getattr(unit, "timestamp", None),
                "role": getattr(unit, "source_role", None),
                "content": getattr(unit, "text", None),
                "origin": getattr(unit, "origin", None),
                "section_path": list(getattr(unit, "section_path", ()) or ()),
            }
            can_support = getattr(unit, "can_support", False) is True
        unit_id = raw.get("unit_id")
        event_key = raw.get("event_key")
        role = raw.get("role")
        content = raw.get("content")
        if not isinstance(unit_id, str) or not unit_id or unit_id in seen:
            raise ModelOutputError("B3 evidence requires unique unit_id", validation_detail="invalid_evidence")
        if not isinstance(event_key, str) or not event_key:
            raise ModelOutputError("B3 evidence requires event_key", validation_detail="invalid_evidence")
        if role not in {"user", "assistant"} or not can_support:
            raise ModelOutputError("B3 evidence must be visible conversation content", validation_detail="invalid_evidence")
        if not isinstance(content, str) or not content:
            raise ModelOutputError("B3 evidence requires content", validation_detail="invalid_evidence")
        seen.add(unit_id)
        result.append({
            field: _json_safe(raw[field])
            for field in _EVIDENCE_FIELDS
            if field in raw and raw[field] is not None
        })
    if not result:
        raise ModelOutputError("B3 requires current evidence", validation_detail="invalid_evidence")
    return result, source_units


def _local_catalog(rows: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    projected: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping) or raw.get("native") is True:
            continue
        memory_id = raw.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id or "/" in memory_id or "\\" in memory_id:
            continue
        key = memory_id.casefold()
        if key in by_key:
            continue
        value = {
            field: _json_safe(raw[field])
            for field in _LOCAL_FIELDS
            if field in raw
        }
        if not isinstance(value.get("type"), str) or value.get("type") not in MEMORY_TYPES:
            continue
        if not isinstance(value.get("scopes"), list) or not value["scopes"]:
            continue
        by_key[key] = value
        projected.append(value)
    return projected, by_key


def _native_catalog(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        native_id = raw.get("native_id")
        if not isinstance(native_id, str) or not native_id:
            continue
        key = native_id.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append({
            field: _json_safe(raw[field])
            for field in _NATIVE_FIELDS
            if field in raw
        })
    return result


def build_single_pass_prompt(
    *,
    evidence_units: Iterable[Any],
    related_memories: Iterable[Mapping[str, Any]] = (),
    native_memories: Iterable[Mapping[str, Any]] = (),
    scope_background: Any = None,
    scope_registry: Any = None,
    create_allowed: bool = True,
) -> tuple[str, tuple[Any, ...], dict[str, dict[str, Any]]]:
    if type(create_allowed) is not bool:
        raise TypeError("create_allowed must be boolean")
    evidence, source_units = _evidence_projection(evidence_units)
    local, local_by_key = _local_catalog(related_memories)
    native = _native_catalog(native_memories)
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "create_allowed": create_allowed,
        "current_evidence": evidence,
        "local_memory_catalog": local,
        "native_memory_catalog": native,
        "scope_background": _json_safe(scope_background if scope_background is not None else []),
        "scope_registry": _json_safe(scope_registry if scope_registry is not None else []),
        "no_memory_reasons": sorted(_NO_MEMORY_REASONS),
        "defer_reasons": sorted(_DEFER_REASONS),
    }
    prompt = "B3_INPUT\n" + json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\nReturn the complete strict B3 envelope."
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ModelOutputError("B3 prompt exceeds budget", validation_detail="other_schema_violation")
    return prompt, source_units, local_by_key


def _memory_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelOutputError("B3 write requires memory object", validation_detail="candidate_shape")
    unknown = set(value) - _MEMORY_FIELDS
    required = {"title", "body", "tags"}
    if unknown or not required.issubset(value):
        raise ModelOutputError(
            "B3 memory fields are invalid",
            validation_detail="unknown_fields" if unknown else "missing_fields",
        )
    return dict(value)


def _canonical_target(raw: Any, local_by_key: Mapping[str, Mapping[str, Any]]) -> tuple[str, Mapping[str, Any]]:
    if not isinstance(raw, str) or not raw or "/" in raw or "\\" in raw:
        raise ModelOutputError("invalid B3 target", validation_detail="invalid_update_target")
    target = local_by_key.get(raw.casefold())
    if not isinstance(target, Mapping) or not isinstance(target.get("memory_id"), str):
        raise ModelOutputError("B3 target is not in local catalog", validation_detail="invalid_update_target")
    return target["memory_id"], target


MemoryValidator = Callable[[str, str, str | None, Mapping[str, Any] | None, Mapping[str, Any], list[dict[str, Any]]], Mapping[str, Any]]


def parse_single_pass_output(
    raw: str,
    *,
    evidence_units: Iterable[Any],
    local_memories: Iterable[Mapping[str, Any]],
    create_allowed: bool,
    validate_memory: MemoryValidator,
) -> dict[str, Any]:
    if not callable(validate_memory):
        raise TypeError("validate_memory must be callable")
    _, source_units = _evidence_projection(evidence_units)
    _, local_by_key = _local_catalog(local_memories)
    value = parse_strict_json(raw)
    if not isinstance(value, Mapping) or set(value) != {"protocol_version", "items", "no_memory"}:
        raise ModelOutputError("invalid B3 envelope", validation_detail="root_shape")
    if value.get("protocol_version") != PROTOCOL_VERSION:
        raise ModelOutputError("unsupported B3 protocol", validation_detail="other_schema_violation")
    items = value.get("items")
    no_memory = value.get("no_memory")
    if not isinstance(items, list) or not isinstance(no_memory, list):
        raise ModelOutputError("B3 arrays are invalid", validation_detail="root_shape")

    unit_ids = {getattr(unit, "unit_id", None) if not isinstance(unit, Mapping) else unit.get("unit_id") for unit in source_units}
    unit_ids = {item for item in unit_ids if isinstance(item, str)}
    candidate_ids: set[str] = set()
    used_targets: set[str] = set()
    binding_rows: list[dict[str, Any]] = []
    prepared: list[tuple[dict[str, Any], Mapping[str, Any] | None]] = []

    common = {"candidate_id", "decision", "evidence"}
    decision_fields = {
        "CREATE": common | {"type", "scopes", "scope_source", "memory"},
        "UPDATE": common | {"target_memory_id", "memory"},
        "NO_CHANGE": common | {"target_memory_id"},
        "DEFERRED": common | {"reason"},
    }
    for raw_item in items:
        if not isinstance(raw_item, Mapping):
            raise ModelOutputError("B3 item must be object", validation_detail="candidate_shape")
        candidate_id = raw_item.get("candidate_id")
        decision = raw_item.get("decision")
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in candidate_ids:
            raise ModelOutputError("B3 candidate_id is invalid", validation_detail="duplicate_candidate_id")
        if not isinstance(decision, str) or decision not in _DECISIONS:
            raise ModelOutputError("B3 decision is invalid", validation_detail="other_schema_violation")
        if set(raw_item) != decision_fields[decision]:
            raise ModelOutputError("B3 item fields do not match decision", validation_detail="unknown_fields")
        candidate_ids.add(candidate_id)
        claims = raw_item.get("evidence")
        if not isinstance(claims, list) or not claims:
            raise ModelOutputError("B3 item requires evidence", validation_detail="invalid_evidence")
        binding_rows.append({"candidate_id": candidate_id, "claims": claims})

        target_record: Mapping[str, Any] | None = None
        item = dict(raw_item)
        if decision == "CREATE":
            if not create_allowed:
                raise ModelOutputError("B3 CREATE is forbidden by lookup state", validation_detail="other_schema_violation")
            if item.get("type") not in MEMORY_TYPES:
                raise ModelOutputError("B3 CREATE type is invalid", validation_detail="invalid_type")
            scopes = item.get("scopes")
            if not isinstance(scopes, list) or not scopes or not all(isinstance(scope, str) and scope for scope in scopes):
                raise ModelOutputError("B3 CREATE scopes are invalid", validation_detail="invalid_scope")
            if item.get("scope_source") not in SCOPE_SOURCES:
                raise ModelOutputError("B3 CREATE scope_source is invalid", validation_detail="invalid_scope_source")
            item["memory"] = _memory_object(item.get("memory"))
        elif decision in {"UPDATE", "NO_CHANGE"}:
            canonical, target_record = _canonical_target(item.get("target_memory_id"), local_by_key)
            target_key = canonical.casefold()
            if target_key in used_targets:
                raise ModelOutputError("B3 target referenced more than once", validation_detail="duplicate_update_target")
            used_targets.add(target_key)
            item["target_memory_id"] = canonical
            if decision == "UPDATE":
                item["memory"] = _memory_object(item.get("memory"))
        else:
            if item.get("reason") not in _DEFER_REASONS:
                raise ModelOutputError("B3 defer reason is invalid", validation_detail="reason_too_long")
        prepared.append((item, target_record))

    # Reuse the established exact quote/whole-unit authority checker instead of
    # implementing a second evidence parser.
    candidate_stubs = [
        {"candidate_id": item[0]["candidate_id"], "_evidence_event_ids_omitted": True, "evidence_event_ids": []}
        for item in prepared
    ]
    bindings = validate_bindings(binding_rows, source_units, candidate_stubs)
    claimed_ids = {
        claim["unit_id"]
        for claims in bindings.values()
        for claim in claims
        if isinstance(claim, Mapping) and isinstance(claim.get("unit_id"), str)
    }

    no_memory_ids: set[str] = set()
    normalized_no_memory: list[dict[str, str]] = []
    for row in no_memory:
        if not isinstance(row, Mapping) or set(row) != {"unit_id", "reason"}:
            raise ModelOutputError("B3 no_memory row is invalid", validation_detail="invalid_evidence")
        unit_id = row.get("unit_id")
        reason = row.get("reason")
        if not isinstance(unit_id, str) or unit_id not in unit_ids or unit_id in no_memory_ids:
            raise ModelOutputError("B3 no_memory unit is invalid", validation_detail="invalid_evidence")
        if reason not in _NO_MEMORY_REASONS:
            raise ModelOutputError("B3 no_memory reason is invalid", validation_detail="invalid_evidence")
        no_memory_ids.add(unit_id)
        normalized_no_memory.append({"unit_id": unit_id, "reason": reason})
    if claimed_ids & no_memory_ids:
        raise ModelOutputError("B3 evidence cannot be both claimed and no_memory", validation_detail="invalid_evidence")
    if claimed_ids | no_memory_ids != unit_ids:
        raise ModelOutputError("B3 evidence coverage is incomplete", validation_detail="invalid_evidence")

    normalized_items: list[dict[str, Any]] = []
    for item, target_record in prepared:
        candidate_id = item["candidate_id"]
        decision = item["decision"]
        normalized: dict[str, Any] = {
            "candidate_id": candidate_id,
            "decision": decision,
            "evidence": [dict(claim) for claim in bindings[candidate_id]],
        }
        if decision == "CREATE":
            validated = validate_memory(candidate_id, decision, None, None, item["memory"], normalized["evidence"])
            normalized.update({
                "type": item["type"],
                "scopes": list(item["scopes"]),
                "scope_source": item["scope_source"],
                "memory": dict(validated),
            })
        elif decision == "UPDATE":
            target_id = item["target_memory_id"]
            validated = validate_memory(candidate_id, decision, target_id, target_record, item["memory"], normalized["evidence"])
            normalized["target_memory_id"] = target_id
            normalized["memory"] = dict(validated)
        elif decision == "NO_CHANGE":
            normalized["target_memory_id"] = item["target_memory_id"]
        else:
            normalized["reason"] = item["reason"]
        normalized_items.append(normalized)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "items": normalized_items,
        "no_memory": normalized_no_memory,
    }


def run_single_pass_stage(
    model_executor: Any,
    backend: Any,
    *,
    evidence_units: Iterable[Any],
    related_memories: Iterable[Mapping[str, Any]] = (),
    native_memories: Iterable[Mapping[str, Any]] = (),
    scope_background: Any = None,
    scope_registry: Any = None,
    create_allowed: bool = True,
    validate_memory: MemoryValidator,
    diagnostic_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    prompt, source_units, local_by_key = build_single_pass_prompt(
        evidence_units=evidence_units,
        related_memories=related_memories,
        native_memories=native_memories,
        scope_background=scope_background,
        scope_registry=scope_registry,
        create_allowed=create_allowed,
    )
    complete = getattr(model_executor, "_complete_json_stage", None)
    if not callable(complete):
        raise TypeError("model executor does not support JSON stages")
    local_rows = list(local_by_key.values())
    return complete(
        backend,
        prompt,
        system=SINGLE_PASS_SYSTEM,
        purpose="gate",
        parser=lambda raw: parse_single_pass_output(
            raw,
            evidence_units=source_units,
            local_memories=local_rows,
            create_allowed=create_allowed,
            validate_memory=validate_memory,
        ),
        diagnostic_context=diagnostic_context,
    )


__all__ = [
    "MAX_PROMPT_BYTES",
    "PROTOCOL_VERSION",
    "SINGLE_PASS_SYSTEM",
    "build_single_pass_prompt",
    "parse_single_pass_output",
    "run_single_pass_stage",
]
