# Memory state contract

This document defines the Core-owned state semantics that are already implemented. It is narrower than the extraction design: model prompts and future protocol fields do not become supported merely because they are discussed elsewhere.

## Current head and history

- `knowledge/` holds one current head for each stable `memory_id`.
- `history/` holds prior complete versions created by an authorized update or retraction.
- `validity` is `valid` or `retracted`. Old Markdown without the field reads as `valid`.
- A completed or cancelled todo is still a valid current record. Its task status is not fact validity.
- Forget deletes the authorized current/history artifacts. Retraction preserves identity and history, so it is not a substitute for forget.

## Create-only identity and trusted raw writes (unreleased)

`create_memory(...)` now creates only a new identity. It does not overwrite an
existing file, even when the caller supplies identical content. Reusing an ID is
an explicit conflict, not a business update or an idempotency receipt. Use the
existing `remember`/incremental work APIs for authorized replay and semantic
updates; this change does not add a new model call or an update planner.

The identity check runs under the existing Vault mutation lock and uses the
bounded Markdown scan, including `knowledge/` and `history/`. Frontmatter IDs,
not filenames, determine identity, and comparison is case-insensitive to match
the public query contract. Renamed/nested files, closed todos and retracted
heads still own their IDs. A different record at the intended destination also
prevents creation. Valid prior versions with different history IDs do not block
creating an unrelated identity.

A known existing identity or occupied destination raises `FileExistsError`.
Ambiguous or invalid matching identities keep the existing query integrity
error. An unidentifiable file, incomplete enumeration or scan limit prevents
proving absence and raises `ValueError`; Core does not silently repair, ignore
or overwrite it. An invalid file whose distinct identity is known does not by
itself block an unrelated creation. Ordinary reads retain their existing local
fault-isolation behavior. A bounded recheck detects changes during the scan.
Cooperating writers are serialized; arbitrary direct file edits after the final
check are outside that lock guarantee.

`write_memory`, `save_memory` and `add_memory` remain **trusted full-document raw
writes**, with the historical `overwrite=True` default. This compatibility path
is not a revision-checked patch API: it may replace all supplied fields and does
not create history. Existing administrative/import scripts keep that behavior;
new scripts should spell out `overwrite=True` when replacing data intentionally.
All three names also accept `overwrite=False` for the same create-only checks:

```python
# Create-only: reject an existing identity or occupied destination.
service.write_memory(memory, overwrite=False)

# Trusted full-document replacement; no CAS or automatic history guarantee.
service.write_memory(memory, overwrite=True)
```

`overwrite` accepts only a real boolean. `create_memory` always uses the
create-only policy; it has no overwrite option. Scripts that previously used
`create_memory` as an upsert must deliberately choose the trusted raw API for
full replacements, rather than silently retaining the old ambiguous behavior.
No raw write entry is exposed as a new MCP tool. Automatic processing and
explicit retention continue using their existing authorized commit paths.

This create-only guard is separate from the revision-checked `update_memory`
contract below. Trusted cross-Vault merging is not supported by this API.
Existing update/retraction history and frozen-plan recovery are not replaced
by the create-only guard. A derived-index failure after a successful raw/create write
still follows the existing raw-write error behavior; do not treat this API as
an operation receipt or retry it blindly to infer whether a prior call committed.

## Retraction

`retract_memory(memory_id, expected_revision=..., reason=...)` is a deterministic Core operation. It:

1. resolves exactly one current target;
2. compares the caller's revision with all protected current content;
3. writes the prior valid version to history;
4. keeps the same current `memory_id` with `validity: retracted` and no current assertion body;
5. rebuilds the derived index.

Repeating the operation against the current retracted revision is idempotent. A stale revision or duplicate current ID fails closed. Restoring a retracted assertion requires a newer, explicit current-state update; Core does not copy the historical body back automatically.

## Read and retrieval projection

Ordinary `read`, `read_page`, `search`, `context`, and `list_todos` exclude retracted assertions. `read(..., include_history=True)` and `read_page(..., include_history=True)` may return the current retracted head for explicit audit, including `validity` and its protected `revision`. Exact forget can still resolve the head and its linked history.

The derived active index excludes retracted heads. History indexing remains available only through APIs that explicitly include history. Closed todos remain in `knowledge/`; status filtering, rather than age-based deletion, controls their normal presentation.

## Compatibility boundary

`closed_todo_retention_days` remains accepted so existing config files continue to load, but it no longer retires the stable current identity. Optional structured fields stored in frontmatter participate in the protected revision. The incremental model protocol supports UPDATE.patch.validity and an explicit restore with current content. The legacy planner still excludes retracted heads; its older protocol is not silently extended. Deterministic Python retraction and explicit update use the same current identity and protected revision semantics.

