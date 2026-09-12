# Conversation-only memory processing

## Input boundary

Only visible user and assistant messages are memory sources. The assistant's
reported findings, project updates and explicit actions can be extracted as
stated. Tool results, mail bodies, attachments, terminal output, hidden reasoning
and system/developer instructions are excluded. This applies to direct capture,
host hooks, Hermes and new processing of legacy inbox records. Old capture
configuration cannot opt tool extraction back in.

Keep uncertainty, attribution, conditions, suggestions and questions intact.
An assistant recommendation is not a user decision; an offer to act is not a
completed action. Existing memory can be read for comparison and deduplication,
but a restatement alone is NO_CHANGE. Extraction does not independently verify
the assistant's report against raw external sources.

## Admission and coverage

User clauses retain stable event/quote references. Each complete assistant reply
is one source unit, preserving headings and topic context instead of expanding
one summary into dozens of sequential Gate batches. A unit can support several
candidates using distinct exact quotes. Gate still classifies future reuse and
CREATE/UPDATE/NO_CHANGE, while Core checks source references, Scope, target type,
dates and revisions. Reference validation is not proof of semantic correctness.

Only the visible conversation requires coverage. Ignored tool errors, truncated
outputs and old tool bodies do not create unresolved evidence. The compatibility
status `external_evidence_status` is `disabled`. Ambiguous conversation content
can still be deferred; no missing date, owner or project is invented.

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

When candidate-specific retrieval discovers an active local memory missing
from the initial Gate context, a bounded target reconciliation stage compares
the validated proposal and its evidence against the current records. The model
chooses CREATE, UPDATE, NO_CHANGE or DEFERRED; an UPDATE must explicitly retain
the target's type. Insufficient or oversized context defers the proposal.
This contract applies only to evidence admitted from the visible conversation.

Final automatic CREATE and UPDATE proposals receive a separate semantic review
after consolidation. For updates, the reviewer compares the selected current target,
admitted source spans and proposed replacement, retaining still-valid old
information unless current evidence supersedes it. It can accept, revise,
return NO_CHANGE or defer. Revisions must pass the same source, date, type,
Scope and target checks; review failure preserves the original memory. This
adds a bounded model stage, not a local text-concatenation or keyword rule.
The reviewer may use the bound span's full current visible message to resolve
negation and references without treating unbound text as new fact authority.
Validated scope operations and native shadow metadata do not bypass review of
the memory content; review cannot add or change those operations. Explicit
remember and deterministic scope correction retain their separate paths.
Automatic duplicate observations remain NO_CHANGE ledger entries and do not
enter the mutation batch as empty metadata operations.

Partial semantic retries submit only unresolved evidence units. Settled outcomes
are retained in the ledger; an identical external observation in a later turn
is recognized by its source identity and exact content, without interpreting
business keywords. New conversation assertions keep their distinct turn identity.
When all newly pending turns in a session are read-only, automatic processing
does not bundle retries of older deferred turns into that query. Those deferred
records and their retry allowance remain available; an explicit scope retry
keeps its existing behavior. Classification uses the current capture policy,
including when an older inbox record still contains an excluded body.

A retry resumes a matching persisted plan without asking the model for a new
summary. CREATE/UPDATE outcomes survive interrupted final-ledger writes.
Explicit cross-project correction and retirement preserve their original
history identity and do not resurrect the wrong target on replay. Ordinary
cross-project target selection does not inherit this special authorization.
Conflicting same-turn writes to one target are deferred/rejected rather than
silently applied in sequence. This remains forward recovery, not a database
transaction across the whole Vault filesystem.

## Background processing

Automatic host capture submits `process` with `background: true`. The MCP call
persists a bounded local job and returns an opaque `job_id` with
`completed: false`; it does not claim that a memory write has completed. A
detached local worker later invokes the same processor and records
`succeeded`, `deferred`, or `failed` plus bounded counts and memory IDs. A
read-only `process_status` call reports that record. Repeated submissions for
one source/session share the active job and request one follow-up run, so a
new visible turn is not processed concurrently. Jobs remain in Vault runtime
state and a dead worker is requeued on the next enqueue or status read.

The explicit synchronous `process` request remains available by omitting
`background` or setting it to `false`.

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

The regression suite and acceptance helpers referenced here are maintained
locally and are not included in this repository or its source distribution.
Remote CI validates package construction and installed command entry points only.

Deterministic backends test schema and filesystem behavior, not hosted-model
accuracy. The local-only `tests/semantic_fixtures.py` adapts prescribed old Gate
judgments to the quote protocol for update/history/maintenance tests. It does
not run in product code and is not proof those judgments are semantically sound.
New adversarial protocol tests choose their quotations explicitly. No model score or absolute
"all languages/all scenarios" accuracy claim is made.

Before release, separately inspect live Model Route behavior on held-out real
shaped inputs, without writing the real Vault. Validate pure queries, mixed
assertions/questions/examples, negation, user confirmations, multilingual
paraphrases, multiple scopes and several external tool categories. Record actual
model/provider, counts of false writes/omissions/deferrals, and failures without
including credentials or private transcripts. Unit-test success cannot replace
that acceptance.

## Native Windows verification boundaries

Windows processing-owner liveness is queried through a process handle with
SYNCHRONIZE access, never by sending a signal. Shared-Vault writes use a
native byte-range lock between processes, not only a Python thread mutex.
The local-only Python suite is intended to run on all three OS families; remote
CI does not exercise it. Native child-process tests cover Linux, macOS and
Windows when run locally.

Only the `install.sh` shell-harness class is POSIX-only; local Windows checks
cover installation, upgrade, PowerShell syntax, host lifecycle and native Codex
acceptance. Test launchers use native `.cmd` wrappers on Windows.
Byte-preservation assertions compare actual before/after bytes, including CRLF.
POSIX mode-bit checks are conditional: Windows file privacy follows the Vault
directory's inherited ACLs, and Unix `0600` must not be interpreted as proof of a
Windows owner-only DACL.

Real-model acceptance requires an explicitly configured model route using
`MEMLEAF_LIVE_MODEL_TOKEN`, `MEMLEAF_LIVE_BASE_URL`, and `MEMLEAF_LIVE_MODEL`.
Live acceptance tooling is maintained locally and is not run by remote CI.
Missing configuration is a blocked acceptance result, not a passing semantic
test. Never publish based only on deterministic mocks.

## Capture policy

New vaults use `tool_evidence_mode: off` and `include_attachments: false`.
Legacy values are accepted on read, but effective capture always drops tool
payloads. Status reports the effective policy. Existing committed memories are
not deleted. New frozen plans carry `conversation_only_v1`; older plans without
that marker fail closed instead of replaying source decisions from the former
external-evidence policy. The journal remains available for explicit recovery.
