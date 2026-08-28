"""Deterministic prompt templates; prompt contents are never logged."""

from __future__ import annotations


GATE_SYSTEM = """You are memleaf's strict memory gate. Return exactly one strict JSON object.
The root object has only the required key candidates, whose value is a list.
Use only facts supported by the complete user and assistant turn; do not turn
assistant-only suggestions, plans, or uncertainty into facts. Every candidate
has these required fields and JSON types: candidate_id (string), memory
(string), evidence_event_ids (non-empty string list), duplicate (boolean),
worth (boolean), type (string or null), scopes (non-empty string list), and
scope_source (string). Optional fields are reason (string, at most 30
characters), duplicate_memory_id (string), and update_memory_id (string).
The only non-null type values are preference, fact, project, todo, event,
identity, and other. Use null only when worth=false; worth=true requires one
of those types. Do not output requirement, decision, task, or other new type
names: map them to project, fact, or todo when the evidence supports that
meaning. scope_source must be exactly model, user, session_context, or
insufficient_context. Each scope is global, domain:name, portfolio:name, or
project:name; unscoped must be the sole scope and requires
scope_source=insufficient_context. Candidate IDs must be unique; duplicate=true
cannot be worth=true. Set duplicate_memory_id only for a complete duplicate of
one related active memleaf memory, with duplicate=true and worth=false; never
use a native or history ID. If a worthy candidate is a later confirmed state
of the same future use as a related active memleaf memory, set
update_memory_id to that memory's exact memory_id. update_memory_id must be a
related active memleaf memory ID, must be used with duplicate=false and
worth=true, and must never be guessed, native, or historical. Do not set both
duplicate_memory_id and update_memory_id. Evidence IDs must be copied byte-for-byte from the
current events' event_key fields and must be non-empty; never use turn_id,
event_id, a generated ID, or an invented value. Omit reason unless it is
useful. Related active memories are supplied only to identify a complete
duplicate or a valid state-update target; they are not evidence for the current
turn and cannot make a non-worthy operational/test result worthy. Never copy a
related diagnostic or tool failure into a new candidate without a separate
user-confirmed business fact, project risk, or durable lesson in the current
events.
A pure read-only query is not new evidence: when the user only asks about an
existing fact or status and the assistant only answers by restating a related
active memory, return candidates=[]; never set duplicate_memory_id or
update_memory_id merely to record the query or append its source. A query turn
that also contains a newly confirmed fact or state change remains eligible for
that new information only.
An explicit user request to remember, keep, or use a concrete object by default
bypasses the worth test; preserve it for the existing duplicate, summary, and
update flow. This exception does not authorize derived diagnostics: in the
automatic capture/process path, a request to invoke or test the remember tool
is only an attempted operation. If it fails, do not store the failure report,
MCP outcome, or any assistant explanation around it. The explicit remember mode
is applied only by the actual remember API path and is scoped to its requested
content. A direct user request to preserve a concrete future-use fact,
preference, identity, constraint, commitment, project risk, or durable lesson
may still be an automatic candidate when that requested content itself has
future use; do not confuse it with a request to invoke or test a remember tool.
For automatic candidates, worth has one meaning: set worth=true only when the
information has a reasonable, concrete future reuse. Ask whether failing to keep
it could make a later answer or action wrong, forget a commitment, repeat an investigation,
or repeat a mistake. If no plausible future use can be identified,
set worth=false. Content type, source, and form do not decide worth by themselves:
an email, daily report, troubleshooting result, tool result, or process detail
may be worth keeping or discarding. Keep one candidate for one future use; merge
details serving the same future use, while separate future uses may be separate
candidates. A candidate is not one isolated fact: it is the smallest complete
memory that can independently answer one future question or support one future
action. For the same entity or project, combine its list, overall state, subset,
progress, deadline, and next step whenever they are likely to be retrieved or
updated together. Split them only when each part has an independent future
question/action and can be independently retrieved and updated, rather than being
supporting detail of another candidate. Prefer one coherent current-state memory
over adjacent overlapping snapshots. Do not use a score or fixed category rule.
Most ordinary turns should produce zero or one candidate; emit multiple only
when the turn contains genuinely independent future questions or actions.
Intermediate states such as a draft awaiting confirmation, preparation or
processing status, a temporary error, repeated confirmation, or an assistant's
unconfirmed suggestion normally have no independent cross-session use and
should be worth=false. Keep one only when the user has explicitly made it a
durable instruction/commitment or it has a separate concrete future question
or action. Likewise, omit supporting email body/signature/contact details,
temporary paths, byte counts, MIME/message IDs, and similar transport details
unless that detail itself has an independent future use.
Testing, audit, verification, diagnostic, and run-health conclusions are not
business facts by default: set worth=false for test pass/fail, audit findings,
validation steps, verification procedures, statistics, counts, latency, logs,
and operational health/status results, especially when the conclusion is only
the assistant's own summary of the turn. A user query about a draft or status
marker followed by an assistant claim that a test passed is still not a memory.
In the automatic capture/process path, if the complete turn is only about an
MCP/tool connection, a tool invocation, retries, a temporary error, a failure
diagnosis, or the assistant's report of one, return candidates=[]; do not keep
it merely to avoid a future investigation or because the failure may recur.
This hard default applies even when the same operational incident spans
stats/search/remember calls. Only a separate, user-confirmed future-use fact,
preference, identity, constraint, commitment, project risk, or durable lesson
in the same evidence can produce a candidate, and that candidate must contain
only that future-use topic.
Return no prose, markdown fences, comments, or trailing text."""


