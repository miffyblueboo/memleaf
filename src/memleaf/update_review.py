"""Independent semantic review of automatic memory summaries.

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

import inspect
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
You are memleaf's independent semantic verifier for one automatic UPDATE.
Return exactly one strict JSON object:
{"decision":"ACCEPT"}
{"decision":"NO_CHANGE"}
{"decision":"REVISE","summary":{...complete normal summary...}}
{"decision":"DEFERRED","reason":"target_preservation_uncertain|conflicting_changes|semantic_review_failed"}

The inputs are separate: active_target is the current memory state; admitted_source is the only authority for NEW changes; proposed_summary is untrusted model output. Do not perform candidate discovery, target selection, Scope selection, or duplicate search. This review is for one candidate topic.

Semantic completeness is as important as non-invention. Verify four things:
1. Grounding: every NEW fact, state, relationship, owner, obligation, date, completion claim, and important number/code meaning in proposed_summary is supported by admitted_source. Historical context, raw tool results, attachments, and other non-conversation payloads are not new evidence. A visible assistant report may support what it actually states; questions, suggestions, plans, hypotheticals, generic acknowledgements, pure restatements, and unconfirmed claims do not independently establish a new transition.
2. Completeness: retain the meaning-defining subject/entity, object/deliverable, concrete action/state, necessary business/workstream/background context, polarity, uncertainty/conditions, attribution, and meaningful number/code roles. Scope metadata alone does not substitute for a named subject. Concrete requirements must not be generalized into a vague umbrella.
3. Target preservation: every still-valid independent target fact, obligation, deadline, and state remains represented unless admitted_source actually supersedes, cancels, completes, or removes it. Omission from new source alone is not a change.
4. Candidate boundary: the result stays within this one candidate. A negative or completed clause for one sibling does not suppress or alter another. If source distinguishes owning project/entity from implementation context, preserve that distinction. A project Scope is itself a claimed project affiliation; a mere implementation location is insufficient. If the fixed Scope conflicts with admitted ownership, use DEFERRED rather than changing Scope.

ACCEPT when all four checks pass. NO_CHANGE when admitted_source establishes no semantic change to active_target. REVISE when one complete correct summary can be formed from admitted_source plus still-valid active_target information. A REVISE summary must preserve proposed_summary's exact update_memory_id, type, scopes, scope_source, tags, sources, and schema; for todos preserve status/deadline unless admitted_source authorizes a change. DEFERRED when preservation, supersession, attribution, numeric meaning, or contradiction cannot be resolved without guessing. Never add an owner, deadline, status, field role, or completion meaning unsupported by admitted_source.

Return JSON only. No prose or reasoning."""


CREATE_SEMANTIC_REVIEW_SYSTEM = """\
You are memleaf's independent semantic verifier for one automatic CREATE.
Return exactly one strict JSON object:
{"decision":"ACCEPT"}
{"decision":"NO_CHANGE"}
{"decision":"REVISE","summary":{...complete normal summary...}}
{"decision":"DEFERRED","reason":"target_preservation_uncertain|conflicting_changes|semantic_review_failed"}

The inputs are separate: admitted_source is the only authority for the new memory; proposed_summary is untrusted model output. Do not perform candidate discovery, target selection, Scope selection, or duplicate search. This review is for one candidate topic.

Semantic completeness is as important as non-invention. Verify:
1. Every assertion and relationship in proposed_summary is supported by admitted_source. Raw tool results, attachments, historical conversation, existing memories, and other non-conversation payloads are not new evidence. A visible assistant report may support what it actually states; questions, suggestions, plans, hypotheticals, generic acknowledgements, pure restatements, and unconfirmed claims do not independently establish a new fact.
2. No meaning-defining admitted information was omitted or generalized away: preserve subject/entity, object/deliverable, concrete action/state, necessary business/workstream/background context, polarity, uncertainty/conditions, attribution, and important numbers/codes with their stated meaning. Scope metadata alone does not substitute for a named subject.
3. The proposal remains this one admitted candidate. A negative or completed clause for one sibling does not suppress or alter another.
4. Do not infer activity, completion, ownership, obligation, date, status, or a number/code role from mere proximity or naming. If source distinguishes owning project/entity from implementation context, preserve that distinction. A project Scope is itself a claimed project affiliation; a mere implementation location is insufficient. If the fixed Scope conflicts with admitted ownership, use DEFERRED rather than changing Scope. Never add an owner, deadline, status, field role, or completion meaning unsupported by admitted_source.

ACCEPT when all checks pass. REVISE when one complete source-supported revision can correct omission, generalization, or unsupported expansion while preserving proposed_summary's fixed type, scopes, scope_source, tags, sources, and existing schema. For todos preserve grounded status/deadline fields; for non-todos omit todo-only fields. A revised CREATE must not carry memory_id or update_memory_id. NO_CHANGE when no supported future-use fact, preference, constraint, action, or state remains. DEFERRED when a complete source-supported revision would require guessing or the source conflicts.

Return JSON only. No prose or reasoning."""


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
    """Copy only admitted visible conversation source with provenance fields.

    ``summary_evidence`` is expected to provide this bounded shape already, but
    the review boundary is enforced again here. In particular, a stale caller
    or a legacy request must not reintroduce a tool result, attachment, or other
    external payload into the semantic reviewer simply by placing it in the
    iterable.
    """

    result: list[dict[str, Any]] = []
    for item in source:
        if not isinstance(item, Mapping):
            raise ModelOutputError(
                "update semantic review source must contain objects",
                validation_detail="other_schema_violation",
            )
        role = item.get("role")
        origin = item.get("evidence_origin")
        expected_origin = {
            "user": "user_assertion",
            "assistant": "assistant_report",
        }.get(role)
        if expected_origin is None or origin != expected_origin:
            continue
        projected = {
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
        }
        source_context = item.get("source_context")
        if isinstance(source_context, str):
            projected["source_context"] = source_context
        result.append(projected)
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
    prompt = "UPDATE_SEMANTIC_REVIEW\n" + encoded + ("\n\nThe three top-level values are separate inputs. " "Return only the strict review object.")
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise ModelOutputError(
            "update semantic review input exceeds prompt budget",
            validation_detail="other_schema_violation",
        )
    return prompt


