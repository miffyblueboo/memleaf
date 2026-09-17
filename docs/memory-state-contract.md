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
