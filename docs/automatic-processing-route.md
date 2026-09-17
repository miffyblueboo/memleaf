# Automatic processing route integration (G4a, opt-in)

This increment routes the **existing** `Memleaf.process()`, CLI `process`, MCP
`process` and detached job worker to the already implemented incremental runner.
It does not add another planner, model review, job queue or writer. Existing
`process_incremental` and selected `remember_incremental` remain single-turn
facades on that same runner, budget and commit journal.

The default is still `legacy`. This is an integration for isolated evaluation,
not live Flash/Hermes acceptance, a release, or permission to change a production
Vault. No existing config, memory, model route or version is automatically migrated.
The existing text `remember()` is not rerouted in this increment. Selected-source
explicit retention already uses the shared incremental runner, but the old text
API's authorization/selection mapping still needs its own compatibility work.

## Select the route, not a second pipeline

The optional configuration field is:

```yaml
process:
  automatic_pipeline: legacy   # legacy | incremental; default remains legacy
```

A missing field preserves legacy behavior. Unknown names, nonstrings and malformed
configuration fail explicitly; they do not fall back to a different model pipeline.
After operator-controlled preparation on an isolated copy, `incremental` may be
selected in that copy's configuration. No command in this batch switches it for you.

The optional per-call override affects that call only:

```python
result = service.process(source="hermes", session_id="session",
                         pipeline="incremental")
# Only an explicit request may spend an existing transport failure's remainder.
result = service.process(source="hermes", session_id="session",
                         pipeline="incremental", recover=True)
```

The CLI uses the same path:

```sh
memleaf process --vault /isolated/vault --pipeline incremental --dry-run --json
memleaf process --vault /isolated/vault --pipeline incremental --json
```

`--dry-run` executes on a temporary copy through the chosen route. It may call the
configured model, so it is not a free/read-only model evaluation. No real model
was called during this increment's tests. Test callers can supply a deterministic
`model=` or constructor model/router subject to the existing fixed single-dispatch
backend contract. Configured backend failures never fall back to legacy stages.

MCP extends the existing `process` tool with optional `pipeline` and `recover`;
`background:true` freezes those controls in the existing queue. No new MCP tool
or host plugin framework is introduced. Hermes' existing background request can
therefore use the explicitly configured route without changing its message
capture implementation. This is protocol-level integration, not a claim that an
installed Hermes binary or copied Provider has been upgraded and tested.

The inbox scheduler starts new work with the existing default candidate budget
(12) and `allow_new_scopes=false`. A supplied exact new project scope remains an
authorized boundary and may be registered under G3g. This route does not enable
unrestricted scope discovery. Previously prepared work retains its original
candidate settings and boundary; an explicit incompatible scope is rejected.

## Bounded scheduling and stable identity

One call attempts at most four source turns. Selection uses the existing proven
source order; local indices are not rewritten and a higher local index cannot
hide a lower pending turn. A known incomplete earlier source blocks overtaking
inside that ordered window. Unknown/overlapping order retains existing conservative
behavior; unseen messages cannot be ordered. Later independent work can still be
attempted after an earlier terminal partial, subject to the runner's context guard.

The remaining backlog is returned rather than drained in an unbounded loop. A
subsequent legitimate process trigger can continue it. The host pending hint
identifies fresh work/zero-call saved-result recovery separately from a transport
retry that still needs `recover=True`. A job status poll never dispatches work.

The scheduler selects the exact persisted capture key. Redacted display turn IDs
are not rehashed as if they were the original host ID. Raw-ID and explicitly typed
captured-key addressing compare equal only for the same canonical source key,
with all scope/candidate/selection controls still exact. Stored arguments and
requests are never rewritten, and a 64-character raw ID is not automatically
interpreted as a captured key. This is addressing, not an authorization token.

Completed source decisions are not replanned. Retained terminal partial/failure
receipts remain visible without new requests. An old frozen legacy plan or old
partial is blocked for migration rather than imported as new evidence. An existing
manual incremental commit without a matching automatic run requires its explicit
resume path; this router does not invent an automatic authorization for it.

## Recovery and immutable acceptance

A normal new turn uses one model call. Top-level unusable response handling,
transport recovery and any explicitly requested partial replan share the same
existing maximum of two durable reservations for that work, not two per batch or
per enqueue. This integration does not change the single-turn recovery contract.

