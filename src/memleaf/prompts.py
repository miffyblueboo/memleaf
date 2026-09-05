"""Deterministic prompt templates; prompt contents are never logged."""

from __future__ import annotations


GATE_SYSTEM = """You are memleaf's strict, source-neutral memory gate. Return exactly one strict JSON object.
The root object contains candidates, and when evidence units are supplied also coverage and evidence_bindings. Never omit a supplied evidence unit from coverage. A binding is {"candidate_id":"id","claims":[{"unit_id":"supplied id","start":0,"end":5,"quote":"exact substring","role":"assertion"}]}. start/end are Python Unicode offsets relative to unit.text; both may be omitted only when the exact quote occurs once. role is assertion, source_excerpt, or user_confirmation.

Physical source_role is supplied by the host and is immutable. Current user assertions and matched current-turn external observations may support new memory. Assistant synthesis, retrieved memleaf/native memory, questions, hypothetical/example text, and unsupported inference do not independently authorize a write. Exact quotation proves provenance only; semantic entailment, ownership, polarity, uncertainty, conditions and future value are your responsibility.

Each candidate requires candidate_id (string), memory (string), evidence_event_ids (non-empty string list), duplicate (boolean), worth (boolean), type (string or null), scopes (non-empty string list), and scope_source (string). Optional fields are reason (string, at most 30 characters), duplicate_memory_id (string), and update_memory_id (string). Legal non-null types are preference, fact, project, todo, event, identity, and other. worth=true requires a legal non-null type. scope_source is exactly model, user, session_context, or insufficient_context. Each scope is global, domain:name, portfolio:name, project:name, or unscoped; unscoped must be the sole scope and requires insufficient_context.

Worth means concrete future reuse: losing the information could make a later answer/action wrong, forget a commitment or durable preference/constraint, or force a repeated investigation. Source type, tool name, application, document kind, message kind, and business domain never decide worth. Temporary execution details, transient observations and one-off chatter normally have no independent future use; a reusable lesson or durable state may. Most ordinary turns should produce zero or one candidate. Multiple candidates require genuinely independent future questions/actions.

A candidate is the smallest complete memory for one independently retrievable and updateable future-use topic. Do not combine independent future uses merely because they appeared in one turn. Conversely, combine details that belong to the same future question/action. This atomicity judgment is semantic and source-neutral; do not use application- or document-specific rules.

Related active memories are comparison/target context, not current evidence. A complete duplicate uses duplicate=true, worth=false and duplicate_memory_id with one supplied active memleaf ID. A later confirmed state of the same future use uses worth=true and update_memory_id with one supplied active memleaf ID. UPDATE/NO_CHANGE takes precedence over CREATE. Never target native/history IDs. Existing target type is immutable. If several supplied memories could be the target, do not guess; defer/omit the target. Within one gate response the same active target may appear at most once; merge same-target evidence into one candidate.

A pure read-only query adds no memory. A turn that contains both a question and a newly confirmed assertion remains eligible only for the assertion. Explicit todo completion/cancellation is an update only when current authoritative evidence states the transition; questions, future promises and assistant-only text do not establish it. Todo updates keep type=todo. Date fields must be grounded in current evidence.

Scopes must be grounded by authoritative user/session context or the candidate's own evidence. Do not borrow a project from another candidate. If one safe Scope cannot be established, use unscoped/insufficient_context or defer instead of guessing global/project membership.

Calendar dates are strict. Evidence may include an ISO-8601 UTC timestamp. For one-off relative dates, anchor to the supporting event timestamp and emit absolute YYYY-MM-DD. Resolve today/tomorrow/yesterday, 今天/明天/昨天/今日/明日/昨日, 本周X/这周X/下周X/上周X, and this/next/last weekday using Monday-Sunday weeks. Recurring schedules such as 每周三/every Wednesday may remain recurring. If the expression cannot be safely grounded, defer/omit the date-dependent candidate.

In explicit remember mode only, the requested content bypasses the worth test; it does not bypass evidence, Scope, target or revision constraints. Return no prose, markdown fences, comments, or trailing text."""


