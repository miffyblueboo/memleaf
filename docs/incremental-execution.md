# Incremental model runner: staged G3c

This is an **opt-in Python execution path**, built on the G3a compiler and G3b
shared Writer/recovery bridge. It does not switch `process()`, `remember()`,
Hermes lifecycle calls or MCP tools to the incremental planner. Package version,
release automation and production model configuration remain unchanged.

## Public API and deliberate scope

```python
# These messages must already have been captured into this authorized Vault.
result = service.run_incremental(
    source="hermes", session_id="session", turn_id="turn",
    backend=single_dispatch_backend, scope="project:Atlas",
    priority_memory_ids=["mem-existing"],
)
# Save result["run_id"]. Filesystem recovery needs no model backend when the
# response or G3b write plan was already persisted.
result = service.resume_incremental_run(result["run_id"])
# A transient provider failure may use only the remaining original allowance:
result = service.resume_incremental_run(result["run_id"], backend=single_dispatch_backend)
```

`backend` implements the existing `complete(prompt, system=..., purpose=...)`
interface and must declare `single_pass_safe=True`. Built-in compatible HTTP
backends perform one fixed POST per completion; no CLI/host fallback or hidden
review is enabled. Custom backend declarations are a **trusted extension
contract**, not proof that arbitrary user code cannot send extra requests.
The supplied backend's model, thinking controls, timeout and output limits are
retained; this API does not discover credentials or choose a stronger model.
Tests replace the HTTP boundary rather than consuming real model quota.

This increment processes **one complete captured turn with automatic retention
semantics**. It is not an implementation of the new explicit-remember selection
protocol. Existing explicit remember is unaffected. `backend=None` can prepare a
bounded durable request, or finish an already received response without an LLM.
The normal entry needs its raw source; resume by run ID can settle an already
committed operation after raw-source cleanup.

## One shared source-work allowance, not a fresh budget per transport

The budget key is the existing `extraction_work_id(turn, automatic, automatic)`.
`run_id` is derived from that key and includes the immutable source revision set.
Scope and candidate arguments are bound to the receipt. Changing route, arguments,
process, prompt or transport does not create an independent allowance.

Normal success calls the backend once. The **total persisted ceiling is two**,
including previous known consumption for that work. An already-consumed legacy
three-request budget stays exhausted. Proven old job consumption is merged once
by the shared budget module; ambiguous migration blocks without dispatch.
Known runtime consumption also prevents a missing/regressed budget file from
being interpreted as a new budget. Deleting all control receipts is outside the
retained-state idempotency guarantee; Markdown alone cannot reconstruct no-memory
or unused authorization decisions.

| Failure or result | Behavior |
|---|---|
| Valid full response | Compile and apply once; successful replay uses no model |
| Invalid whole JSON / invalid top-level shape / empty or oversized response | At most one retry using the same frozen input; no previous assistant message |
| Timeout, rate limit, network failure or unusable provider envelope | Return retryable when allowance remains; retry only on an explicit next invocation |
| Authentication, unavailable route or unclassified backend failure | Terminal failure, no retry loop |
| Parsed local row error, missing evidence or DEFERRED | Commit independent valid decisions and retain unresolved state; do not repeat a full semantic pass |
| Sources, scope or target snapshot changed | Block uncommitted old result; do not manufacture replacement evidence |
| Knowledge/index/final receipt I/O failure | Resume stored response or frozen G3b operations; no model regeneration |
| Explicit Forget | Cancel affected request/response before deletion; a returning callback cannot write it back |

Selective structural `repair` and changed-context `replan` are **not enabled** in
this increment. `completed_with_unresolved` is not quality success, and a repeated
invocation does not re-extract its successful rows. It retains its original
remaining allowance for a later explicitly implemented recovery contract rather
than resetting counts. Native comparison is connected by G3d. Full G3 still requires selective recovery
and host/model acceptance. No additional reviewer call is hidden under "recovery".

## Durable phases and correlation

`incremental_runs` in the existing processed ledger stores a versioned,
checksummed receipt. It binds the request, source window, snapshot, arguments,
budget identity, and a stable G3b commit intent. It does not become the source of
truth for permanent memories.