## Review fixes and recovery boundary (unreleased)

Explicit retraction now freezes its before/after payload and operation identity in
`_state/retractions/<memory_id>.json` before writing history or the current head.
This is a local forward-recovery journal, not another model workflow. A retry of
the same request resumes its remaining writes, index rebuild and settlement.
`RetractionCommitError.applied` reports whether the current head is known to have
changed; it does not claim that the index or settlement is complete. Keep the
same expected revision and reason when retrying the interrupted operation.

After settlement, a small receipt on the current head supports the same request's
replay. It validates the protected result so a later real edit is never overwritten
by that replay. A current retracted revision also allows an idempotent index repair,
including for withdrawals created before these journals existed. The operation
never reloads an old body as current or generates another history version merely
to finish an index repair. A later edit cancels an incompatible pending snapshot.

Ordinary lock acquisition does not replay retractions. Explicit forget cancels
related retraction journals (including their saved plaintext) before deleting the
selected records. A missing target is not recreated. Corrupt journals are kept
for inspection and fail closed; there is no automatic reset. Recovery is triggered
by an explicit retraction retry, not a daemon or an extra model request.

Legacy planner eligibility is shared by normal search, project fallback,
priority-target lookup, contextual unions and projections. Withdrawn heads are
not exposed as ordinary active targets. This legacy eligibility rule is distinct from the incremental planner, whose
validity-aware identity view and explicit restore protocol are implemented.
Neither path can prove the truth of a natural-language restoration from schema alone.

Semantic duplicate comparisons exclude `field_basis`. For unresolved deadlines,
`due_anchor` contributes only the currently supported calendar fields
(`source_time`, `reference_time`, `timezone`, `precision`); source IDs do not make a
new fact. A resolved deadline compares its date and retained wording, not the
observation that supplied it. Complete provenance remains protected by revision.
Do not use the semantic duplicate digest as an authorization or operation ID.

Frozen pre-validity UPDATEs have a bounded compatibility check: the old digest is
accepted only while the on-disk target still has no explicit `validity` field, is
valid, and every other protected value matches. Current revisions use the existing
algorithm. Explicit validity, retraction or another authored edit closes that
fallback. Canonical rewrites of an old file require a fresh revision rather than
silently changing its old authorization. This does not enable mixed old/new writers;
use the controlled stop-write upgrade procedure before resuming pending work.

Malformed validity types raise controlled validation errors. Existing scanning
can skip the invalid record and keep unrelated records usable; the public integrity envelope reports malformed and conflicting records with
bounded snapshot validation. Explicit writes need a provably unique target and
can conservatively block where ordinary reads continue with incomplete diagnostics.
The public repository does not store test files; package installation and source
syntax are checked by CI, while semantic acceptance runs in a separate environment.

## Current revision-target integrity (unreleased)

`memory_revision` and explicit `retract_memory` now resolve current ownership
through the same bounded Markdown scanner used for query integrity, rather than
silently skipping unreadable records through the legacy list helper. Duplicate
claims (including case variants or a malformed second claim) raise the existing
`memory_id_conflict`/`memory_unreadable` errors. A malformed target is not absent.
Only a validated current record can supply a write revision; history-only IDs do
not become current targets.

These two APIs require provable current identity ownership. An unidentifiable
file, unreadable directory or scan limit blocks the operation with
`memory_unreadable`; this can temporarily block an unrelated revision lookup or
retraction. An identifiable bad record for another ID does not by itself block
the target. Ordinary public reads retain their existing local fault isolation;
this stricter write-authority policy is not a promise that malformed files make
all retrieval unavailable. ID case aliases use the actual on-disk memory ID for
the existing journal, receipt and history linkage. A renamed/nested current file
is updated at its resolved location, not copied to a guessed standard filename.

The scanner is rechecked before issuing a revision, before writing retraction
history and again before changing the current head. Changes observed during
preparation or history writing raise `scan_changed`, retain the pending journal
and do not overwrite the newer head. A prewritten old-version history snapshot
may remain when the head write is refused; this is not an all-files rollback or
proof that the retraction committed. A later retry with stale authorization is
still rejected through the existing revision check; a later valid edit wins.
There is one bounded verification per boundary, not a rescan retry loop.
Arbitrary direct file edits after the final check are outside the cooperating
Vault-lock guarantee; these checks do not make a filesystem compare-and-swap.