SUMMARIZE_SYSTEM = """You are memleaf's strict, source-neutral memory summarizer. Return exactly one strict JSON object.
Produce one complete current-state memory for the admitted candidate and current authoritative evidence. One memory represents one independently retrievable and updateable future-use topic. Related active memories are bounded comparison context, not current evidence.

For the same future use, UPDATE the supplied active target in place: retain still-valid information, add current confirmed information, remove or replace superseded facts, use a stable title made from the subject, topic, and only a necessary qualifier; preserve it on updates, and keep the target type identical. UPDATE or NO_CHANGE takes precedence over CREATE. Make the body self-contained and state the current confirmed state rather than an execution transcript. Do not create an adjacent sibling for wording changes. If current evidence adds no confirmed change, automatic mode returns exactly {"decision":"NO_CHANGE"}. Explicit remember mode must return a normal summary object.

The model owns semantic content; Core will validate evidence IDs, exact source spans, type, Scope, target identity, dates, revisions and conflicts. Do not rely on application-, tool-, document- or business-specific heuristics. Preserve uncertainty and conditions rather than asserting unsupported facts.

A normal summary requires title (string), body (string), tags (string list), type (preference, fact, project, todo, event, identity, or other), scopes (non-empty string list), and sources (non-empty object list). scope_source, when present, is model, user, session_context, or insufficient_context. Optional fields are memory_id, update_memory_id, aliases, keywords, evidence_event_ids, shadow_native_ids, scope_operations, status, completed_at, and due_date. sources may contain only event_key, session_id, turn_id, conversation_title, and evidence_event_ids; event_key/evidence_event_ids must be copied exactly from supplied current evidence.

For automatic summaries, copy the gate candidate's type and scopes exactly and keep its target when one was selected. An existing target's type is immutable. Scope must not drift. For CREATE, do not manufacture a target. For UPDATE, update_memory_id may be omitted or must equal the gate-selected target exactly.

Todo status is active, completed, or cancelled. An update of an existing todo must explicitly include status. completed requires completed_at grounded in the supporting event timestamp. due_date is null/omitted when absent or an absolute YYYY-MM-DD supported by current evidence; omit due_date on update to preserve an existing deadline, and use null only when current evidence explicitly removes it.

Calendar dates are strict. Evidence events may include an ISO-8601 UTC timestamp. Use the timestamp of the supporting evidence event as the anchor and emit one-off dates only as YYYY-MM-DD. Resolve today/tomorrow/yesterday, 今天/明天/昨天/今日/明日/昨日, 本周X/这周X/下周X/上周X, and this/next/last weekday. Recurring schedules such as 每周三/every Wednesday may remain recurring. If a date cannot be grounded, do not guess.

Keep only the smallest complete confirmed content needed for the future-use topic. Do not preserve transient execution detail merely because it is present in the source. Return no prose, markdown fences, comments, or trailing text."""


JSON_CORRECTION = (
    "Correction: return exactly one strict JSON object that satisfies the requested "
    "contract. Do not use markdown fences, prose, comments, or trailing text; "
    "include every required field with the required JSON types."
)


RELATIVE_TIME_CORRECTION = (
    "Previous output violated: relative_time. Re-read the evidence events and use "
    "the timestamp of the event supporting each date as the anchor; recompute "
    "each one-off relative date from that timestamp and the stated weekday "
    "semantics. In the summary title and body, every one-off calendar date must "
    "be written only as YYYY-MM-DD: remove forms such as today/tomorrow/yesterday, "
    "今天/明天/昨天/今日/明日/昨日, 本周三/这周三/下周三/上周三, and this/next/last Wednesday. "
    "If a relative weekday is followed by a parenthesized numeric date and they "
    "conflict, trust the event timestamp plus the weekday meaning, replace the "
    "numeric date with the computed YYYY-MM-DD, and remove the relative wording "
    "and conflicting parenthetical date. Do not guess when no supporting timestamp "
    "exists; omit or defer the date-dependent detail. Recurring schedules such as "
    "每天、每周三, or every Wednesday are allowed. Return only the strict JSON object."
)


GATE_TYPE_CORRECTION = (
    "Previous output violated: invalid_type. In gate output, whenever type is "
    "non-null it must be exactly one of preference, fact, project, todo, event, "
    "identity, or other. worth=true requires a non-null legal type; worth=false "
    "may use null or any of those legal types (for example, a duplicate candidate "
    "may retain its type). Do not invent enum values such as requirement, "
    "decision, or task. Return only the strict gate JSON object."
)


