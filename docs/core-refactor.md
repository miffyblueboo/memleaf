# Shared memory core refactor — first verified increment

Baseline: `bc7622e0f4c5c7b6658336c4deb71fce6a370f11` (unreleased 0.2.26 candidate).
This is a development branch, not a new stable release. The maintainer will run
live-model acceptance locally; fixture tests are not described as model quality.

## Product contract retained

- Markdown in `knowledge/` and `history/` remains the permanent source of truth.
  Existing Vault paths, frontmatter, memory IDs, history and tool names remain compatible.
- No database, vector service, queue server, daemon, telemetry or runtime dependency
  is introduced. The existing host-managed stdio MCP subprocess remains supported.
- Hermes still registers `MemleafMemoryProvider` through the official plugin API.
  Hermes native MEMORY.md / USER.md remain read-only cooperating sources.
- All hosts use one Core and the selected shared Vault. Source/session identifiers
  track provenance and lifecycle; they do not restrict permanent visibility.
- Scope Map -> search -> selected read, retrieval gates, budget, global todo paging,
  installation and model/provider configuration are unchanged.

## Real ownership boundaries

`Processor` is now an orchestrator with explicit composition, not a mixin,
attribute proxy or a second parallel pipeline:

| Component | Owns |
| --- | --- |
| `ModelExecutor` | Existing Model Route, bounded correction, diagnostics |
| `PlanningContext` | Related-memory lookup and candidate/target context |
| `MemoryPlanner` | Model outputs -> proposed changes; no Markdown commit |
| `MemoryCommitter` | Automatic and explicit commit validation, journal handoff; forget mutation |
| `ProcessJournal` | Claims, snapshots, cleanup, pending plans and retry progress |
| `TurnAudit` | Per-run candidate/evidence outcomes and uncommitted overlay |
| `recording_policy` | Explicit pre-capture permission controls, not memory extraction |

The first structural commit deliberately preserves old semantics. Subsequent
contract fixes are separate; moving code is not presented as completing semantic
simplification. The large legacy planner and semantic helpers remain work items.

## Behavior fixes in this increment

1. Capture preserves process-owned root journal fields instead of overwriting
   `pending_turn_plans` and `pending_operations` when another message arrives.
2. Forget preflights targets, cancels matching frozen operations before deleting
   files, and preserves unrelated requests in the same frozen plan. A stale lease
   cannot commit the cancelled plan. A new explicitly authorized remember event
   may store the fact again; there is no permanent topic blacklist.
3. Explicit remember still skips the value Gate, but its explicitly selected Scope
   must survive summary validation. UPDATE carries the target's pre-model revision
   through the same optimistic validation as automatic UPDATE.
4. A successfully persisted assistant event consumes only tool cache records proven
   present in the inbox. Failed/late capture does not discard unsaved evidence.
   Session-level historical loss no longer taints unrelated future turns.
5. Direct leading recording controls and `record=False` prevent turn contents from
   entering the inbox and host tool cache. Suppression is explicit, not a fake
   stored result. Session off/resume state survives rebuild/restart; late private
   callbacks remain private. Examples/quoted commands do not toggle the policy.
6. `coverage_unresolved` receives at most one subsequent natural retry. At most four
   such prior turns are selected per process call, alongside new complete turns.
   Other ambiguity/missing-source reasons are retained, not blindly retried. The
   host keeps a pending trigger only while automatic retry work remains.

Recording controls intentionally recognize a small explicit command vocabulary
(e.g. `这段不要记录`, `接下来不要记录`, `恢复记录`, `Don't record this`,
`stop recording this session`). This is not a promise to classify arbitrary
natural-language privacy instructions; integrations can use `record=False`.
Policy stores only permission booleans and hashed identifiers, not private text.
Late-callback protection keeps identifiers for private turns; it is not a cache
of private conversation content and is separate from permanent-memory Scope.

## What this increment does not claim

- The legacy post-Gate todo completion/rework recovery and planner heuristics have
  NOT all been removed. They must be replaced with bounded model-driven planning
  while preserving task behavior; no email-specific path is reintroduced.
- Compatible same-target semantic updates still need planning consolidation; the
  existing conservative conflict result is retained for now.
