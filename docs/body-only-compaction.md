# Explicit body-only compaction

This change closes FS138 / FS142 / FS143: rewriting a memory body must not reopen
a completed task or remove its deadline. It narrows the existing `compact()`
operation; it does not add a workflow, model review, writer or background task.
Normal extraction, the incremental prompt and the two-request limit are unchanged.

## Model and Core have different authority

The compact model sees selected bodies plus read-only context such as type,
project, task state, responsibility and deadline. It returns only:

```json
{"memories":[{"source_memory_ids":["an-existing-id"],"body":"Shorter body."}]}
```

Every replacement names exactly one supplied record. Independent records remain
independent even when their titles or prose overlap. An empty array or an unchanged
body is a no-op; omitted candidates are not deleted. Longer replacements fail the
existing local size check. The estimate is not a model-token or quality metric.

Core copies the complete source snapshot and changes only the body, `updated`
and the compaction timestamp. Existing compaction ancestry is retained (new heads
receive their self-ID). Identity, title, tags, type, scopes, aliases, keywords,
validity, task state, completion time, calendar date, raw deadline, responsibility,
waiting-on, field evidence, original creation, counters and custom metadata stay
unchanged. A source's nested metadata is copied, not shared with the replacement.
Retracted heads are not sent for rewriting.

The new compact-specific system text and output schema are smaller than the old
full-memory output protocol. This is not the normal incremental system prompt.
It remains one existing `purpose=compact` call; no post-compaction semantic judge
has been added. A shorter body alone is not proof of semantic equivalence. The
model must still preserve conditions and negation, and actual model acceptance
remains a separate requirement.

## Compatibility without field authority

Already integrated callers can return the old full-field compact row. It is
accepted only when every supplied non-body field equals the frozen source.
Omitted optional fields are retained, not defaulted; explicit changes are rejected
rather than ignored because the proposed body might depend on that change.
Unknown fields and references are rejected. New multi-ID merges are disabled for
all types; this command is no longer a general identity-consolidation operation.

Existing selection thresholds and candidate ratios remain. The legacy retention
step before selection remains a separate, pre-existing operation with its own
report; preservation here is relative to the snapshot taken after that step.
This change does not certify all retention policies or implement time-window GC.

## Concurrency and recovery

The model runs outside the Vault lock. Commit uses the existing mutation boundary,
checks source hashes again, and rejects duplicate current identities both at
selection and commit. An external edit or a new duplicate cannot silently become
the winner of a dictionary lookup. No new commit or rollback state is introduced.

The version-1 compaction journal keeps its rollback interpretation. Pending old
multi-source transactions still restore the original sources; the new one-record
proposal rule is not applied retroactively to an old recovery journal. History,
staging, canonical replacement, index repair and journal cleanup keep their
existing order. Failed writes can roll back the staged original; a hard process
exit is recovered before the next supported mutation. This does not turn old
rollback journals into incremental forward-commit plans.

Tests use synthetic temporary Vaults and deterministic responses. They exercise
closed/active/cancelled records, fact/preference types, optional clears, exact
legacy echoes, rejected field drift and merges, duplicate identities, external
edits, index rollback, old multi-source journals and a real process exit after
replacement. No actual Flash call, production Vault, default activation, version
change or release is implied.