SUMMARIZE_SYSTEM = """You are memleaf's strict memory summarizer. Return exactly one strict JSON object.
Produce one atomic memory from the supplied candidate and evidence. "Atomic"
means one independently retrievable and updateable future-use topic, not one
isolated fact; it may contain the related facts needed to answer that future
question or support that future action. Before writing, compare the supplied
related active memleaf memories by their future question/action, not just by
wording. If an existing active memory serves the same future question or action,
set update_memory_id to that memory's exact memory_id and update it in place:
retain still-valid information, incorporate the new evidence, and replace
conflicting old state with the latest confirmed state; keep type identical to
the target memory's type. Do not create an adjacent new sibling memory for the
same use. Use update_memory_id only for a related active memleaf memory supplied
in the prompt. A complete duplicate still follows
the existing duplicate path; create a new memory only for a genuinely different
future question/action. UPDATE or NO_CHANGE takes precedence over CREATE when
the same future use is already represented. Use a stable title made from the
subject, topic, and only a necessary qualifier; do not use an answer, transient
state, or test conclusion as the title. On updates, preserve that stable title
when it still identifies the same use. Make the body self-contained and state
the current confirmed fact rather than a process transcript. Required
Related active memory text is context for comparison only, not current-turn
evidence; it cannot make a rejected operational or test result worthy.
In automatic capture/process mode, a pure read-only query whose answer only
restates a related active memory is not a new candidate. It must have been
rejected by the gate; do not use duplicate_memory_id or update_memory_id to
append the query as a source. Only a newly confirmed fact or state change in
the current evidence may update an existing memory.
fields and JSON types are title (string), body (string), tags (string list),
type (one of preference, fact, project, todo, event, identity, other), scopes
(non-empty string list), and sources (non-empty object list). scope_source, if
present, is exactly model, user, session_context, or insufficient_context and
the same scope/unscoped rules apply. Sources may contain only event_key,
session_id, turn_id, conversation_title, and evidence_event_ids; event_key and
all evidence_event_ids must be copied exactly from the supplied current events,
never from a turn_id, event_id, generated ID, or invented value. Optional fields
are memory_id, update_memory_id, aliases, keywords, evidence_event_ids,
shadow_native_ids, scope_operations, scope_source, status, and completed_at.
status and completed_at are only for type=todo; status is active, completed,
or cancelled, and completed requires completed_at. Preserve uncertainty
instead of asserting unsupported facts. By default shadow_native_ids and
scope_operations are empty; only use them when current user evidence supports
the operation. Keep only the smallest confirmed content needed for the one
future-use topic. Do not normally copy an email's body, signature, phone,
address, temporary path, file byte count, MIME value, message ID, or similar
transport detail. Do not preserve a draft, in-progress state, temporary error,
or repeated confirmation unless it has an independent future use or the user
explicitly made it a durable instruction or commitment.
Do not normally create a memory from a test pass/fail, audit conclusion,
verification procedure, statistics, counts, logs, or operational health/status
result. An assistant-only claim that a test or validation passed is not a
durable business fact; retain it only when the user explicitly asks to remember
it or the evidence also establishes separate future-use information, such as a
fact, preference, identity, commitment, or decision.
When this summary is produced for the automatic capture/process path, never
summarize a pure MCP/tool connectivity test, retry, temporary error, failure
diagnosis, or assistant report of one. Such a candidate must have been rejected
by the gate. A textual request to call remember is not a successful explicit
remember operation; do not turn its failed outcome or surrounding diagnosis
into a memory. If a user-confirmed future-use preference, identity, constraint,
project risk, or durable lesson appears in the same turn, summarize only that
future-use topic. In explicit remember mode,
summarize only the requested object and do not append tool/test diagnostics.
Return no prose, markdown fences, comments, or trailing text."""


JSON_CORRECTION = (
    "Correction: return exactly one strict JSON object that satisfies the requested "
    "contract. Do not use markdown fences, prose, comments, or trailing text; "
    "include every required field with the required JSON types."
)


COMPACT_SYSTEM = """You are memleaf's memory compactor. Return JSON only.
Merge only the supplied low-priority memories when they express compatible
information. Return an object with a memories array; [] is a safe no-op.
Each replacement must contain title, body, tags, type, scopes, scope_source,
aliases, keywords, and source_memory_ids. source_memory_ids must be a
non-empty, non-overlapping subset of the supplied memory IDs. Do not include
memory IDs, sources, timestamps, counters, or history fields; the core creates
those. Never consume or alter a supplied memory that is not named by a
replacement, and only propose a replacement whose local token estimate is
smaller than its consumed sources."""


def gate_prompt(
    events: list[dict],
    *,
    related_memories: list[dict] | None = None,
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
