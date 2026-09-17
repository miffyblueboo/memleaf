# Selected-source explicit retention (G3e, opt-in)

This increment connects a real, selected retention authorization to the existing
incremental preview/compiler, configured backend, two-dispatch run ledger and
shared commit/recovery. It does not enable a second dispatcher or writer. Default
`remember()`, `process()`, Hermes hooks and MCP routing are not switched yet.

## Public contract

```python
preview = service.preview_incremental(
    source="hermes", session_id="session", turn_id="turn",
)
# Each source_refs item maps an ephemeral eN to an immutable captured event key.
# The trusted caller chooses the user-selected message(s), not every message.
source_ref = preview["source_refs"][0]["source_ref"]
result = service.remember_incremental(
    source="hermes", session_id="session", turn_id="turn",
    intent_id="stable-user-authorization-id",
    selected_source_refs=[source_ref],
    retention_request="Remember only the second point from the selected message.",
)
# Transport retry consumes the same remaining allowance; no automatic loop.
if result["retry_available"]:
    result = service.remember_incremental(
        source="hermes", session_id="session", turn_id="turn",
        intent_id="stable-user-authorization-id", selected_source_refs=[source_ref],
        retention_request="Remember only the second point from the selected message.",
        recover=True,
    )
```

`selected_source_refs` is a nonempty list of distinct, exact event-key hashes
from the selected complete captured turn. It is not a title, body hash invented
by the caller, native ID, memory ID, numeric position or model `e1` reference.
Foreign, superseded, missing and context-only events are rejected before dispatch.
Selection order is irrelevant; duplicate refs are rejected. Preview exposes only
ID/role metadata alongside the existing model request, not an extra copy of text.

`retention_request` is the user's actual selection request. The caller must have
real retention authorization; generating another string is not evidence of user
consent. This is a trusted local Python API, not a remote authentication service.
Common credential patterns are redacted using the existing redactor before the
request is stored or sent. This is best-effort redaction, not proof that arbitrary
sensitive material has been detected. Null/empty/control-invalid or overlong input
is rejected. Input limits also remain subject to the total planning budget.

The selected messages alone are `new`; unselected messages from that turn retain
their original roles and are `context`. The actual selection request is included
as `retention_request` in the same model input and bound into the snapshot. Selecting
a message containing two topics does not authorize both by itself. Core validates
the source boundary; the same planner interprets 'the second point'. It cannot
prove topic fidelity through references alone. Live semantic testing is still needed.

The existing fixed system prompt already defines `explicit_remember`. It stays
byte-for-byte unchanged. No new semantic review, topic keyword filter or model
call is introduced. Explicit retention still allows UPDATE, NO_CHANGE or DEFERRED;
NO_MEMORY cannot silently discard the selected authorized content. Empty items
remain unresolved, not successful retention. Privacy, scope, native sharing,
source validation, lifecycle checks and whole-target CAS continue to apply.

## Identity, requests and retries

Within one Vault/source/session the same `intent_id` names one immutable user
authorization. Its receipt binds turn, selected source revisions, normalized request
hash, scope and candidate settings. Changing the selection, request or scope
under that intent is a conflict, not a new budget. A genuinely new user request
uses a new intent and still compares against current local and native memories.
The run ID is authorization-stable; the existing request budget additionally binds
the selected immutable event/revision set. Unselected context is part of the frozen
snapshot, not another authorized assertion or a reason to reset the budget.

Automatic NO_MEMORY/closed budgets and completed explicit authorizations are
independent decisions. A new explicit authorization does not inherit an old
job's automatic consumption. Retries of this same explicit authorization reuse
the canonical runner/resume path and at most two reservations across process
restarts or configured/backend facades. Default legacy text `remember()` is still
staged separately: this is not yet a promise of identical identities across the
legacy text API and the new selected-source API.

Source-local time is copied from the selected evidence, never from the retention
request processing time. A request to retain old content is not proof that its old
business state has become current again. Pending requests reject changed source,
context, native notes/sharing or targets. Completed replay returns an historical
receipt, not a new assertion that today's source or target is unchanged.

The request must refer to available captured material for a new authorization.
If raw source was legitimately cleaned, Core does not reconstruct it from the
request text, a receipt hash or a model-generated summary. A retained completed
run can still be returned with its original arguments or run ID without source
or model access. Fresh evidence can be captured under a genuine new authorization;
this increment does not implement arbitrary historical-source reconstruction.

## Settlement, cleanup, privacy and recovery

Explicit success settles its own work only. It does not edit the automatic
processed_turn row, its watermarks, deferred decisions or cleanup deadlines.
Otherwise selecting one point could consume unrelated assertions in that turn.
The already existing automatic pipeline may later process the turn and compare
against the new memory normally. Partial explicit decisions likewise do not claim
permanent ownership of the whole automatic source turn.

While request/commit recovery is pending, the shared ownership and context-retention
checks protect the sources and prevent concurrent conflicting dispatch. A prepared
commit must recover before another same-turn worker proceeds. Once completed,
explicit work releases this protection; partial/failed control-state reclamation
retains the current conservative policy and remains separate lifecycle work.

The shared commit bridge freezes operation IDs and full before/after state before
writes. Crashes resume the same response and operation, never rerun a successful
NO_CHANGE or reallocate an already applied ID. Index failure remains recoverable
with no extra semantic request. Only the explicit work receipt is settled.

Common run/commit validators check the request-kind and selection binding together;
missing or contradictory control fields are not silently treated as automatic.
The persisted terminal binding contains source IDs and the redacted request hash,
not retention-request text. Complete/failed/cancelled paths strip request,
response and retention_request; recoverable partial state has the bounded G3f
exception described below. The chosen memory itself is retained as authorized.
Forget cancels existing dependent runs and their plaintext before removing targets;
replaying the old intent remains cancelled. A truly new authorized retention request
can start a new work when its selected source is still available and recording is
permitted. It does not recover unavailable or explicitly purged raw material.

## Verification and limits

Public tests cover selected-vs-context source boundaries, unselected operation
rejection, role preservation, same/new intent, automatic NO_MEMORY then explicit
retention, explicit then automatic processing, scope conflicts, source revisions,
request changes, immutable source time, native NO_CHANGE, privacy, independent
old-budget consumption, actual subprocess exit, concurrent callers, stored-response
and commit/index recovery. Tests use temporary Vaults and deterministic backends;
request counts are not provider billing measurements.

No real Flash, production Vault, native Hermes installation or Windows/macOS
acceptance is implied. G3f supplies explicit limited partial repair/replan.
Native conflict coordination, generalized scope merging, source-window
GC and default host routing remain subsequent work. Do not activate the experimental route by changing old defaults.

## G3f recovery-state clarification

New partial results may retain a bounded `partial_basis`, including the original
redacted retention request, solely for explicit recovery. Fully complete, failed
and cancelled outcomes, and the end of a partial round, remove this plaintext.
This refines the earlier terminal-payload wording: a finished partial invocation
is not a fully resolved work. Selection, intent and automatic-turn isolation remain
unchanged during partial recovery. See `incremental-partial-recovery.md`.

G3g connects bounded scope registration to the same shared commit path; see
`incremental-scope-registration.md`. Discovery is opt-in and never expands an
explicit write boundary.