A proven missing head can cancel a **valid** pending journal without recreation.
An unreadable/ambiguous target or corrupt journal is preserved for inspection,
not silently deleted as a missing target. The existing version-1 journal and its
checksums are unchanged. Reads since preparation are revision-neutral: retry
keeps the latest resolved head's hit accounting while retaining the frozen
business state, receipt revision and original deterministic history identity.
After a write error, `RetractionCommitError.applied` is true only for the exact
protected replacement, false for the exact pre-state, and unknown for a third
revision (for example, a later authored edit that copied the receipt marker).

No model call, new MCP tool, general update API or new journal framework is added.
This repairs current revision/retraction contracts; it does not complete
FS107/FS108, all writer paths, real-host validation or migration acceptance.

## Explicit structured update (development candidate)

```python
revision = service.memory_revision("task-id")
result = service.update_memory(
    "task-id", expected_revision=revision,
    patch={"body": "The report has been delivered.", "status": "completed"},
    authorized_scopes=["project:Atlas"],
)
```

This is a trusted, exact Python operation, not a new MCP tool, automatic model
phase, semantic classifier, or replacement for `remember`. The caller supplies
all intended fields and both old and new authorized scopes. Core never invents
a missing target or silently changes update into create/upsert. Result action
is UPDATE or NO_CHANGE, with current revision, replay status and index status.
No model call is made. `authorized_scopes` is an explicit caller boundary, not
an access-control system against the OS owner of this local Vault.

The whitelist is title, body, tags, aliases, keywords, scopes, status,
completed_at, due_date, due_text, assignee, waiting_on, validity and type.
Omission preserves a field. Null clears only permitted nullable task fields;
empty lists explicitly clear list fields. IDs, timestamps, sources, field_basis,
operation receipts and arbitrary extra metadata are not patchable. Existing
custom metadata is preserved. A new scope must be registered first. Reopening
closed tasks requires `reopen=True`; restoring a retracted assertion requires
`restore=True` and a nonempty current body. Type correction requires
`allow_type_change=True` and explicitly consistent task fields. Changing status
does not fabricate a completion time. A valid assertion cannot have an empty body.

Before IO the exact request, before/after payloads, operation ID, target revision,
scope registry guard and observation time are frozen in a version-1 journal at
`_state/explicit_updates/<canonical-id>.json`. Loading validates the bounded JSON,
checksum, request whitelist and reconstruction of the same after state. Retry
uses the original expected revision and identical request. The shared Writer
writes history once, rechecks current file/scope state, replaces that actual
current path, then rebuilds indexes and settles. Read statistics are preserved.
A later valid edit wins over an old pending plan. An absent target is never recreated.

After an IO exception, `ExplicitUpdateError.applied` is true, false or unknown;
`recovery_required` remains true. It is not an assertion that index/settlement
completed. A matching settled receipt permits zero-model replay/index repair.
NO_CHANGE does not create history or advance field observation times. Pending
plans are bounded to 128 per journal kind and 16 MiB aggregate. Damage is kept,
not reset. Forget cancels saved payloads before removing the selected records.

## Exact Forget and dependency-aware maintenance

Exact ID Forget matches all validated case-insensitive identity claims and their
linked history. It may intentionally remove valid conflict copies; ordinary
reads never choose one as a winner. A damaged or unidentified file prevents
claiming a complete deletion. A unique exact title can resolve an unambiguous
identity; even a single merely fuzzy candidate needs confirmation. Old source
and intent cancellations use logical identity matching, not filename spelling.
Source keys and opaque operation identifiers are not case-normalized.

A full current/history scan precedes optional retention. Any unresolved explicit,
incremental or legacy mutation defers the optional pass instead of pruning a
snapshot it might need. Damaged authority fails with original bytes retained.
Incomplete scans defer cleanup; source rewrites and pruning recheck each current
record. This is conservative backpressure, not new automatic data deletion.
Pending explicit updates/retractions participate in freshness and migration
preflight; status observation validates but never executes them.

## Provenance after direct edits

Protected revision equality is evidence of unchanged persisted content, not
proof of the truth of an assertion. A valid exact explicit/retraction receipt
or retained applied incremental replacement revision can establish
`verified_revision`. A mismatching known proof is `external_change_detected`;
without a usable proof, old content is `legacy_unverified`. Planner input marks
these distinctions and does not present old field timestamps as verified support
for externally changed or unverified current content.

A supported update after a detected external edit clears the old unsupported
field bases, records `external_edit_observed_at` at the observation/commit time,
and supplies bases only for the fields actually changed by this operation.
This is not the unknowable original time of the user's filesystem edit. Other
current fields remain intact; Core does not rewrite actual content back from
cache. Unknown legacy provenance is not promoted into proof merely by reading it.