```text
ready -> dispatching -> response_ready -> committing -> completed
                  \-> retryable                      \-> completed_with_unresolved
                   \-> failed / blocked / cancelled
```

The entire model response is saved **before** knowledge mutation. On restart a
saved response is consumed before any new request; an existing G3b commit receipt
is resumed before comparing the old snapshot with its own already-applied writes.
No regenerated memory IDs, operation IDs or NO_MEMORY decisions are needed.
Terminal runtime receipts omit request/response bodies. G3b keeps its existing
bounded unresolved operation/issue contract.

Budget reservation and runtime persistence are separate atomic files. A crash
between them can conservatively consume an unused slot; it must never grant an
extra slot. `reserved_requests` is not billing or a count of confirmed HTTP
requests. `model_calls_this_invocation` counts observed backend invocations in the
current caller, `responses_observed` records results, and uncertain/unattributed
reservations are reported separately. The system does not invent provider usage
for a dead process.

`budget_finalized=false` can coexist with committed memory when terminal budget
bookkeeping failed. Cached completed replay retries this local bookkeeping,
without calling the model or rewriting the memory. Commit success and control
cleanup success are not collapsed into one boolean.

## Concurrency, source retention and cancellation

A Vault-wide runtime owner fences other incremental runners and legacy
process/remember workers during dispatch. It has a PID and unique token;
OS file locks cover only local read/modify/write sections, **not network calls**.
Manual writes and Forget remain possible, so source/snapshot/revision checks and
the commit token guard still apply at the actual Writer boundary.

A dead PID can be recovered. A known live owner is not stolen based on elapsed
time alone; unknown liveness stays conservative. A process-local active-token
registry permits recovery after that same process's failed owner-release write,
but is not a replacement for the persistent owner. A live PID reused by another
process may require operator review; there is no distributed lease service.

Runtime-owned sources are excluded from the old pipeline and protected from
cleanup before the commit receipt exists. New source revisions can become new
work after previous runtime/commit owners are terminal; a pending old revision
must first be recovered/blocked. Existing legacy or manually staged decisions
cannot be silently fed into a second pipeline.

Forget cancels a runtime if an affected target or shared source is in its frozen
context. This is intentionally conservative: a mixed request can be cancelled
as a whole while independently committed sibling memories remain. Request and
response bodies are erased from the runtime receipt; ordinary history/source
removal still follows the existing Forget boundary, not a promise to erase
unmanaged backups or arbitrary backend logs.

## Capacity and fixed prompt

Conservative staged limits are 128 runtime receipts, 1 MiB per receipt and 16 MiB
for this receipt collection. A full ledger rejects new work rather than dropping
active or terminal replay protection. Retention/migration of terminal runtime
receipts remains a later explicit policy; this is not an unlimited inbox service.
The 128 KiB request cap reserves space for the short retry suffix. Existing
compiler evidence/target/response bounds still apply without truncating facts.

The normal fixed system prompt is unchanged from G3a: 1,861 Unicode code points,
3,587 UTF-8 bytes, LF including the final newline. Recovery adds only a short
same-input retry instruction. These are text sizes, not token or latency claims.

## Acceptance performed for this increment

The public suite retains all 242 earlier tests and adds runner tests for normal
CREATE/UPDATE/NO_CHANGE/NO_MEMORY, multi-turn same-ID completion, bounded retries,
partial isolation, restored responses, request budget migration/loss, captured
revision changes, corruption and bounded capacity. Recovery checks include ten
runtime-save fault subcases, real subprocess exits before/after response receipt,
thread/process ownership, index failure, and Forget in-flight/before commit.
The built-in HTTP adapter is tested with its POST replaced: payload settings and
one call are checked without real network traffic.

Passing deterministic tests is not live Flash semantic acceptance. Do not enable
this route as the default until selective unresolved recovery,
new-scope transactions, explicit remember integration and the agreed real
Hermes/Flash plus native Windows/macOS acceptance are complete.

Read-only native comparison and pending context retention are described in
[incremental-native-comparison.md](incremental-native-comparison.md).
