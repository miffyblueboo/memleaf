# Processing performance and semantic-quality acceptance

This document defines acceptance for automatic memory extraction without treating one validation layer as proof of another.

## Invariants

- Markdown under `knowledge/` remains the active-memory source of truth; `history/` remains historical state.
- Gate stays serial. Model work must not run while a Vault write lock is held.
- Candidate/review parallelism is bounded and opt-in through an explicitly parallel-safe backend. The default maximum is 3.
- Same-target updates are coordinated before independent final reviews. Revision checks, idempotency and atomic commit remain authoritative.
- Performance improvements must not remove Gate, summarization, reconciliation or semantic-review checks that are required for the same input.
- Prompt/review rules are source-neutral. No mail-, calendar-, customer-, product- or other application-specific extraction rule is used.

## Safe model telemetry

`process()` returns `model_metrics`, and detached process-job results retain the same bounded projection. Metrics contain only structural numbers:

- call count, retry count and failed-call count;
- cumulative model-request duration and stage wall-clock duration;
- input/output character and UTF-8 byte counts;
- maximum in-flight model calls;
- stage buckets for Gate, summarize, semantic review, coordination and target reconciliation.

Prompts, responses, memory bodies, source text, credentials and provider secrets are not retained by these metrics. Existing optional diagnostic logging keeps its separate bounded structural contract.

## Semantic acceptance contract

A memory must remain understandable without reopening its source. For one admitted future-use topic, the summary/reviewer must preserve source-supported information that defines the item's meaning: the explicit subject or named entity, object/deliverable, concrete action or state, necessary business/workstream context, and any number or code together with its source-stated meaning when that meaning is required for interpretation.

Minimality does not permit semantic generalization. A concrete requirement cannot become only “related matters” or an umbrella coordination item. Scope metadata is not a substitute for a named subject in the title/body when that subject distinguishes the item. A customer/project that owns an item and a broader product/platform in which it is implemented are separate relationships; one must not silently replace the other. Existing memories are comparison context and cannot supply a new ownership/project relationship. If attribution, a number's role, ownership, deadline or status is not established, preserve uncertainty or defer rather than guess.

Independent deliverables that can be executed, tracked or closed independently remain independent memories. Same-turn aggregate reconciliation may merge only one future-use topic; otherwise the item remains deferred for reprocessing rather than being silently discarded as no change.

## Acceptance layers

### 1. Deterministic unit/regression tests

Required checks include:

- telemetry contains only allowlisted structural fields and counts retries correctly;
- unsafe Host/callable routes remain serial; a parallel-safe API route respects `process.model_concurrency`;
- final review concurrency preserves request/result ordering and does not mutate audit/Vault state from worker threads;
- semantic-review contracts require both non-invention and preservation of meaning-defining facts;
- existing negative-context, evidence-span, scope-operation/native-shadow, aggregate-splitting and Windows process-owner regressions continue to pass.

Passing this layer proves only the deterministic contracts under test.

### 2. Cross-platform CI and MCP/background-runtime verification

Run the repository's supported OS matrix and the process-job/MCP tests. Verify that detached worker status exposes the bounded metrics, reruns aggregate metrics safely, and no model call is made while the Vault write lock is held.

Passing this layer proves the tested runtime paths and platforms. It does not prove that a real model will always extract every business fact.

### 3. Authorized real-model replay in an isolated Vault

Use an explicitly authorized conversation and the same configured model/provider, but point the replay at an isolated temporary Vault. Record only the safe structural metrics plus the resulting test memories. Check at least:

- omissions of subject/customer, concrete requirement, business context, and numeric meaning;
- incorrect project/customer attribution or product-as-owner substitution;
- incorrect todo state, owner, date or completion inference;
- independent-item splitting and aggregate deferral/reprocessing;
- duplicate/update behavior under repeated identical input;
- sensitive-data boundaries and the configured external-provider authorization.

For a before/after performance comparison, use the same captured input and equivalent model configuration. Compare model call count, retries, stage request-duration totals, stage wall-clock duration, maximum concurrency and final-memory quality. Do not claim a latency regression or improvement from different source input.

### 4. Production conversation observation

After release, compare one or more normal authorized production sessions against the same semantic checklist. Production observations are separate evidence from an isolated replay because model/service latency and real conversation structure can differ.

## Delivery reporting

Every delivery report must identify each layer as **passed**, **failed**, or **not run**. Never promote “unit tests passed” or “CI passed” into a claim that real-model semantic acceptance passed. If the real conversation content is not authorized for a particular external model/service, do not send it merely to complete acceptance; report that layer as not run.
