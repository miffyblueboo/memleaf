# Explicit revision-checked updates (unreleased, reconciled contract)

`Memleaf.update_memory` is a trusted Python API, not a new MCP/model tool.
It accepts a caller-selected current ID, a required 64-character protected
`expected_revision`, and a nonempty field patch. Missing/current-history-only,
ambiguous or unreadable targets never become implicit CREATE/upsert operations.

```python
revision = service.memory_revision("mem-item")
if revision is None:
    raise ValueError("The current record does not exist")
result = service.update_memory(
    "mem-item", expected_revision=revision,
    patch={"status": "completed", "waiting_on": None},
)
```

## Fields and authority

Allowed fields are title/body, tags/aliases/keywords, scopes, task status,
completed_at, assignee/waiting_on, deadline or direct due_date/due_text,
validity, and an explicitly permitted type correction. Lists are bounded by 64
items of at most 256 characters; body is bounded by 16384 characters and other
text by 512. Existing arbitrary custom metadata survives but cannot be patched.
ID, created, sources, field_basis, counters and receipts are not editable keys.
Omitted fields survive. Only documented nullable fields can be explicitly clear.

Reopening requires `reopen=True` and status active. Restoring a retracted fact
requires `restore=True`, validity valid and a current nonempty body. Type changes
require `allow_type_change=True`; incompatible task fields must be explicitly
cleared. A completed_at value requires a timezone and completed status; omitting
it never invents an execution time. Withdrawal can use `retract_memory`, or an
explicit validity=retracted update, with the original identity and history.

A call without authorized_scopes grants only the selected target's existing
scopes. A scope move needs `authorized_scopes=[old_scope, new_scope, ...]`
covering both sides, with an already registered destination (or global/unscoped).
It does not create a scope registry node or infer an alias.

`deadline={"text": "selected expression"}` uses optional `source_time` to resolve
a calendar value; a relative expression without that anchor remains unresolved.
`deadline={"clear": true}` records cancellation. It cannot be combined with
direct due_date/due_text in one patch. Missing source time is unknown; the edit
has an observation time, not an invented source or completion timestamp.

## Recovery and outcomes

New plans use a checksummed version-2 envelope, frozen before/after, original
revision, operation identity and scope guard. The shared Writer creates history
and revalidates before changing the head. Index and receipt settlement are
separate from the business write. Only an explicit same-request retry resumes
this plan. Forget validates/cancels pending plaintext before deleting targets.

The response identifies UPDATE or NO_CHANGE, current revision and zero model
calls. UPDATE includes operation_id, index status and the equivalent legacy
replayed/already_applied flags. Failure exposes recovery_required and an applied
value of true/false/unknown according to the protected current revision.
NO_CHANGE makes no new history or timestamp and reports index not_refreshed.
Replay rechecks the head after index work, rather than returning outdated success.

A new request cannot replace another matching pending operation. If the head
has independently changed, an invalid or stale request retains the old payload;
a validated new-current-revision request may replace it. Same-request recovery
preserves later read counters, but counters do not create business authority.
The existing new-plan limit is 128 and the inventory byte budget is bounded.

Both earlier local v1 layouts can resume exact validated frozen operations; all
new writing goes through one manager and one shared Writer. Simultaneous layouts
claiming one ID are rejected. See `candidate-reconciliation.md` for the exact
compatibility choices and test adaptations.

These are local cooperative-file guarantees, not multi-file ACID, an operating
system atomic CAS, authentication against arbitrary code with Vault access, or
real model/host acceptance. Raw write_memory/save_memory/add_memory still expose
the separately documented trusted whole-record replacement contract.