- Existing tool-evidence retention/configuration semantics still require their
  own compatibility review. This increment repairs consumption/permissions, not
  a silent redefinition of include_tool_output/include_attachments.
- Compaction and explicit low-level library writes retain their existing paths.
  This is not an assertion that all maintenance mutations are already centralized.
- Old abandoned host cache entries are not globally purged by this migration.
- There is no claim of multi-file database-style atomicity or perfect model judgment.

## Validation

The baseline was reconstructed with Git tree `f55989c9637c8b0345d01f6ebb09c548baec9dd0`.
Its 682-test suite passed locally. The behavior-preserving composition step passed
that same suite. New user-result contracts first reproduced the relevant failures,
then the full suite reached 701 tests with zero failures/errors and 2 conditional
skips (Linux/Python 3.13). See the exact branch commit's CI for native platforms.

No old test methods were deleted. Private helper mocks were moved to their new
owners. One explicit-remember Scope fixture now supplies a corrected second model
response and additionally checks stored Scope; its original assertions remain.
Tests must continue using temporary Vaults and no live credentials.

## Phase 2 — model-owned state decisions and grouped updates

This increment builds on `33dc0edaa99351237ca3e5ac1b36abb140735c83`.
It removes the post-Gate completion/rework candidate factories and the summary
state injection that could override a model's NO_CHANGE. A missing required todo
UPDATE status is now rejected and corrected through the bounded model path, not
silently supplied by a task-specific rule. A complete Gate NO_CHANGE remains a
valid no-write; correct recognition of new facts remains a model-quality concern.

`UpdateCoordinator` coordinates only an exceptional group of multiple admitted
updates to the same current memory. Single updates incur no group call. Group
reconciliation uses the existing ModelExecutor/Model Route, the original target
revision and exact admitted source spans. The model returns one compatible UPDATE,
a genuine NO_CHANGE, or a DEFERRED group. Deterministic validation checks all
members, target, Scope, type and evidence; proposals do not become new evidence.
Scope-retirement/native-shadowing authorizations are not implicitly combined.
Oversized or unresolved groups are deferred whole; unrelated target groups can
still commit. This does not add a new data service or a second persistence path.

A compatible group freezes one write with all contributing candidate receipts.
It produces one current-memory update and one historical version. Replay records
the original UPDATE for every member, using the same operation ID without another
model call. Explicit forget cancels all contributing members and their frozen
payload, preserving independent sibling requests. A later explicit remember is
new authorization, not blocked by an old cancellation.

Validation on the complete phase-2 local tree: 717 tests, zero failures/errors,
2 conditional skips (Linux, Python 3.13). The suite grew from 701 to 717 tests. Sixteen new tests were added; four existing
shared-target tests were renamed and adapted to the authorized group contract.
They retain no-partial-write, retained-inbox and single-history coverage, and add
per-candidate accounting. Group conflicts now advance the normal turn watermark
with explicit retained DEFERRED records instead of failing the entire Gate. The strict public
Gate parser still rejects repeated targets by default; only the planner opts in
to grouped updates. Final-commit CI remains the source for native platform results.

Still separate work: tool-output/attachment retention configuration alignment,
low-frequency maintenance/low-level library mutation consolidation, and the
remaining legacy Scope/plan heuristics. This phase does not claim those are
finished, or that deterministic fixtures substitute for local live-model testing.

## Phase 3 — shared evidence-retention policy

Based on phase-2 commit `3ac8a29394f358b55527d8874b64c55c93d4359f`.
This increment aligns tool-output/attachment policy across direct capture,
HostRuntime pending cache, the copied Hermes provider and new planning calls.
New Vaults write an explicit bounded mode; old false/absent flags remain metadata
only, with no silent opt-in. Document sources remain excluded unless authorized.
Metadata exclusions cannot create false missing-evidence retries or later regain
removed content. Configuration changes do not retroactively delete memories or
rewrite frozen operations. The full boundary is in `docs/evidence-retention.md`.

Phases 2 and 3 supersede the corresponding first-increment TODO entries above.
Remaining design work is the legacy Scope/plan heuristics and coordination of
low-frequency compaction and raw-library mutation paths. No release-complete
claim is made merely because the current regression suite passes.
