# Explicit body-only compaction

This change closes FS138–143 and the protected-field portion of E09 in the v4.1
redesign. It is not runtime receipt compaction (`maintain-state`), automatic
extraction, a task-state update, or a multi-ID deduplication service.

## One existing entry, one narrower model job

`service.compact(model=..., router=...)` keeps the existing explicit maintenance
entry and threshold/low-priority candidate selection. Normal extraction still
never invokes it on its return path. The configured threshold is a local storage
estimate, not a claim about a model's context capacity. No default route,
package version, incremental prompt or automatic request allowance changes.

The model now proposes only:

```json
{"memories":[{"source_memory_ids":["task-1"],"body":"Shorter equivalent body."}]}
```

Each row addresses one exact supplied ID. Omitted rows and an empty array mean no
change. Identical bodies are also a no-op. Unknown IDs, duplicate rows, merged
IDs, and additional fields are rejected before any new compaction journal or
history is written. A legacy full-summary response is no longer accepted for a
new request; prompt and parser change together. Existing journals contain raw
Markdown and hashes, not these JSON proposals, and remain readable unchanged.

The compact prompt guides equivalent shortening: preserve qualifications,
negation, quantities and unresolved points. It does not repeat business-specific
rules or ask the model to re-decide value, type, scope, responsibility or dates.
Current status, responsibility and deadline text are read-only context. Internal
field-basis payloads, arbitrary custom metadata and full source logs are not sent.

Core clones the complete current record, then changes only its body and technical
`updated`, `compacted_at` and `compaction_source_ids` metadata. The permanent ID,
validity, type, title, tags, aliases, keywords, scope, status, completion time,
deadlines (including unresolved or explicitly cleared metadata), responsibility,
source list, field basis, custom extensions and read counters stay unchanged.
Field basis still describes the original business evidence; a compression time
is not a newer business assertion. A preflight comparison additionally protects
these fields even after parsing. History preserves the previous body and source
list, rather than silently rebounding provenance as part of this operation.

The old complete-summary reconstruction, canonical multi-source selection and
provenance union are removed from new planning. Multi-ID merging is deliberately
not available here, including for facts. A shorter sentence does not establish
that several memories are the same business object.

## Bounded inputs and visible limits

The existing candidate ratio selects at most 32 whole records per invocation.
The full fixed prompt plus dynamic prompt must fit 128 KiB UTF-8 before backend
resolution. The response has the same 128 KiB bound and at most 32 proposals.
Targets are never text-truncated, silently split or retried under a new batch.
An oversized required body causes an explicit error. There is one call into the
selected backend, no added review, date inference or repair call. `backend_calls`
counts this invocation boundary, not independently observed provider billing or
hidden transport retries within an externally supplied backend.

The output must reduce both the existing content estimate and the actual stored
record bytes. Results expose `record_bytes_before` and `record_bytes_after` for
changed records; these exclude the retained historical copy and are not total
Vault-disk savings. `active_tokens_*` remain local estimates, not tokenizer or
latency measurements. A more concise body is not proof of semantic equivalence:
real model acceptance still has to inspect the preserved business meaning.

## No unrelated cleanup

Body compaction no longer runs retention first. A no-op or malformed response
must not prune unrelated histories or trim source lists. Explicit retention and
runtime receipt maintenance remain separate operations under their existing
contracts. The compatibility `retention` result states `not_run` rather than
claiming cleanup success.

## Snapshot and recovery

The existing bounded current-Markdown scan detects malformed records, duplicate
identities and path problems before dispatch. Because this maintenance request
selects from the entire eligible inventory, an incomplete inventory blocks new
compaction instead of silently choosing a claimant. Ordinary public reads remain
independently available according to their existing integrity contract.

Retracted heads are not candidates. Closed valid Todos remain candidates and
must remain closed. For this batch the old rollback journal still addresses
canonical paths; a relocated target is rejected before dispatch, not copied into
a canonical path or silently renamed. Supporting arbitrary relocation would
require a separately versioned path-recovery contract.

The model runs outside the Vault lock. Commit uses the existing mutation boundary,
reconciles previously applied incremental operations, rescans for ambiguity and
rechecks each selected target's path and content. Concurrent edits, retraction,
deletion or Forget invalidate an unapplied proposal. No hash grants new scope or
restores an absent target.

The existing version-1 compaction journal and rollback policy are unchanged.
Interrupted old multi-source journals can still restore their exact staged
sources; they are not reinterpreted as the incremental forward-commit journal.
An index failure rolls back this maintenance transaction. A subsequent supported
mutation can finish recovery without a model call. Starting a fresh explicit
compaction afterward is a new invocation, not a promise to cache or replay an
unrecorded model response. Arbitrary external programs that ignore Vault locking
remain outside an atomic cross-program guarantee.

## Verification

New deterministic tests cover completed/active/cancelled state, exact protected
metadata and history, oversized provenance, read-only context, old response
rejection, distinct facts, bounds, no-op behavior, malformed/duplicate files,
relocated paths, concurrent edits, Forget, index failure, an old multi-source
journal and a real child-process exit after head replacement. The existing
public suite stays unchanged. All use synthetic temporary Vaults and fixed
backends: no private conversations, native note files or live model are used.
Native matrix results must be associated with the exact final candidate. This
document does not authorize a release, production activation or real-model run.
