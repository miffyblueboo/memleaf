# Incremental scope registration (G3g, opt-in)

G3g connects scope discovery to the existing incremental snapshot, compiler,
commit journal and shared MemoryWriter. It does not introduce another writer,
registry, database, model stage or background service. Markdown remains the
memory fact source. Default process/remember/Hermes/MCP routing is unchanged.

## Caller boundary and identity

The existing `allow_new_scopes` preview/apply flag is now also available on
`run_incremental`, `process_incremental` and `remember_incremental`. It defaults
to false, must be a boolean, and is bound into the original arguments/snapshot.
Changing it during a retry is not a new authorization. Partial recovery inherits
it and cannot broaden the original scope boundary.

```python
# A complete, permitted source turn must already exist in an isolated Vault.
selection = dict(source="hermes", session_id="example", turn_id="turn-1",
                 allow_new_scopes=True)
result = service.process_incremental(**selection)
# Or prepare a request and apply an externally supplied response using the SAME
# selection, original expected_snapshot and an authorized stable intent_id.
```

The flag allows the model to propose a new `project:name`; it does not override
an explicit write scope. An Atlas-only work cannot write Beacon even with the
flag enabled. A caller's explicit, previously unregistered scope is already an
authorized named boundary; an accepted memory in it can register that exact node
without granting arbitrary project discovery. New domain/portfolio structures,
parents, filesystem bindings or aliases are not model-editable.

The snapshot includes the bounded configured scope catalog, not just scopes
of selected memories. Known canonical keys, case variants, and explicitly
declared same-kind aliases are resolved to the existing identity. No fuzzy name
matching, inferred client mapping or topic keyword classification is performed.
Ambiguous aliases fail explicitly. `global` and `unscoped` are special values and
do not create registry nodes. Scope changes on an existing memory still require
both the old and new scope to be allowed, and retain its ID and history.

## Frozen guard and visibility

A versioned guard hashes the validated nodes from the current scope registry.
It includes alias/parent/path metadata in the hash, but never copies the config
payload, paths or credentials into the commit journal. The model sees canonical
scope names and declared aliases only. Scope registration is not an opportunity
to expose the rest of the configuration.

The configured registry is bounded at 256 nodes for this staging route. The
existing full request cap still includes the fixed prompt, sources, selected
memories, scope catalog and recovery instructions. Exceeding a bound reports a
context error; it does not silently omit a needed target. These are implementation
limits, not measured model capacity or a promise of optimal retrieval.

Before a still-unapplied CREATE/UPDATE/NO_CHANGE, Core rechecks the guard. Alias,
parent, node or path changes invalidate the frozen decision. Additions made by
this work's already-applied operations are recognized and do not invalidate its
own siblings. Unrelated non-scope config changes do not change this guard.
NO_MEMORY needs no registry write and is not rejected solely by a scope change.

## Commit ordering and recovery

Registration is an operation dependency, not a separate semantic result:

1. Compile/validate the complete memory and freeze IDs, before/after Markdown,
   expected revision, scope guard and any missing scope keys.
2. Write the memory through the existing MemoryWriter and prove it applied.
3. Merge only its missing empty nodes into the latest validated configuration.
4. Settle the operation, refresh derived indexing and finish the source receipt.

A rejected, deferred or NO_MEMORY candidate never pre-creates an empty project.
Multiple accepted memories for one new scope reuse the one node. Retraction does
not register a new scope for a now-invalid assertion. There is no automatic
registry garbage collector or deletion of an independently configured node.

Memory and config are separate files. This is a recoverable ordering, NOT a
cross-file atomic transaction. A crash between steps 2 and 3 may leave the current
Markdown visible before the config node is added; the existing Scope Map can
derive that scope from the head. The operation remains `applied`, with a pending
`scope_registration`, and the receipt reports recovery_required. It is not
reclassified as an uncommitted CREATE or semantic DEFERRED.

`resume_incremental(work_id)` (or the canonical runtime recovery) completes the
same registration/index/receipt without a model call, new ID or duplicate history.
If config writing succeeded but its acknowledgement failed, recovery observes
the existing node and does not append or overwrite it again. Existing node
metadata and unrelated current configuration values are preserved. The current
save_config serializer is reused; comments/formatting are not guaranteed to round
trip unchanged.

A post-head alias conflict does not rename the already-written fact or overwrite
the new configuration. It leaves a visible scope-registration error for correction
and recovery. A subsequently removed, re-scoped or retracted head is not used to
recreate its former missing scope. Forget cancels pending registration with its
frozen operation; independently committed sibling records are not deleted.

Registered configuration is merged under the existing Vault lock, with an extra
raw-byte check immediately before replacement to detect an external edit during
preparation. Arbitrary external writers do not obey that lock; a final
check-to-replacement race still exists. This is not distributed consensus or a
promise to make an editor and a file writer a single atomic transaction.

## Journal compatibility and partial recovery

New incremental commit receipts use version 2 and require a scope guard. The
reader continues to accept version-1 receipts under their original contract;
version-1 receipts cannot contain new registration effects. Wrapper/payload
versions must match, and saving legacy work preserves its version. Old pending
work is not silently granted scope-discovery capability or rewritten with fresh
source/target hashes. Completed legacy receipts remain historical results.

Old software cannot be made to understand version 2 by adding a field. Use the
planned controlled stop/backup/upgrade procedure; do not mix old writers with
new scope-dependent work. An old pending model request whose snapshot no longer
matches the expanded registry view must be reported as stale, not automatically
reissued under a new budget.

G3f recovery round-trips the guard and aliases. Own successful scope additions
are not new semantic context. A real external registry change can justify a
bounded replan within the SAME original write authorization and remaining request
allowance; mechanical repair rejects such a change. Inherited settled operations
keep their operation IDs and are not re-executed or re-registered. The public
five-action protocol and fixed system prompt are unchanged.

## Deterministic validation and remaining work

Tests cover false-by-default discovery, explicit boundaries, registered and
ambiguous aliases, similar-but-independent projects, partial siblings, duplicate
registration, changes before a head write, config failures before/after replace,
real process exit, concurrent callers, existing metadata preservation, source
revision, removed/re-scoped heads, Forget, legacy/v2 receipts and partial recovery.
They use temporary Vaults and fixed responses, not real model semantics.

Default agent-route integration, lifecycle/GC policy, mixed-version migration and
real Flash/multi-turn/native-OS acceptance remain separate work. No version bump,
release, production Vault mutation or extra online review is authorized by this
staging increment. A passing execution contract is not proof that the model
always assigns the right business project.
