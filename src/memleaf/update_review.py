"""Independent semantic review of automatic UPDATE summaries.

The normal summarize call is allowed to propose a complete replacement value
for an active memory.  This module adds a second, deliberately narrow model
contract immediately before that value is frozen.  The reviewer receives the
current target, the admitted source projection, and the untrusted proposed
summary as separate inputs.  It may accept the proposal, return a semantic
no-op, rewrite the complete summary, or defer when preservation cannot be
established.

This module has no Vault or network dependency.  Callers provide the existing
``ModelExecutor`` and backend, and supply the already configured summary
parser for a possible revised summary.
"""

from __future__ import annotations

import json
import math
from typing import Any, Callable, Iterable, Mapping

from .llm import ModelError
from .validation import ModelOutputError, parse_strict_json


UPDATE_REVIEW_DECISIONS = frozenset({"ACCEPT", "NO_CHANGE", "REVISE", "DEFERRED"})
_REVIEW_FIELDS = frozenset({"decision", "summary", "reason"})
_DEFER_REASONS = frozenset({
    "target_preservation_uncertain",
    "conflicting_changes",
    "semantic_review_failed",
})
_MAX_PROMPT_BYTES = 128 * 1024
_MAX_JSON_DEPTH = 32


UPDATE_SEMANTIC_REVIEW_SYSTEM = """\
You are memleaf's independent semantic reviewer for one automatic UPDATE.
Return exactly one strict JSON object and no prose:
{"decision":"ACCEPT"}
{"decision":"NO_CHANGE"}
{"decision":"REVISE","summary":{...complete normal summary...}}
{"decision":"DEFERRED","reason":"target_preservation_uncertain|conflicting_changes|semantic_review_failed"}

For REVISE, start from proposed_summary's validated schema, not active_target's
storage projection. The complete inner summary requires title, body, tags,
type, scopes, sources and update_memory_id. Preserve its exact update target,
type, scopes and admitted source references. Do not omit tags or sources, or
replace update_memory_id with memory_id. For non-todo memories omit todo-only
status/completed_at/due_date fields. For todos keep the proposed status and
deadline unless the admitted source explicitly authorizes a change.

The active target is the current memory being updated. The admitted source is
the only authority for NEW facts, states, owners, dates, obligations and
supersessions. The proposed summary is untrusted model output, not source
evidence. Compare the target and proposal semantically: ACCEPT only when every
still-valid independent target fact, obligation, deadline and state remains
represented and the proposal adds only admitted changes. Wording changes do
not require a rewrite. Use REVISE when the proposal dropped or misstated a
still-valid target item; return one complete summary that preserves semantic
content without copying the whole old text or concatenating bodies. A REVISE
summary must keep the proposal's target ID, type, scopes, scope source and
admitted source references; it may not switch targets or broaden
authorization. Use
NO_CHANGE when the admitted source makes no confirmed change to this target.
Use DEFERRED when preservation, supersession, or contradiction cannot be
resolved. Never use business keywords, string matching, or unrelated context
to make the decision. Do not add fields to the response contract.
"""


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Project validated prompt data without silently dropping content."""

    if depth > _MAX_JSON_DEPTH:
        raise ModelOutputError(
            "update semantic review input is too deeply nested",
            validation_detail="other_schema_violation",
        )
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ModelOutputError(
                "update semantic review input contains a non-finite number",
                validation_detail="other_schema_violation",
            )
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, (str, int)) for key in value):
            raise ModelOutputError(
                "update semantic review input contains an invalid key",
                validation_detail="other_schema_violation",
            )
        return {
            str(key): _json_safe(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in value]
    raise ModelOutputError(
        "update semantic review input contains an unsupported value",
        validation_detail="other_schema_violation",
    )


_TARGET_FIELDS = (
    "memory_id",
    "title",
    "body",
    "type",
    "scopes",
    "status",
    "completed_at",
    "due_date",
)


def _target_projection(target: Any) -> dict[str, Any]:
    """Keep only the safe current-target fields needed for semantic review."""

    if isinstance(target, Mapping):
        return {
            field: target[field]
            for field in _TARGET_FIELDS
            if field in target
        }
    result: dict[str, Any] = {}
    for field in _TARGET_FIELDS:
        if hasattr(target, field):
            result[field] = getattr(target, field)
    return result


def _source_projection(source: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Copy the admitted source projection while keeping its provenance fields."""

    result: list[dict[str, Any]] = []
    for item in source:
        if not isinstance(item, Mapping):
            raise ModelOutputError(
                "update semantic review source must contain objects",
                validation_detail="other_schema_violation",
            )
        # ``summary_evidence`` already returns this bounded shape.  Copy it
        # explicitly so arbitrary request metadata cannot become source.
        result.append({
            key: item[key]
            for key in (
                "event_key",
                "timestamp",
                "role",
                "content",
                "evidence_origin",
                "unit_id",
                "section_path",
            )
            if key in item
        })
    return result


