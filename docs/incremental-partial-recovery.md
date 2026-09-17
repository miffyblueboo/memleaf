# Incremental partial recovery (G3f, opt-in)

This batch adds one explicit, bounded partial continuation to the canonical
incremental runner, source-work budget and shared MemoryWriter. It does not add
an automatic reviewer, a second dispatcher, new business heuristics, a daemon or
an alternate write path. Default process/remember/Hermes/MCP routes remain unchanged.

## API and two recovery modes

```python
# first is the result of process_incremental / remember_incremental / run_incremental.
repaired = service.recover_incremental_partial(first["run_id"], mode="repair")

# Use replan only when real new context is available. Optional IDs pin actual
# current records missing from the previous catalog; they do not grant write scope.
continued = service.recover_incremental_partial(
    another_partial["run_id"], mode="replan", context_memory_ids=["mem-known"],
)
```

These illustrate different partial works, not a required repair-then-replan chain.
Each work can bind at most one partial round. A call which reports unchanged
context, exhausted model allowance or no safe structural repair does not bind a
round, change its receipt or call a model. An already-bound round resumes the same
request and commit; changing its mode/context IDs is a binding conflict.

**repair** is entirely local. It accepts a scalar evidence string only when it
names an exact supplied reference and turns it into a singleton list. It accepts
a singleton scope list only when that element is an exact known scope value.
It does not invent action, target, status, at, deadlines or missing assertions;
it does not drop unknown fields, guess numeric references, reinterpret namespaces
or rewrite body/title values. The original failed target group is compiled as a
whole. An unresolved row cannot be recovered just because an unrelated row parses.
A changed referenced target, changed source context or changed native guard blocks
repair; silently adapting an earlier business decision is not structural repair.
No model is used for repairs that can be uniquely performed by code.

**replan** uses the original work's remaining allowance, not a new intent or run.
There must be actual changed source context, a changed current target/native
context, or an actual previously unprovided catalog record. Reordering references,
read counters, changing transport and this work's own writes are not new context.
A supplied ID must resolve to a readable current record within the unchanged
candidate/request limits. Missing data is never reconstructed from text hashes.
Context changes only enable consideration: relevance and business meaning remain
with this single planner, not a keyword relevance gate.

## What the model sees, and what it may change

Only isolated, originally unresolved source blocks are `new`. The original user
selection and retention request remain binding for explicit retention. Other
selected-turn messages and the bounded necessary prior/subsequent window are
context; additional messages are not independently extracted in this old work.
Their normal automatic work remains separate. An edited/superseded original
source or missing required prior context requires coordination, not rebinding the
old work to a different source revision.

The recovery input contains a small `recovery.settled` operation summary without
old response bodies or permanent IDs. Current catalog records still provide the
business state needed for matching. Previously settled target identities are
read-only to UPDATE. Existing e/m/s bindings are preserved; added context receives
new references. Native targets remain read-only and sharing is rechecked.

The normal system prompt is byte-for-byte unchanged. Replan alone appends a short
recovery instruction; the full request is checked against the existing 128 KiB
cap. There is no additional full failed-response transcript or routine review.
Keeping necessary source context does not mean asserting that every retained
context sentence was semantically necessary, or that Top-K found every match.

At message-block granularity, a successful statement and an unresolved statement
can share a block. Replanning such a block could duplicate or reinterpret the
success. This batch conservatively excludes shared settled blocks and entire
failed target groups that cross that boundary. An unlocated error is not silently
claimed resolved by a new response. Such residuals remain explicit rather than
being converted to NO_MEMORY. This is a known precision limit, not a claim that
all partial results automatically converge. Exact local row repair may still
resolve a uniquely bound structural error without re-extracting the shared block.

## Budget and stopping

Normal response retry, transport recovery and replan share the existing maximum
of two durable reservations. If the normal path already used two requests,
replan sends none. Local repair needs zero requests. A replan response that is
invalid, empty, oversized, or still partial does not cause a third request or an
extra partial round. No mode switch, client nonce or changed context resets the
budget. Genuine later user authorization remains separate from retrying this work.

Repeated ordinary process/recover polling still returns the partial receipt;
only this explicit partial API arms a new round. Saved responses and prepared
commits resume with zero calls, including without model configuration. Reservation
before dispatch remains conservative across a process exit; metrics do not claim
that unknown dispatch outcomes prove provider billing or non-billing.

## Immutable decisions and cumulative recovery receipts

Before the initial response can be committed, the runner durably saves a bounded,
versioned recovery seed alongside that response. If the result is partial, it
retains `partial_basis` (original snapshot and parsed rows) after releasing the
normal request/response fields. This is necessary recovery state, not a permanent
conversation archive. Complete/cancelled/failed results, and the end of the one
partial round, erase it. Old partial receipts lacking the seed fail explicitly
with `partial_recovery_basis_unavailable`; no lost model decision is guessed.

The same run/budget identity continues. A deterministically related child commit
work uses the same commit bridge. It carries the original settled operations with
identical operation IDs and states; they are skipped, not rewritten or rechecked
as current assertions. Only definite unresolved bindings are replaced. Remaining
unlocated/shared errors and deferred operations stay visible. The original commit
receipt is immutable. Permanent IDs for new operations are frozen before any write.

The child source receipt accounts for the full original selected source set,
not just the recovery subset. Explicit remember still settles only its own work,
not the automatic whole-turn watermark. A completed cumulative child proves its
parent resolved and releases the parent's context protection without a separately
racy GC flag. Parent/source/authorization/accepted-operation relationships are
validated. Partial or failed children retain the existing conservative protection;
generalized journal retention and migration/GC remain separate work.

Index and source-receipt failure resume the child operations, not the model. An
already applied operation is recognized before any stale remaining operation is
rejected. The run retains the prior successful commit result even if the recovery
fails; failure is not reported as though nothing was ever saved.

## Privacy, concurrency and verification

Sources, old and added target IDs, and native guards remain part of cancellation
and revalidation. Forget erases retained partial plaintext and cancels any in-flight
response. Share revocation cannot resend native bodies from the old seed. Recording
revocation, source revision, external target edits and missing budget state block
unsafe continuation. The Vault file lock is not held during network calls; the
existing durable owner token fences competing runners. This remains a trusted
local Python API, not a remote authorization or filesystem sandbox.

Public deterministic tests cover isolated/shared sources, exact structural repair,
new-context gating, old receipts, source/target/native revisions, same/new selection
boundaries, scope denial, unchanged accepted IDs, timeout and exhausted allowance,
write/index/ledger faults, native NO_CHANGE, Forget, concurrency and actual process
exits before/after the second response. They use temporary Vaults and local model
substitutes. Real Flash semantics, native Windows/macOS/Hermes acceptance, default-route
integration, lifecycle GC and controlled migration are not
claimed complete by this batch.

G3g connects bounded scope registration to the same shared commit path; see
`incremental-scope-registration.md`. Discovery is opt-in and never expands an
explicit write boundary.
