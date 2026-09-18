# Existing text remember route (G4b, opt-in)

The existing `Memleaf.remember()` and MCP `remember` tool can explicitly select
incremental execution. This is an adapter to the same selected-retention runner,
budget, native comparator, compiler and shared MemoryWriter; not another planner.
The already implemented automatic route is independent. Neither route is enabled
on an existing Vault by this change. Package version and fixed prompts are unchanged.

## Selection and compatibility

```python
result = service.remember(
    "Use the stable API.", source="hermes", session_id="session",
    intent_id="the-real-user-authorization", scopes="project:Atlas",
    pipeline="incremental",
    # Supply only a known original timestamp, never an invented completion time.
    source_time="2026-09-17T10:00:00+08:00",
)
# For a transport failure only; saved responses/commits do not need this flag.
if result["retry_available"]:
    result = service.remember(
        "Use the stable API.", source="hermes", session_id="session",
        intent_id="the-real-user-authorization", scopes="project:Atlas",
        pipeline="incremental", source_time="2026-09-17T10:00:00+08:00",
        recover=True,
    )
```

The optional `process.remember_pipeline` is `legacy` by default and accepts only
`legacy` or `incremental`. The per-call `pipeline` overrides it. Selecting
`process.automatic_pipeline=incremental` alone does not change text remember.
Unknown values fail explicitly, not by falling back to another semantic pipeline.
The MCP tool adds only the same optional `pipeline`, `recover` and `source_time`
fields. There is no new MCP tool, CLI command, daemon or automatic live migration.
A supplied model/router still must meet the existing single-dispatch contract.

`content` and its `text` alias contain the actual submitted user material. Supplying
both with different values is rejected on the incremental route. One text request
selects the supplied content, not the entire originating chat. Use the existing
`remember_incremental(selected_source_refs=..., retention_request=...)` when exact
captured messages and a subtopic selection are available. Neither route invents
consent for text merely quoted by a caller; the local API caller must be trusted.

## Source without a fabricated conversation

A standalone text has no real assistant reply. It is captured as exactly one user
input with a versioned `explicit_input` control marker in the existing inbox.
Ordinary user-only turns remain incomplete; only the matching explicit selection
may process this typed input. Public capture does not expose this internal marker.
No synthetic assistant promise, final reply, completion timestamp or tool result
is created to satisfy conversation completeness.

The originating Agent remains the source, preserving native-sharing rules. An
internal, deterministic per-intent session isolates the transport from the user's
automatic conversation. Its identity is not a permanent-memory visibility filter.
Source provenance and field bases retain `input_kind=explicit_text`, the original
session ID and original turn key alongside the actual captured source location.
The marker's authorization/control hashes are not in the model projection.

The marker binds the intent, normalized scope, redacted text/request fingerprint
and origin recording policy. The snapshot/input fingerprint includes it only when
present, so ordinary legacy source digests do not change. Marker version or shape
errors are not treated as unmarked conversation. External editing invalidates the
frozen source; no trust boundary is promised against arbitrary code with file access.

Missing `source_time` stays unknown. Core keeps an unresolved relative deadline
expression rather than treating API execution time as its date. When supplied,
source-local time is preserved through retry and calendar conversion. Calling
remember today on old material does not make its old state current. Existing text
API users must explicitly opt in to this more conservative timestamp contract.

## Intent and replay

The caller should provide a stable `intent_id` for every retriable authorization.
For compatibility, a stable `event_id` or `turn_id` derives the same default intent
and event identity as legacy remember. With no identifiers, an ordinary new call
creates a fresh intent and returns it. A retry must reuse that returned identity;
`recover=True` without any stable identifier is rejected before capture.

The legacy event identity detects cross-route reuse. An old legacy authorization
cannot receive a fresh incremental budget, and switching back cannot re-extract an
incremental authorization. Existing legacy plans need their original recovery or
explicit migration. A genuinely new user authorization uses a new intent and still
compares current memory; identical text is not automatically a new memory.

Same intent with changed submitted text, source time, scope or event addressing is
a conflict. Recovery reuses the original source, run, operations and allowance;
changing a router or transport is not an authorization event. This route does not
yet merge identities across the legacy text API and arbitrary selected-message API
calls whose source sets genuinely differ. That requires explicit provenance, not
text-similarity matching.

## Recording, settlement and recovery

Capture and every unapplied source check honor the real caller session/turn's
recording policy, not just the internal session. Stop/private controls and common
redaction remain active; the marker contains no unredacted body. New partial repair
and changed-context replan reuse the existing bounded recovery entry point.

Successful standalone retention settles only its own internal source for normal
inbox cleanup. It does not advance the user's originating automatic turn watermark
or consume unrelated assertions. Both automatic schedulers skip typed standalone
inputs even after a crash before the capture receipt was saved. An ordinary partial
conversation, by contrast, is not marked ready or discarded by this adapter.

Requests still have at most two durable reservations. Repeat calls do not spend a
transport retry without `recover=True`. A saved response or frozen commit resumes
without resolving model configuration; already applied identities/history are not
reallocated. Results retain canonical run/commit state and expose compatibility
`memory_ids`, `memories_written`, `intent_id` and `processed_turns`. These counts
are confirmed operations, not a claim of model-semantic correctness or provider
billing. Failed-before-receipt infrastructure errors preserve the original run's
recovery result; callers should persist its ID rather than invent another intent.

Forget cancels existing dependent requests and frozen payloads before target
deletion. The old intent stays cancelled. A genuinely new authorized request may
submit fresh text; unavailable or explicitly purged historical source is not
reconstructed from hashes. Partial/control-state GC and bounded recovery windows
remain the existing separate policy; this is not unlimited exactly-once storage.

## Damaged control state

Integration testing exposed a shared reader that converted invalid processed JSON
into an empty state. Capture and execution now share strict root reading. Missing
state on a genuinely unused path is not equivalent to a missing required ledger
in an established Vault. Established layout/authority checks reject the latter;
reopening, capturing or changing routes must not reconstruct empty authority and
reissue an old work budget. A read-only query of valid committed Markdown may
remain available while progress is reported as unknown.

Malformed JSON, duplicate keys, unsupported versions, invalid session/event
containers, symlinks and IO failures are likewise not silently repaired or
overwritten. Payload-specific run/commit validators continue checking their own
checksums and versions. Minimal supported older dictionaries receive in-memory
missing-field defaults; custom keys are preserved. This compatibility does not
authorize manufacturing an entirely missing required control file. See
[the closeout scope](development-closeout.md) and the processed-state/lost-authority
regressions for the distinction.

This deliberately changes corrupted-state behavior on both routes: operations
requiring that control authority fail visibly, retain the original file and make
no model call. It is not a general lost-ledger restoration tool or proof that an
operator cannot manually delete state. It does not reset budgets, infer missing
consent or report a damaged state as NO_MEMORY.

## Cutover and validation

Keep legacy defaults until controlled stop/backup/upgrade/restart and the planned
live acceptance. Older code does not understand the standalone marker and cannot
be expected to enforce its cleanup semantics. Changing a config key does not kill
an old process or grant safe mixed-version writes. No default activation, production
Vault mutation, release, real Flash calls or native Hermes/Windows/macOS acceptance
was performed by this increment.

Deterministic tests cover one-user projection, origin provenance, same/new intents,
legacy-route collisions, explicit and configured selection, unchanged originating
receipts, cleanup replay, deadlines, scope and native guards, paused capture,
malformed control state, concurrency, writer/index faults, actual subprocess exit,
partial repair/replan and Forget. They are separate from live semantic fidelity.
