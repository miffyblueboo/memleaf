# General processing reliability contract — 0.2.26 candidate

This is source-neutral processing, not a mail extractor. Dialogue, documents,
calendars, issue trackers and terminal/tool observations use the same admission
and commit boundaries. No tool/topic keyword grants write permission.

## Evidence and semantic decisions

The host supplies physical user events or actual matched current-turn tool
call/results. Assistant synthesis, a caller-supplied digest, and read-back of
memleaf knowledge cannot independently authorize new writes. Standard memleaf
resource names and explicit paths into the current Vault are treated as
read-back, including aliased filesystem tools. Undeclared/relative resources
cannot always be identified; adapters must supply meaningful resource identity.

Evidence inventory IDs depend on captured event/source locations, not candidate
numbering or unrelated inventory order. User origin/syntax labels are hints,
not the final semantic classification. One example request does not mark an
independent real assertion as hypothetical. Actual pasted documents/code may
be evidence; examples, suggestions and hypothetical content are not new facts.

The Gate still decides future value, entailment, semantic role, ownership and
CREATE/UPDATE/NO_CHANGE. Each writable candidate quotes actual evidence units.
Core validates the unit/event identity, physical source, exact text and bounds.
Start/end are relative to unit.text; they may both be omitted for a unique exact
quotation, in which case Core locates it without model character counting.
Malformed/ambiguous references fail the contract. Matching a quote proves
provenance, not semantic truth. This mechanism is not a universal NLP proof.

Legacy candidate-only output has no n-gram or short-text authorization bypass.
It must repeat a complete non-query source statement, or produce an explicit
validated quotation via the bounded correction path. Automatic summarization
sees only admitted spans, candidate semantics and the bounded existing-target
context. Its source references must stay within admitted event keys. Final
scope/type/target/date checks remain active.

## Coverage, limits and no-op behavior

A Gate may map multiple facts in one evidence unit to several candidates, or
one candidate to several units. Coverage is checked against real supplied IDs.
One source-neutral correction can classify missing units; its candidates pass
the same validator/deduplication path, never a post-Gate business-pattern writer.
Already valid siblings survive a malformed correction. Work that remains
ambiguous is retained and explicitly reported, not guessed into a global scope.

Evidence decisions, candidate dispositions and filesystem operations are
separate ledgers. `execution_status=ok` is not complete extraction:
`coverage_status=partial` plus `unresolved_evidence_count` describes remaining
work. Known incomplete/missing observations remain unresolved even when a model
labels them NO_CHANGE. Incomplete turns retain their source instead of being
cleaned after the usual grace period. Scope-filtered retries may revisit them;
no endless automatic model retry or extra external tool call is introduced.

Tool evidence is bounded to eight records with at most 2,000 content characters
per captured result record and 320-character metadata fields, with redaction at
Core capture. Large unambiguous top-level record collections retain complete
records and enclosing context within that budget. Per-record provenance takes
precedence over common source metadata. An overflow slot reports omitted
records. Arbitrary large prose is not split into falsely complete facts;
unsupported/incomplete content needs a supported complete source excerpt or a
later source input. Execution outcome and completeness are distinct.

Codex pending tool data retains sixteen turns; bounded tombstones make evicted
uncaptured evidence visible as incomplete. Older loss beyond 256 tombstones
leaves a conservative session diagnostic. In that extreme state missing
observations may stay partial until source evidence is supplied again. No old
observation is attached as a new fact to a different turn.

Automatic NO_CHANGE does not modify permanent Markdown, sources or history.
Processing watermarks/diagnostics may still advance: these are not permanent
memory ownership. Existing native-memory coexistence, global shared Vault
visibility and Scope Map/search/read contracts remain unchanged.

## Commit and recovery

Final requests, source identifiers, candidate/evidence decisions and scope
operations are frozen to a checksum-protected local plan before mutation.
Limits are 8 MiB per plan and 16 MiB for the pending plan inventory. No raw tool
transcript is duplicated into the plan. Plans contain the final memory payload,
which remains private Vault data. A checksum detects corruption, not a malicious
local filesystem owner.

Under the existing Vault lock, current target revisions are checked before
commit. A stale update cannot overwrite another agent's change. Exact
commit-time duplicates include title and complete state-bearing content, so two
independent titled tasks are not merged solely because their bodies match.
Model-assisted same-future-use matching still uses bounded existing candidates;
it is not replaced with fuzzy string authorization or embeddings.

A retry resumes a matching persisted plan without asking the model for a new
summary. CREATE/UPDATE outcomes survive interrupted final-ledger writes.
Explicit cross-project correction and retirement preserve their original
history identity and do not resurrect the wrong target on replay. Ordinary
cross-project target selection does not inherit this special authorization.
Conflicting same-turn writes to one target are deferred/rejected rather than
silently applied in sequence. This remains forward recovery, not a database
transaction across the whole Vault filesystem.

## Read-only inspection

```bash
memleaf audit --vault /path/to/existing/vault --json
memleaf process --vault /path/to/existing/vault --source hermes --session-id SESSION --dry-run --json
```

Audit is local, creates no source lock/index and makes no model call. It reports
verifiable identical active payloads and invalid/pending accounting; it does not
infer which release produced old data or automatically delete/repair anything.

Dry-run executes the normal processor on a private temporary copy. It can call
the configured independent Model Route and read configured native sources, but
never writes them or the original Vault. It returns candidate decisions and
knowledge/history changes, not configuration credentials. The copy is deleted.
Snapshots are limited to 256 MiB/100,000 relevant files, reject symlinked children
and compare original content before/after. Concurrent source changes invalidate
the preview. There is deliberately no apply-preview command.

## Verification boundaries

Deterministic backends test schema and filesystem behavior, not hosted-model
accuracy. tests/semantic_fixtures.py adapts prescribed old Gate judgments to the
quote protocol for update/history/maintenance tests. It does not run in product
code and is not proof those judgments are semantically sound. New adversarial
protocol tests choose their quotations explicitly. No model score or absolute
"all languages/all scenarios" accuracy claim is made.

Before release, separately inspect live Model Route behavior on held-out real
shaped inputs, without writing the real Vault. Validate pure queries, mixed
assertions/questions/examples, negation, user confirmations, multilingual
paraphrases, multiple scopes and several external tool categories. Record actual
model/provider, counts of false writes/omissions/deferrals, and failures without
including credentials or private transcripts. Unit-test success cannot replace
that acceptance.
