# Incremental commit and recovery (G3b, staged)

This opt-in Core/Python bridge submits G3a `items` proposals through the shared
MemoryWriter. It does not switch `process()`, `remember()`, Hermes hooks or MCP
tools to a new model pipeline. These APIs make **zero model calls**. A supplied
response is a proposal, not proof of semantic correctness. Validate on isolated
Vaults before production use.

## Explicit authorization

1. Call `preview_incremental(source=..., session_id=..., turn_id=..., ...)`.
2. Obtain the response through an authorized caller workflow.
3. Call `apply_incremental()` with the same selection, `response`, original
   `expected_snapshot`, and a stable, caller-owned `intent_id`.
4. On a recoverable I/O error, `IncrementalCommitError.result` identifies the
   work. Explicitly call `resume_incremental(work_id)` after resolving the fault.

Example of the final two steps (the selected turn must already be captured):

```python
from memleaf import Memleaf
from memleaf.incremental_commit import IncrementalCommitError

core = Memleaf("/path/to/isolated-vault")
selection = dict(source="hermes", session_id="example", turn_id="turn-1",
                 scope="project:Atlas", priority_memory_ids=["mem-task"])
preview = core.preview_incremental(**selection)
# response_json comes from this exact preview request, not invented state.
try:
    result = core.apply_incremental(
        **selection, response=response_json,
        expected_snapshot=preview["snapshot_id"], intent_id="stable-authorization-id",
    )
except IncrementalCommitError as error:
    # Keep this ID for a later explicitly initiated recovery, without a new LLM.
    work_id = error.result["work_id"]
```

The receipt binds the normalized selection, original snapshot, and parsed JSON
response. Whitespace-only JSON changes are equivalent; altered decisions,
boundaries or snapshots under the same intent are rejected. A genuine new
user authorization can create new work but still must match current state.
The key is Vault-local and includes source/session/turn/intent. This is not a
remote authorization server; trusted callers supply the intent identity.

First application rechecks source/context, recording policy, targets and scope
under the existing Vault lock. A live legacy worker or old pending frozen plan
must be finished or explicitly migrated; this API never steals its ownership.
`allow_new_scopes=True` is rejected until new registry transactions are wired.
Existing scopes and explicitly authorized boundaries are usable. The preview
may describe capabilities not yet authorized by this staged writer.

## Complete target groups, shared persistence

All operation IDs, permanent IDs, complete before/after payloads, revisions and
dispositions are frozen durably before the first knowledge/history write.
The existing MemoryWriter owns history creation and atomic head replacement.
A missing UPDATE target never becomes CREATE; an occupied CREATE ID is not
silently replaced. Full-target CAS preserves concurrent edits.

Same-target dependent lines form one candidate version. An invalid line rejects
that whole group, while unrelated valid groups can succeed. A redundant
NO_CHANGE for an updating target joins its evidence, avoiding a second CAS on
the obsolete pre-update version. Patch omissions preserve fields, custom
metadata, creation time, and still-effective deadlines and assignments.

NO_CHANGE settles evidence without inventing a new business history or advancing
all field clocks. NO_MEMORY is a durable no-memory result without a fake target.
DEFERRED/invalid/missing decisions remain visible. Empty `items` is not complete
coverage. Exact-state CREATE deduplication excludes provenance, but is not a
semantic same-entity classifier and cannot replace real-model recall testing.

## Recovery and visibility

Versioned/checksummed receipts live under `incremental_commits` in the existing
processed ledger. Markdown remains the permanent memory fact source. Limits
are 8 MiB per work and 16 MiB for this receipt collection. Capacity overflow
fails explicitly: there is no silent eviction, counter reset or unlimited
historical replay guarantee. Full receipt-retention/GC is not yet implemented.

An operation is prepared, applied, then settled, or becomes blocked/cancelled/
deferred. Applied is proven by the complete replacement revision and a Core
operation marker, not only the source ID. Before a later supported mutation,
the common boundary records a prior exact head as applied; it does **not** replay
pending writes. Forget can therefore cancel pending content before replay.

Retry reuses frozen IDs and decisions, recognizes already-written heads, does
not duplicate history, and preserves later changes. A duplicate ID blocks that
target rather than unrelated valid operations. Corrupt receipts fail explicitly
and are not rewritten as an empty ledger.

Index status is dirty before any head write. Index failure does not undo a
business commit or trigger a model call. While dirty, query indexing can be
derived in memory from current Markdown; explicit resume repairs disk indexing.
No raw inbox is injected to pretend it is current committed memory.

Source settlement and `receipt_settled` are persisted in one final ledger write.
A crash after the last NO_MEMORY/NO_CHANGE operation receipt but before source
settlement still requires recovery. Successful operation rows alone do not
prove the final work acknowledgement was completed.

Terminal operation receipts omit frozen before/after bodies. Normal history
retains the previous version according to policy; explicit Forget remains a
separate deletion contract. Identifiers and unresolved diagnostic needs may
remain in receipts; they are not an unlimited conversation archive.

## Coexistence with legacy scheduling

Before any unapplied operation, the source and used context window must remain
unchanged and recording must be allowed. New/revised context blocks stale
unapplied work, without undoing already-applied siblings. Revision previews pin
known prior targets. A new proposal requires current context and a new explicit
authorization, not modification of an old frozen response.

Staged turns cannot silently re-enter the legacy model pipeline on partial or
I/O failure. Independent later turns remain schedulable. Source receipts use
the common processed-turn ledger, contiguous local watermarks and configured
`process.inbox_cleanup_hours`. Pending/partial work protects its source from old
cleanup. Until selective recovery adds semantic closure, unresolved staged receipts retain this
protection; old failures are not silently forgotten to make coverage complete.

## Retraction and Forget

`validity=retracted` preserves identity and history; a supported newer explicit
confirmation with a current body may restore the same identity. Todo status
completed/cancelled is distinct from fact validity. Scope/revision rules remain
in force; no low-level overwrite bypass is used.

Forget cancels target frozen payloads before deleting the selected current and
history group. Uncommitted siblings sharing that source are also cancelled and
reported. Already-committed independent siblings are not deleted. This does not
claim semantic erasure of all shared raw blocks, host logs or external backups;
source erasure requires its own explicit scope.

## Results and stage boundary

Results separate execution, coverage, operations and index state. Operation
counts and compiler-issue counts are distinct. `model_calls=0` refers to this
Core API, not to the cost of a response generated externally. No semantic
quality pass is inferred from complete coverage.

G3c separately connects opt-in dispatch and the shared one-plus-one budget;
G3d adds read-only native comparison through this same commit bridge. See their
contracts. Still not enabled: semantic repair/replan, default agent-route
replacement, general new-scope transactions, automatic unresolved-work closure,
or real Flash/Windows/macOS host acceptance. These are not hidden behind an
experimental success flag. Uncooperative external file writes are not made
transactional by the Vault lock.
