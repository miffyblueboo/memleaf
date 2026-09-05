# Shared memory core — controlled refactor

Baseline: `bc7622e0f4c5c7b6658336c4deb71fce6a370f11` (the unreleased 0.2.26
candidate). This document describes the completed four-increment design, not
an assertion that arbitrary model outputs are correct. The maintainer chose to
perform live-model acceptance locally; scripted fixtures are contract tests.

## Product boundaries

Markdown in `knowledge/` and `history/` remains the permanent source of truth.
Existing Vaults, paths, frontmatter, memory IDs and public tools remain compatible.
No database, vector engine, model weights, queue service, daemon, telemetry or
third-party runtime dependency is introduced. The existing host-managed stdio
MCP subprocess remains supported; no separate service needs deployment.

Hermes still registers `MemleafMemoryProvider` with the official plugin API.
Native MEMORY.md / USER.md stay read-only cooperating sources. Hermes and Codex
use the same Core and selected Vault. Source/session identifiers describe
provenance and lifecycle, never permanent-memory visibility. Scope Map -> search
-> selected read, retrieval enforcement levels, budget, global todo paging,
installation and the independent Model Route remain intact.

## Ownership

| Component | Responsibility |
| --- | --- |
| Processor | Orchestrate process/remember, compose components explicitly |
| ModelExecutor | Existing Model Route, bounded correction, diagnostics |
| PlanningContext | Read related memories, Scope evidence and target context |
| MemoryPlanner | Validate model proposals and construct change requests |
| UpdateCoordinator | One bounded reconciliation for an exceptional same-target group |
| MemoryCommitter | Validate revisions, freeze/execute writes, coordinate forget cancellation |
| ProcessJournal | Claims, progress, temporary evidence cleanup and retained retry state |
| TurnAudit | Per-candidate/evidence outcomes and uncommitted lookup overlay |
| recording_policy | Pre-capture permission controls, no business extraction |
| evidence_policy | One retention rule for capture, host caches and new planning |

There is one automatic pipeline, not parallel old/new writers, mixin inheritance
or a dynamic attribute proxy. `Memleaf._mutation_boundary()` is the common Vault
lock plus interrupted-compaction recovery boundary for automatic/explicit
commits, raw-library writes, forget and compaction snapshot/commit. Each operation
retains its own authorization, revision checks and journal. Models execute outside
this critical section. Pending automatic writes are NOT replayed by entering the
boundary: forget must be able to cancel them first.

## Semantic ownership

The model selects type, Scope, target and content. Deterministic code validates
identity, allowed source spans, schema, explicit constraints and local conflicts.
It can reject a proposal, but must not silently change its business meaning.

Removed paths include completion/rework post-Gate candidate factories, forced
summary status/timestamp insertion, keyword-based fact/project/todo conversion,
last-retry Scope reassignment, local splitting of rejected candidates, formal-plan
preference that changes type, and old/new body concatenation. A persistent wrong
UPDATE target is DEFERRED, not converted into CREATE. Valid sibling candidates
continue. The model may correct its proposal during bounded retries.

`NO_CHANGE` remains a valid no-write decision. Missing required todo-update state
is returned for model correction, not guessed locally. A model-declared ambiguous
target can be represented as coverage DEFERRED / target_ambiguous. An exact title
lookup with more than one target cannot select one. Source-local rejection and
retrieval heuristics remain conservative checks, not a guarantee of semantic
entailment. Only exact full-statement matching is used for legacy unbound outputs.

For an UPDATE, the model must return the complete intended current state, retaining
still-valid facts and omitting retired facts. Code no longer appends old paragraphs
to an incomplete summary. The prior complete version is preserved in history.
Real-model quality still needs local evaluation, including preservation of old
facts and recognition of new assertions mixed with questions.

Multiple admitted updates to one target are reconciled using the existing Model
Route and original target revision. One compatible summary produces one write and
one history record with all contributing receipts. True conflicts defer the group;
unrelated targets proceed. Single updates incur no group call. Oversized groups
are retained, not split into competing writes. Scope-retirement/native-shadowing
authorizations are not silently combined.

## Lifecycle and privacy

Capture preserves process-owned root journal fields. Frozen requests keep complete
payloads and checksums; recovery uses the saved payload without another model call.
Revision checks under lock refuse stale overwrites. Replay preserves the actual
CREATE/UPDATE outcome for each contributing candidate. This is forward recovery,
not a database-style atomic transaction across every file in a Vault.

Explicit forget cancels matching frozen operations before deleting files. Unrelated
siblings survive, stale cancelled owners cannot commit, and a new explicitly
authorized remember event can store the fact again. No permanent topic blacklist
is added. Explicit remember skips value classification, not its supplied Scope or
target-revision constraints.

Successfully captured tool cache records are consumed only after their presence in
inbox is confirmed. Failed capture retains evidence; historical loss cannot taint
unrelated future turns. Retention modes and old-config compatibility are specified
in `evidence-retention.md`. Historical abandoned cache entries are not globally
purged as part of upgrade; no destructive migration is performed.

Leading recording controls and `record=False` suppress user/assistant/tool content
before persistence. Session off/resume state survives rebuild/restart, and late
callbacks for private turns remain private. Examples and quoted controls cannot
change permission. The command vocabulary is intentionally bounded (for example
`这段不要记录`, `接下来不要记录`, `恢复记录`, `Don't record this`, `stop recording this session`);
this is not arbitrary-language privacy inference. Integrations may use record=False.
Permission state stores only booleans and hashed identifiers, never private text.

Coverage-unresolved evidence receives at most one subsequent natural retry, capped
to four old turns per process call alongside new complete work. Missing-source,
Scope/target and ownership ambiguities wait for relevant new information or explicit
retry instead of spending model budget forever. Exhaustion retains unresolved input.

## Validation and test-contract migration

The existing suite is retained. Renamed tests distinguish a MODEL correcting its
Scope/type/target from the removed local repair. Public outcomes remain checked:
stable ID, complete new content, one prior history version, no wrong-Scope write,
retained inbox and independent sibling progress. Prior tests that expected invalid
proposals to be rewritten now assert bounded model correction or explicit DEFERRED.

A test that required creation of a fact absent from its input and attached to an
unrelated target now requires zero writes; the valid duplicate sibling remains
unchanged. Ambiguity fixtures explicitly return evidence-level DEFERRED and assert
retention, rather than relying on plan-title keywords. Dedicated raw-response tests
cover malformed evidence and illegal targets without the fixture binding adapter.

Final-source testing must include the full Linux, Windows and macOS matrix, wheel
entry points and the matching sdist suite. Native CLI registration is not live
semantic acceptance. Test results are tied to exact commits/artifacts, not to a
mutable local worktree or a percentage-complete estimate.