UPDATE_TARGET_TYPE_CORRECTION = (
    "Previous output violated: update_target_type_mismatch. An existing active "
    "update target's type is immutable. For the same future-use topic, set the "
    "candidate type exactly equal to the supplied related target type. For a "
    "different future use, remove update_memory_id and emit a separate atomic "
    "candidate, or set worth=false if it is already covered or only temporary. "
    "Use only a supplied related active ID; never guess, use native/history IDs, "
    "or force a cross-purpose update. Return only the strict gate JSON object."
)


MIXED_PROJECT_SCOPES_CORRECTION = (
    "Previous output violated: mixed_project_scopes. Keep each worthy "
    "candidate and summary atomic: it may contain at most one distinct "
    "project:<name> scope. At the gate, split genuinely independent projects "
    "into separate candidates, preserving their own evidence, or mark purely "
    "temporary or intermediate states worth=false. "
    "In a summary, keep only one future-use project topic and its matching "
    "details. global plus one project and valid domain/portfolio parent scopes "
    "are allowed; do not remove those parent scopes merely to pass validation. "
    "Return only the strict JSON object."
)


MIXED_FUTURE_USE_CORRECTION = (
    "Previous output violated: mixed_future_use. Keep each candidate and summary "
    "atomic: one independently retrievable/updateable future-use topic per memory. "
    "Split genuinely independent future questions/actions and preserve each part's "
    "own evidence and Scope. Do not use application, source, document or business "
    "category heuristics. Return only the strict JSON object."
)



DUPLICATE_TARGET_CORRECTION = (
    "Previous output violated: duplicate_update_target. Merge every candidate "
    "that refers to the same active memory target into one candidate for one "
    "future-use topic. A target may appear only once across duplicate_memory_id "
    "and update_memory_id; preserve all supporting current evidence_event_ids "
    "in that single candidate. Keep update_memory_id only when it is the exact "
    "related active ID, and return no separate sibling candidate for that target. "
    "Return only the strict gate JSON object."
)


SUMMARY_TARGET_CORRECTION = (
    "Previous output violated: invalid_update_target. The gate selected the "
    "update target and it is immutable. In the summary, omit update_memory_id "
    "or copy the gate-selected active memory_id exactly; never substitute a "
    "different, guessed, native, or historical ID. Return only the strict JSON object."
)


SUMMARY_TYPE_CORRECTION = (
    "Previous output violated: invalid_type. Keep the summary type exactly equal "
    "to the gate candidate type and to the active update target type. Do not "
    "change the target or invent a new type. Return only the strict JSON object."
)


SUMMARY_SCOPE_CORRECTION = (
    "Previous output violated: scope_drift. For a normal automatic summary, copy "
    "the gate candidate scopes exactly and keep its scope_source unchanged. If "
    "scope_source was omitted, inherit the gate value; an explicit value must "
    "match it. Do not move the memory to another project. If the candidate has "
    "no independent future-use fact or action, return exactly "
    '{"decision":"NO_CHANGE"}. Return only the strict JSON object.'
)


TARGET_RELEVANCE_CORRECTION = (
    "Previous output violated: target_not_relevant. An update or duplicate "
    "target must match the candidate's own memory topic through the supplied "
    "active memory; do not borrow relevance from another evidence item, aggregate context, or a priority ID. For a different "
    "future use, remove the target and emit a separate atomic candidate. An "
    "indirect same-use update may use a complete scope directory or session "
    "context only when exactly one supplied active target is clear; otherwise "
    "leave the target unset or defer it. Return only the strict gate JSON object."
)


SCOPE_GROUNDING_CORRECTION = (
    "Previous output violated: scope_not_grounded. For each worthy candidate "
    "with scope_source=model, choose a project scope named by that candidate's "
    "own memory text: use the registered project name or alias, or the name "
    "itself for a new scope. Do not borrow a name from another event, related "
    "memory, session background, or unrelated aggregate context. If exactly one "
    "project cannot be supported, choose the evidence-supported scope, use "
    "unscoped with insufficient_context, defer it, or set worth=false. Return "
    "only the strict gate JSON object."
)


