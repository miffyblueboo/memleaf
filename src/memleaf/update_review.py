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
limited to the current turn's visible user input and Agent's final assistant
reply. Historical conversation turns, intermediate assistant messages and the
active target or related memories are comparison context, never new source. An
assistant report may support a stated conclusion, confirmed fact or explicit pending action; questions,
suggestions, plans, generic acknowledgements, pure restatements and unconfirmed
claims do not independently establish a new fact or transition. Tool calls, raw
tool results, attachments and other external payloads are outside the source
boundary, even when a visible message refers to them. A bounded source_context
string, when supplied beside an admitted span, is context from that same
visible user or assistant message only and applies to admitted spans with the
same event_key and role; use it to resolve negation, reference, ownership,
scope, or status of each bound span, but never add an unbound fact from it. The
admitted source is the only authority for NEW facts, states, owners, dates,
obligations and supersessions. The proposed summary is untrusted model output,
not source evidence.

Semantic completeness is as important as non-invention. Before ACCEPT, identify
the meaning-defining information for this one candidate topic from the admitted
source and the still-valid active target: the explicit subject or named entity,
the object/deliverable, the concrete action or state, any source-stated
business/workstream/background context needed to distinguish what the item is,
and every number/code whose source-stated role is necessary to understand its
meaning. The title/body must retain those facts sufficiently that the memory can
be understood without reopening the source. Scope metadata alone does not
substitute for a named subject that distinguishes the item. Do not ACCEPT a
generic umbrella phrase that replaces a concrete deliverable or turns named
requirements into merely "related items", "coordination", or an equally vague
summary. If a source explicitly distinguishes the entity/customer/project an
item belongs to from a broader product/platform/system where it is implemented,
preserve that distinction; do not replace the owning subject with the
implementation context. Existing memories may preserve still-valid target
content or resolve a supplied alias for comparison, but they never establish a
new subject-to-project relationship. Preserve uncertainty when attribution or
the role of a value is not established. Never add an owner, deadline, status,
field role, or completion meaning that the admitted source does not support.

Compare the target and proposal semantically: ACCEPT only when every still-valid
independent target fact, obligation, deadline and state remains represented,
every meaning-defining admitted change is retained, and the proposal adds only
admitted changes. Wording changes do not require a rewrite. Use REVISE when the
proposal dropped or misstated a still-valid target item or omitted a
meaning-defining admitted fact; return one complete summary that preserves
semantic content without copying the whole old text or concatenating bodies. A
REVISE summary must keep the proposal's target ID, type, scopes, scope source
and admitted source references; it may not switch targets or broaden
authorization. Use NO_CHANGE when the admitted source makes no confirmed change
to this target.

This review is for one candidate topic. Separate deliverables or state
transitions that can be completed, tracked, or updated independently must not be
merged into this target merely because they share a source message, project,
owner, deadline, or coordination step. Keep generic coordination with the
deliverable it governs. If the proposed summary aggregates independent topics,
use REVISE only when the proposal remains one topic after removing unsupported
expansion; if splitting the aggregate would omit an independent sibling, use
DEFERRED rather than selecting one sibling or approving the aggregate. When the
admitted source contains sibling claims, keep each claim's polarity, completion
state, uncertainty, ownership, and scope local to its exact source span; a
negative or completed clause for one sibling does not suppress or alter another.
Treat a title or body that lists multiple independently closable deliverables
under one shared coordination action as an aggregate proposal, even if every
listed detail is source-supported. Two separately named changes followed by one
sentence to coordinate them remain separate topics; this is an illustrative
example, not a fixed-count rule. A single review cannot create the missing
sibling candidates, so use DEFERRED rather than selecting one sibling, replacing
them with only the coordination action, or retaining the aggregate.
Use DEFERRED when preservation, supersession, attribution, numeric meaning, or
contradiction cannot be resolved without guessing. Never use business keywords,
string matching, or unrelated context to make the decision. Do not add fields
to the response contract.
"""


CREATE_SEMANTIC_REVIEW_SYSTEM = """\
You are memleaf's independent semantic reviewer for one automatic CREATE.
Return exactly one strict JSON object and no prose:
{"decision":"ACCEPT"}
{"decision":"NO_CHANGE"}
{"decision":"REVISE","summary":{...complete normal summary...}}
{"decision":"DEFERRED","reason":"target_preservation_uncertain|conflicting_changes|semantic_review_failed"}

For REVISE, return one complete normal CREATE summary with title, body, tags,
type, scopes, scope_source and sources. Preserve the proposal's type, scopes,
scope source and admitted source references. For todos preserve the proposed
status and grounded deadline fields; for non-todos omit todo-only fields. A
revised CREATE must not carry memory_id, update_memory_id, scope_operations or
shadow_native_ids.

