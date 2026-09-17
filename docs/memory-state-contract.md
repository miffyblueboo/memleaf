# Memory state contract

This document defines the Core-owned state semantics that are already implemented. It is narrower than the extraction design: model prompts and future protocol fields do not become supported merely because they are discussed elsewhere.

## Current head and history

- `knowledge/` holds one current head for each stable `memory_id`.
- `history/` holds prior complete versions created by an authorized update or retraction.
- `validity` is `valid` or `retracted`. Old Markdown without the field reads as `valid`.
- A completed or cancelled todo is still a valid current record. Its task status is not fact validity.
- Forget deletes the authorized current/history artifacts. Retraction preserves identity and history, so it is not a substitute for forget.

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

`closed_todo_retention_days` remains accepted so existing config files continue to load, but it no longer retires the stable current identity. Optional structured fields stored in frontmatter participate in the protected revision. The automatic model protocol does not yet emit `validity`; retraction is currently a deterministic Core/Python operation, and retracted heads stay out of that legacy planner's candidate projection until the protocol can represent an explicit restore safely.

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
not exposed as ordinary active targets. This is still **not** automatic semantic
restoration support: the future planner must carry an explicit validity-aware
identity view before it can reason about retracted targets.

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
can skip the invalid record and keep unrelated records usable; a full scan-quality
report for all malformed files remains a separate design item, not a claim of this
patch. Public tests are included in the source distribution and use temporary
Vaults only, with no model or network access:

```bash
PYTHONPATH=src python -m unittest discover -s tests_public -p 'test_*.py' -v
```
