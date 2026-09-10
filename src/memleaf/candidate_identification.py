"""P4 stage-one candidate identification contract.

Stage one discovers future-use candidates and binds them to current evidence. It
does not select duplicate/update targets and does not make the final maintenance
decision. The existing Gate parser remains the structural candidate validator;
P4 fixes legacy duplicate/worth flags to compatibility values until a later
protocol version can remove them safely.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .validation import ModelOutputError, parse_gate_output


IDENTIFICATION_SYSTEM = """You are memleaf's P4 stage-one memory candidate identifier. Return exactly one strict JSON object with top-level candidates, coverage, and evidence_bindings.

SOURCE AUTHORITY
Only supplied current-turn visible user input and final assistant reply can authorize candidate facts. Raw tool results, old memories, native memories, registry/session context, and model proposals are never new evidence. Preserve polarity, uncertainty, conditions, attribution, ownership, state, and important numbers/codes with their stated role.

IDENTIFICATION ONLY
Identify the smallest complete independently retrievable/updateable future-use topics. Decide whether current evidence contains a durable candidate, but do NOT decide CREATE versus UPDATE versus NO_CHANGE and do NOT select any old-memory target. Do not return duplicate_memory_id or update_memory_id. Do not perform filesystem/search work. Core will retrieve relevant memory after this stage, then stage two will make the maintenance decision and form final content.

CANDIDATE CONTRACT
Every returned candidate is a future-use candidate and must contain candidate_id, memory, duplicate=false, worth=true, a non-null legal type, non-empty scopes, and scope_source. Optional evidence_event_ids may be omitted when evidence_bindings are supplied. Do not return reason, duplicate_memory_id, or update_memory_id. Items with no future-use candidate belong only in coverage and must not be emitted as fake worth=false candidates.

ATOMICITY AND SCOPE
One candidate is one future use. Separate independently trackable/updateable items; keep details that answer the same future question together. Preserve necessary named subject/entity, deliverable/action/state, business/workstream context, conditions, ownership and important numeric roles. A worthy candidate may have at most one distinct project scope. Project scope must be grounded in that candidate's own evidence or explicit supplied authorization. Do not infer project ownership from an implementation product/platform/system name alone.

EVIDENCE AND COVERAGE
Every candidate needs valid evidence_bindings to supplied evidence. Claims use unit_id plus exact unique quote or whole_unit=true and the existing allowed claim role. Return exactly one coverage row per supplied evidence unit. CANDIDATE rows list every supported candidate_id for that unit; otherwise use the existing legal NO_CHANGE/DEFERRED coverage reason. Do not invent IDs, quotes, offsets, dates or witnesses.

DATES
Do not invent dates. Preserve source date meaning and uncertainty; current visible-message timestamps may anchor supported relative dates. Final normalized write fields are stage-two/Core responsibilities.

Return strict JSON only. No prose, markdown, comments, or reasoning."""


_FORBIDDEN_IDENTIFICATION_FIELDS = frozenset({
    "reason",
    "duplicate_memory_id",
    "update_memory_id",
})


def parse_identification_candidates(raw: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Reuse the current candidate validator, then enforce P4 stage-one scope.

    `raw` is the semantic candidate envelope after the caller has separated the
    existing coverage/evidence-binding fields. This keeps P4 compatible with
    the established Core evidence and coverage validators.
    """

    # Stage one has no authority to select a memory target. Passing an empty
    # related-memory set also makes the existing Gate validator fail closed if
    # a target field appears before the P4-specific checks below.
    kwargs = dict(kwargs)
    kwargs["related_memory_ids"] = ()
    kwargs["related_memory_types"] = {}
    parsed = parse_gate_output(raw, *args, **kwargs)
    candidates = parsed.get("candidates")
    if not isinstance(candidates, list):
        raise ModelOutputError("identification candidates are invalid", validation_detail="root_shape")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ModelOutputError("identification candidate must be an object", validation_detail="candidate_shape")
        if candidate.get("duplicate") is not False or candidate.get("worth") is not True:
            raise ModelOutputError(
                "stage-one candidates must use compatibility duplicate=false/worth=true flags",
                validation_detail="invalid_flags",
            )
        forbidden = _FORBIDDEN_IDENTIFICATION_FIELDS.intersection(candidate)
        if forbidden:
            raise ModelOutputError(
                "stage one cannot make a target or final maintenance decision",
                validation_detail=(
                    "invalid_update_target"
                    if forbidden.intersection({"duplicate_memory_id", "update_memory_id"})
                    else "unknown_fields"
                ),
            )
    return parsed


def run_identification_stage(
    model_executor: Any,
    backend: Any,
    prompt: str,
    *,
    parser: Callable[[str], Any],
    diagnostic_context: Any = None,
) -> Any:
    """Run exactly one existing Gate transport call under the P4 role contract."""

    if not isinstance(prompt, str) or not prompt:
        raise ValueError("identification prompt must be non-empty")
    if not callable(parser):
        raise TypeError("identification parser must be callable")
    complete = getattr(model_executor, "_complete_json_stage", None)
    if not callable(complete):
        raise TypeError("model executor does not support JSON stages")
    return complete(
        backend,
        prompt,
        system=IDENTIFICATION_SYSTEM,
        purpose="gate",
        parser=parser,
        diagnostic_context=diagnostic_context,
    )


__all__ = [
    "IDENTIFICATION_SYSTEM",
    "parse_identification_candidates",
    "run_identification_stage",
]