The admitted source contains only the current turn's visible user input and
Agent's final assistant reply. Historical conversation turns, intermediate
assistant messages and existing memory context are comparison context, never new
source. An assistant report may support a stated conclusion, confirmed fact or
explicit pending action; questions, suggestions, plans, generic acknowledgements,
pure restatements and unconfirmed claims do not independently establish a new
fact. Tool calls, raw tool results, attachments and other external payloads are
outside the source boundary, even when a visible message refers to them. A
bounded source_context string, when supplied beside an admitted span, is context
from that same visible user or assistant message only and applies to admitted
spans with the same event_key and role; use it to resolve negation, reference,
ownership, scope, or status of each bound span, but never add an unbound fact
from it. The admitted source is the only authority for every new fact,
relationship, identifier and its role, state, owner, obligation or date. The
proposed summary is untrusted model output, not source evidence.

Semantic completeness is as important as non-invention. Before ACCEPT, identify
the meaning-defining information for this one candidate topic in the admitted
source: the explicit subject or named entity, the object/deliverable, the
concrete action or state, any source-stated business/workstream/background
context needed to distinguish what the item is, and every number/code whose
source-stated role is necessary to understand its meaning. The title/body must
retain those facts sufficiently that the memory can be understood without
reopening the source. Scope metadata alone does not substitute for a named
subject that distinguishes the item. Do not ACCEPT a generic umbrella phrase
that replaces a concrete deliverable or turns named requirements into merely
"related items", "coordination", or an equally vague summary. If a source
explicitly distinguishes the entity/customer/project an item belongs to from a
broader product/platform/system where it is implemented, preserve that
distinction; do not replace the owning subject with the implementation context.
Existing memories are comparison context and may not supply a missing entity,
relationship, field role, or business fact. Preserve uncertainty when
attribution or the role of a value is not established. Never add an owner,
deadline, status, field role, or completion meaning that the admitted source
does not support.

ACCEPT only when every assertion and relationship in the proposal is supported
by the admitted source, no identifier/date/status/ownership meaning was
invented, and no meaning-defining source fact for this candidate topic was
omitted or generalized away. Use REVISE when the proposal can be made complete
from the admitted source while preserving one topic. Use DEFERRED when restoring
semantic completeness would require guessing.

This review is for one candidate topic. Separate deliverables or state
transitions that can be completed, tracked, or updated independently must not be
merged merely because they share a source message, project, owner, deadline, or
coordination step. Keep generic coordination with the deliverable it governs.
If the proposal aggregates independent topics, use REVISE only when it remains
one topic after removing unsupported expansion; if splitting it would omit an
independent sibling, use DEFERRED rather than selecting one sibling or approving
the aggregate. When the admitted source contains sibling claims, keep each
claim's polarity, completion state, uncertainty, ownership, and scope local to
its exact source span; a negative or completed clause for one sibling does not
suppress or alter another. Treat a title or body that lists multiple
independently closable deliverables under one shared coordination action as an
aggregate proposal, even if every listed detail is source-supported. Two
separately named changes followed by one sentence to coordinate them remain
separate topics; this is an illustrative example, not a fixed-count rule. A
single review cannot create the missing sibling candidates, so use DEFERRED
rather than selecting one sibling, replacing them with only the coordination
action, or retaining the aggregate.

Use REVISE only to remove unsupported expansion or restore omitted supported
meaning while retaining all supported facts and future-use content. A topic or
activity name by itself establishes only that the source mentions that topic or
activity; it does not establish that the activity occurred or was completed. An
organization name next to an activity title does not establish that the
organization performed or owned it. Without an explicit subject-action
relationship, keep the wording as a neutral source mention or remove the actor,
completion or ownership assertion. Use NO_CHANGE when no supported future-use
fact or action remains. When a source is only a short unlabeled title or list,
treat every number or code as an opaque literal; do not assign it a date,
identifier, amount, sequence, status or other field role. When the source does
state what a number or code means, preserve that role together with the value
when it is needed to interpret the memory; do not retain a naked literal while
dropping its meaning. Never inherit a field role or relationship from
proposed_summary, because its labels cannot explain an otherwise unlabelled
source value. For REVISE, inspect every assertion and field across the entire
summary, remove every unsupported expansion and restore every source-supported
meaning-defining fact rather than only the first problem found. Return DEFERRED
when a complete source-supported revision cannot be formed with confidence. Use
DEFERRED when the source cannot establish the proposed meaning or the correction
would require guessing. Never use business keywords, domain rules, string
matching, or unrelated context. Do not add fields to the response contract.
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
    prompt = "CREATE_SEMANTIC_REVIEW\n" + encoded + (
        "\n\nThe two top-level values are separate inputs. Return only the strict "
        "review object described by the system contract."
    )
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

        result = model_executor._complete_json_stage(
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