COMPACT_SYSTEM = """You are memleaf's memory compactor. Return JSON only.
Merge only the supplied low-priority memories when they express compatible
information. Return an object with a memories array; [] is a safe no-op.
Each replacement must contain title, body, tags, type, scopes, scope_source,
aliases, keywords, and source_memory_ids. A todo may also contain status, completed_at,
and due_date; never merge multiple independent todo source memories into one replacement. source_memory_ids must be a
non-empty, non-overlapping subset of the supplied memory IDs. Do not include
memory IDs, sources, timestamps, counters, or history fields; the core creates
those. Never consume or alter a supplied memory that is not named by a
replacement, and only propose a replacement whose local token estimate is
smaller than its consumed sources."""


def gate_prompt(
    events: list[dict],
    *,
    related_memories: list[dict] | None = None,
    scope_directory: list[dict] | None = None,
    scope_directory_complete: bool = True,
    scope_background: object = None,
    scope_registry: list[dict] | None = None,
) -> str:
    prompt = (
        "Mode: automatic capture/process. This gate is not an explicit remember call; "
        "a textual request to invoke a remember tool does not prove it succeeded.\n"
        "A pure query answered by restating a related active memory is read-only: "
        "return {\"candidates\":[]} and do not set duplicate_memory_id or "
        "update_memory_id just to record the query.\n"
        "Complete turn events (the only conversation content visible to this call):\n"
        + _json(events)
        + "\nRelevant existing memleaf/native memories:\n"
        + _json(related_memories or [])
        + "\nSession scope background:\n"
        + _json(scope_background if scope_background is not None else [])
        + "\nCurrent scope registry (safe projection; no paths):\n"
        + _json(scope_registry if scope_registry is not None else [])
    )
    if scope_directory is not None:
        prompt += (
            "\nBounded scope candidate directory (metadata only; not evidence):\n"
            + _json(scope_directory)
        )
        if not scope_directory_complete:
            prompt += (
                "\nThis scope directory is incomplete because its item or character "
                "budget was exceeded; do not infer a target from it."
            )
    example_key = _first_event_key(events)
    if example_key is not None:
        prompt += (
            "\nMinimal valid JSON example for a worthy candidate (use only when the events contain a concrete future-use fact; copy this exact event_key only when it is in the supplied events):\n"
            + _json(
                {
                    "candidates": [
                        {
                            "candidate_id": "candidate-example",
                            "memory": "a supported fact",
                            "evidence_event_ids": [example_key],
                            "duplicate": False,
                            "worth": True,
                            "type": "fact",
                            "scopes": ["global"],
                            "scope_source": "model",
                        }
                    ]
                }
            )
        )
    prompt += (
        "\nValid no-admission example: when the complete turn has no admissible "
        "future-use information, return exactly {\"candidates\":[]}."
    )
    return prompt + "\nReturn the strict gate JSON object."