Saved responses and prepared commits resume without a model. A repeat automatic
trigger does not spend a remaining transport attempt. Parsed partial content is
not automatically repaired/replanned by polling or by setting `recover=True`;
use the existing bounded `recover_incremental_partial` contract when applicable.
Old successful NO_MEMORY and NO_CHANGE decisions do not change on repetition.

After an outer runtime exception, the wrapper reads that run's exact durable
commit receipt to retain **proven** applied IDs and operation outcomes. It does
not search similar text to guess which write succeeded. A prepared operation with
an uncertain filesystem result is resolved only by the existing commit recovery.
A later invocation does not count an already acknowledged operation as a new write.

A derived index or cleanup failure keeps the successful business result and a
visible recovery status; it must not be reported as a successful complete job just
because source coverage happened to be complete. MemoryWriter/CAS, source/native
revision guards, scoped registration, Forget and operation identity stay shared.
No network wait holds the Vault file lock; the existing durable owner fences
concurrent planners and stale responses.

## Queue binding and cutover

An accepted job binds effective route, configured default, normalized scope and
recover flag. A same-session request with different controls cannot silently
coalesce into it. Legacy queue records missing these fields keep legacy semantics.
Malformed stored controls fail validation without resetting the queue.

A waiting job refuses execution when the configured default has changed since its
acceptance. A synchronous batch checks for config changes between source attempts;
already accepted/applied work is not rolled back. This does **not** guarantee
instant cancellation when a file is edited during a model call, does not detect
arbitrary ABA config edits, and cannot stop binaries that do not implement these
checks. Scope/source changes retain the stronger per-operation existing guards.

Production cutover still requires the approved cold procedure: pause supported
write triggers; finish or explicitly isolate old plans; stop known workers and old
MCP/Provider processes; take a consistent backup respecting Forget; upgrade copies
together; verify installed capabilities and rerun acceptance; only then select
the route under separate authorization. Reading `process_status` is not a
migration permit. Do not erase old plans or budget records to make a switch pass.

## Result and status contract

The process result names `pipeline`, `execution_status` and `coverage_status`.
`processed_turns` counts fully completed turns in this invocation; `attempted_turns`
counts attempted source work including zero-call recovery. `memories_written`
counts newly acknowledged applied operations, not native NO_CHANGE targets.
`committed_operation_ids` provides bounded deduplication in job attempt aggregation.
A hard crash before a receipt returns leaves acknowledgement uncertain; it does
not justify claiming an exact lifetime write counter.

`pending_inbox_turns`, `deferred_inbox_turns`, unresolved counts and result rows are
current observations. Job aggregation uses the last backlog gauges while retaining
applied operation identities and actual invocation model-call counters. Successful
writes from an earlier attempt are not erased by a later refused attempt. Rows
contain IDs, safe reason codes and bounded counters, not memory bodies, raw model
responses or credentials. Output rows are capped at 100 with an explicit truncation
flag; this is not a claim of a complete dump of an arbitrarily large queue.

The existing `process_status` without job_id now also reports configured route,
retained incremental run statuses, pending commits and live owner state. It makes
no model calls or writes, and explicitly returns `switch_permission=false`.
Retained failed/partial receipts are not a current semantic correctness score or a
proof of all uncaptured messages. Existing local search/read remain usable with
model configuration absent. Queue errors do not become successful NO_MEMORY.

## Validation and outstanding work

Public tests cover legacy defaults, route overrides, exact scope, source ordering,
batch caps, redacted ID selection, CLI/MCP/queue controls, captured-route conflicts,
old queue compatibility, safe summaries, native NO_CHANGE, index/cleanup failures,
source/target edits, Forget, concurrent dispatch, and a real child process exiting
after its saved response. All Vaults and model responses in tests are synthetic.

Not completed here: default production activation, the legacy text remember
selection bridge, universal lifecycle/receipt GC, migration tooling and mixed-old-
binary prevention, true live Flash semantic/latency/holdout evaluation, native
Windows/macOS/Hermes acceptance, or all G4 public retrieval completeness work.
The fixed 1,861-codepoint/3,587-byte main prompt is unchanged. This batch modifies
execution coordination, not business-specific semantic rules.