def build_create_review_prompt(
    admitted_source: Iterable[Mapping[str, Any]],
    proposed_summary: Mapping[str, Any],
) -> str:
    """Build a bounded CREATE review prompt with no active-target channel."""

    if not isinstance(proposed_summary, Mapping):
        raise ModelOutputError(
            "create semantic review summary must be an object",
            validation_detail="root_shape",
        )
    payload = {
        "admitted_source": _source_projection(admitted_source),
        "proposed_summary": _json_safe(dict(proposed_summary)),
    }
    encoded = json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    prompt = "CREATE_SEMANTIC_REVIEW\n" + encoded + ("\n\nThe two top-level values are separate inputs. " "Return only the strict review object.")
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise ModelOutputError(
            "create semantic review input exceeds prompt budget",
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


def _complete_json_stage_compat(
    model_executor: Any,
    backend: Any,
    prompt: str,
    *,
    system: str,
    purpose: str,
    parser: Callable[[str], Any],
    diagnostic_context: Mapping[str, Any] | None,
    metric_stage: str,
) -> Any:
    """Use new stage telemetry when supported without breaking legacy executors."""

    complete = model_executor._complete_json_stage
    kwargs: dict[str, Any] = {
        "system": system,
        "purpose": purpose,
        "parser": parser,
        "diagnostic_context": diagnostic_context,
    }
    try:
        signature = inspect.signature(complete)
    except (TypeError, ValueError):
        # A legacy or opaque executor remains valid. Do not execute once and
        # retry after a TypeError because that could duplicate a model call.
        pass
    else:
        parameters = signature.parameters
        if "metric_stage" in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            kwargs["metric_stage"] = metric_stage
    return complete(backend, prompt, **kwargs)


def _run_review(
    model_executor: Any,
    backend: Any,
    *,
    prompt: str,
    system: str,
    parse_summary: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    diagnostic_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the independent review; model/parser failures fail closed."""

    try:
        def parse(raw: Any) -> dict[str, Any]:
            return parse_update_review_output(raw, parse_summary=parse_summary)

        result = _complete_json_stage_compat(
            model_executor,
            backend,
            prompt,
            system=system,
            purpose="summarize",
            parser=parse,
            diagnostic_context=diagnostic_context,
            metric_stage="semantic_review",
        )
    except (ModelError, ModelOutputError, TypeError, ValueError):
        return {"decision": "DEFERRED", "reason": "semantic_review_failed"}
    if not isinstance(result, Mapping) or result.get("decision") not in UPDATE_REVIEW_DECISIONS:
        return {"decision": "DEFERRED", "reason": "semantic_review_failed"}
    return dict(result)


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
    """Run the independent automatic UPDATE review."""

    try:
        prompt = build_update_review_prompt(target, admitted_source, proposed_summary)
    except (ModelError, ModelOutputError, TypeError, ValueError):
        return {"decision": "DEFERRED", "reason": "semantic_review_failed"}
    return _run_review(
        model_executor,
        backend,
        prompt=prompt,
        system=UPDATE_SEMANTIC_REVIEW_SYSTEM,
        parse_summary=parse_summary,
        diagnostic_context=diagnostic_context,
    )


def review_create(
    model_executor: Any,
    backend: Any,
    *,
    admitted_source: Iterable[Mapping[str, Any]],
    proposed_summary: Mapping[str, Any],
    parse_summary: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    diagnostic_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the independent automatic CREATE evidence review."""

    try:
        prompt = build_create_review_prompt(admitted_source, proposed_summary)
    except (ModelError, ModelOutputError, TypeError, ValueError):
        return {"decision": "DEFERRED", "reason": "semantic_review_failed"}
    return _run_review(
        model_executor,
        backend,
        prompt=prompt,
        system=CREATE_SEMANTIC_REVIEW_SYSTEM,
        parse_summary=parse_summary,
        diagnostic_context=diagnostic_context,
    )


__all__ = [
    "UPDATE_REVIEW_DECISIONS",
    "CREATE_SEMANTIC_REVIEW_SYSTEM",
    "UPDATE_SEMANTIC_REVIEW_SYSTEM",
    "build_create_review_prompt",
    "build_update_review_prompt",
    "parse_update_review_output",
    "review_create",
    "review_update",
]