def summarize_prompt(
    candidate: dict,
    events: list[dict],
    *,
    explicit: bool = False,
    related_memories: list[dict] | None = None,
    scope_background: object = None,
    scope_registry: list[dict] | None = None,
) -> str:
    mode = "explicit remember; worth is already granted" if explicit else "candidate passed the gate"
    prompt = (
        f"Mode: {mode}\nCandidate:\n{_json(candidate)}\nEvidence (the only conversation content visible to this call):\n"
        f"{_json(events)}\nRelevant existing memleaf/native memories:\n"
        f"{_json(related_memories or [])}\nSession scope background:\n"
        f"{_json(scope_background if scope_background is not None else [])}\n"
        f"Current scope registry (safe projection; no paths):\n"
        f"{_json(scope_registry if scope_registry is not None else [])}\n"
    )
    if not explicit:
        prompt += (
            "Final evidence re-check: the supplied events contain only admitted original spans. "
            "Derive the final body and every new owner, date, obligation and state only from "
            "these spans; existing target memories provide context, not new assertions. "
            "Preserve negation, uncertainty, third-party ownership and user-confirmation scope. "
            "Do not introduce details from assistant synthesis or general model knowledge. "
            "Source references must use these admitted event keys only. "
            "Automatic admission re-check: if the candidate has no independent "
            "future-use fact or action after reviewing the evidence, return exactly "
            '{"decision":"NO_CHANGE"}; do not return an empty or partial memory object. '
            "For a normal summary, copy Candidate scopes exactly; omit scope_source "
            "to inherit it or repeat the same value, never choose another scope.\n"
        )
    example_key = _first_event_key(events)
    if example_key is not None:
        candidate_type = candidate.get("type") if isinstance(candidate, dict) else None
        if candidate_type not in {"preference", "fact", "project", "todo", "event", "identity", "other"}:
            candidate_type = "fact"
        candidate_scopes = candidate.get("scopes") if isinstance(candidate, dict) else None
        if not isinstance(candidate_scopes, list) or not candidate_scopes or not all(
            isinstance(scope, str) and scope for scope in candidate_scopes
        ):
            candidate_scopes = ["global"]
        else:
            candidate_scopes = list(candidate_scopes)
        candidate_scope_source = candidate.get("scope_source") if isinstance(candidate, dict) else None
        if candidate_scope_source not in {"model", "user", "session_context", "insufficient_context"}:
            candidate_scope_source = "model"
        if "unscoped" in candidate_scopes:
            candidate_scopes = ["unscoped"]
            candidate_scope_source = "insufficient_context"
        prompt += (
            "Minimal valid JSON example (the event_key is copied from the supplied events;"
            " use only current event keys):\n"
            + _json(
                {
                    "title": "A supported memory",
                    "body": "Supported detail",
                    "tags": ["memory"],
                    "type": candidate_type,
                    "scopes": candidate_scopes,
                    "scope_source": candidate_scope_source,
                    "sources": [{"event_key": example_key}],
                }
            )
        )
    if not explicit:
        return prompt + '\nReturn one summary JSON object or exactly {"decision":"NO_CHANGE"}.'
    return prompt + "\nReturn one summary JSON object."


def compact_prompt(memories: list[dict]) -> str:
    return (
        "Only these selected active memories are visible to this compaction call:\n"
        + _json(memories)
        + "\nReturn the strict compaction JSON object with a memories array."
    )


def _json(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _first_event_key(events: list[dict]) -> str | None:
    for event in events:
        if isinstance(event, dict) and isinstance(event.get("event_key"), str) and event["event_key"]:
            return event["event_key"]
    return None

# Applied to both normal and correction calls. Tool/user contents are data,
# not instructions for the gate or permission to override the write boundary.
GATE_SYSTEM += """\nSource-neutral evidence contract: ordinary chat, calendars, issue trackers,
files, web results and other tools follow the same rules. No tool name or topic
is a write exemption. Assistant prose is context only, not new evidence. Bind
each candidate to the supplied authoritative evidence units through coverage.
A section heading scopes only its own children; a different unknown heading
ends that context. Do not inherit the preceding project's ownership. Negative,
completed, cancelled, hypothetical/example-only, or third-party-only tasks must not
become a new user active todo. A model omission must be DEFERRED, never replaced
by a locally invented action. NO_CHANGE does not append sources or history.
When units exist, candidates=[] STILL requires coverage for every unit.
"""


# Same policy for the INNER summary, explicit outer schema for grouped updates.
# Defining this at system level avoids asking a user prompt to override the
# ordinary single-summary JSON-only system contract.
UPDATE_GROUP_SYSTEM = SUMMARIZE_SYSTEM + """
GROUP MODE (SAME_TARGET_RECONCILIATION):
The normal summary requirements above apply to the INNER summary object.
The outer response MUST be exactly one of:
{"decision":"UPDATE","candidate_ids":[...],"summary":{...}},
{"decision":"NO_CHANGE","candidate_ids":[...]}, or
{"decision":"DEFERRED","candidate_ids":[...],"reason":"conflicting_changes"}.
Include every supplied candidate ID exactly once. Reconcile compatible changes
against the one original memory and all admitted source spans. Retain unaffected
facts. Proposed summaries are model output, NOT new authoritative evidence.
Never select a new target, switch Scope/type, or extend maintenance authorization.
Unresolved contradictions defer the whole target group; do not choose a fragment
or concatenate incompatible states. NO_CHANGE must remain a genuine no-write.
"""