def build_update_review_prompt(
    target: Any,
    admitted_source: Iterable[Mapping[str, Any]],
    proposed_summary: Mapping[str, Any],
) -> str:
    """Build the bounded, source-separated semantic-review prompt."""

    if not isinstance(proposed_summary, Mapping):
        raise ModelOutputError(
            "update semantic review summary must be an object",
            validation_detail="root_shape",
        )
    payload = {
        "active_target": _target_projection(target),
        "admitted_source": _source_projection(admitted_source),
        "proposed_summary": _json_safe(dict(proposed_summary)),
    }
    encoded = json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    prompt = "UPDATE_SEMANTIC_REVIEW\n" + encoded + (
        "\n\nThe three top-level values are separate inputs. Return only the strict "
        "review object described by the system contract."
    )
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise ModelOutputError(
            "update semantic review input exceeds prompt budget",
            validation_detail="other_schema_violation",
        )
    return prompt


def parse_update_review_output(
    raw: Any,
    *,
    parse_summary: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Parse the strict review envelope and validate a possible revision."""

    value = parse_strict_json(raw) if isinstance(raw, str) else raw
    if not isinstance(value, Mapping):
        raise ModelOutputError(
            "update semantic review must be an object",
            validation_detail="root_shape",
        )
    unknown = set(value) - _REVIEW_FIELDS
    if unknown:
        raise ModelOutputError(
            "update semantic review contains unknown fields",
            validation_detail="unknown_fields",
        )
    decision = value.get("decision")
    if not isinstance(decision, str) or decision not in UPDATE_REVIEW_DECISIONS:
        raise ModelOutputError(
            "invalid update semantic review decision",
            validation_detail="other_schema_violation",
        )
    if decision in {"ACCEPT", "NO_CHANGE"}:
        if set(value) != {"decision"}:
            raise ModelOutputError(
                f"{decision} review cannot carry extra fields",
                validation_detail="unknown_fields",
            )
        return {"decision": decision}
    if decision == "DEFERRED":
        if set(value) != {"decision", "reason"}:
            raise ModelOutputError(
                "DEFERRED review requires only a reason",
                validation_detail="unknown_fields",
            )
        reason = value.get("reason")
        if not isinstance(reason, str) or reason not in _DEFER_REASONS:
            raise ModelOutputError(
                "invalid update semantic review defer reason",
                validation_detail="other_schema_violation",
            )
        return {"decision": decision, "reason": reason}
    if set(value) != {"decision", "summary"}:
        raise ModelOutputError(
            "REVISE review requires only a summary",
            validation_detail="unknown_fields",
        )
    proposed = value.get("summary")
    if not isinstance(proposed, Mapping):
        raise ModelOutputError(
            "REVISE summary must be an object",
            validation_detail="root_shape",
        )
    revised = parse_summary(proposed)
    if not isinstance(revised, Mapping):
        raise ModelOutputError(
            "REVISE summary parser returned a non-object",
            validation_detail="root_shape",
        )
    return {"decision": decision, "summary": dict(revised)}


def review_update(
    model_executor: Any,
    backend: Any,
    *,
    target: Any,
    admitted_source: Iterable[Mapping[str, Any]],
    proposed_summary: Mapping[str, Any],
    parse_summary: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    diagnostic_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the independent review; model/parser failures fail closed."""

    try:
        prompt = build_update_review_prompt(target, admitted_source, proposed_summary)

        def parse(raw: Any) -> dict[str, Any]:
            return parse_update_review_output(raw, parse_summary=parse_summary)

        result = model_executor._complete_json_stage(
            backend,
            prompt,
            system=UPDATE_SEMANTIC_REVIEW_SYSTEM,
            purpose="summarize",
            parser=parse,
            diagnostic_context=diagnostic_context,
        )
    except (ModelError, ModelOutputError, TypeError, ValueError):
        return {"decision": "DEFERRED", "reason": "semantic_review_failed"}
    if not isinstance(result, Mapping) or result.get("decision") not in UPDATE_REVIEW_DECISIONS:
        return {"decision": "DEFERRED", "reason": "semantic_review_failed"}
    return dict(result)


__all__ = [
    "UPDATE_REVIEW_DECISIONS",
    "UPDATE_SEMANTIC_REVIEW_SYSTEM",
    "build_update_review_prompt",
    "parse_update_review_output",
    "review_update",
]
