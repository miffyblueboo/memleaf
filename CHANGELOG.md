# Changelog

All notable changes to memleaf are documented here.

## 0.2.42 — 2026-09-12

- Rework ordinary automatic extraction around the unified `b3-single-pass-v1` planner. The current visible user/assistant turn is the only new-fact evidence; existing local/native memories remain comparison context. One structured model stage decides CREATE / UPDATE / NO_CHANGE / DEFERRED, with deterministic Core validation and at most one bounded format/structure repair.
- Make the latency-critical `single_pass` stage non-thinking by default and use bounded structured output for supported OpenAI-compatible routes. Fixed safe API routes enforce a six-second primary request cap, an eight-second preparation-plus-model window, a ten-second total turn deadline, and rejection of late results before commit; no unmeasured P50/P95 claim is made.
- Persist background extraction request/time budgets by process-job and turn identity so worker restarts cannot regain consumed attempts or wall time. Damaged budget/job state, invalid ownership/order, and wall-clock rollback fail closed instead of reopening a fresh extraction budget.
- Commit automatic processing one complete turn at a time and re-read durable Markdown/journal state before planning the next turn. Frozen-turn recovery preserves already committed work without replaying model calls/history, while one process invocation drains at most four claimed turns and exposes the remaining backlog explicitly.
- Move compaction/maintenance out of the `process()` / `remember()` extraction critical path, keep explicit no-write instructions as deterministic zero-model-call settlements, and retain structural-only model telemetry without prompt/response bodies or credentials.
- Separate B3 protocol compatibility from strict transport deadline safety. Host/Python callbacks can use the same B3 semantic protocol without falsely claiming hard cancellation; mixed auto routes remain fail-closed, `single_pass` forbids hidden host-to-API fallback, and explicit `remember()` keeps its existing bounded validator retry semantics.
- Preserve the product architecture: Markdown under `knowledge/` remains the active source of truth, `history/` remains historical state, permanent memory stays globally shared across agents using the same Vault, and no database, Redis, vector service, resident daemon, or local-model dependency is introduced. The release candidate passed Linux Python 3.11/3.12/3.13, Windows Python 3.11/3.12/3.13, macOS Python 3.11/3.13, native Codex Windows/macOS, wheel/sdist, installed-entry-point, and full unittest CI; real-model quality/latency acceptance remains a separate user-run step.

## 0.2.41 — 2026-09-11

- Promote the B3 single-pass automatic memory planner for explicitly `single_pass_safe` API backends. Core prepares bounded local retrieval/context once, one semantic planner stage decides CREATE / UPDATE / NO_CHANGE / DEFERRED, deterministic validation remains authoritative, and model-output repair is bounded to at most one retry (`max_attempts=2`). Host/custom/host-backed providers keep the proven P3 fallback, while explicit `remember` retains its existing single-summary path.
- Make project Scope provenance Core-owned in the single-pass path. The model can no longer authorize `scope_source`; Core derives `user`, `session_context`, `insufficient_context`, or `model` from the selected scope set and applies deterministic grounding for model-derived project affiliation. Legacy model-provided `scope_source` is tolerated only for compatibility and ignored.
- Carry forward the P0/P2/P3 transport reductions used by the fallback path, including bounded batch semantic review and CREATE-summary batching, while preserving Gate admission, target reconciliation, history/audit/commit semantics, idempotency, provider-neutral `thinking=low`, and fail-closed handling for invalid or unresolved evidence.
- Remove committed long-run benchmark scripts/results/docs from `main` and split oversized admission, Hermes provider, and large regression-test modules by responsibility. This is repository-maintainability cleanup rather than a search-performance claim; regression coverage is retained rather than deleted.
- Preserve the product architecture: Markdown under `knowledge/` remains the active-memory source of truth, `history/` remains historical state, permanent memory stays globally shared across agents using the same Vault, provenance/session fields do not become visibility filters, and no database, Redis, vector service, daemon, background resident service, or local-model dependency is introduced.
- Validation for the integrated production tree passed the full Linux Python 3.11/3.12/3.13, Windows Python 3.11/3.12/3.13, macOS Python 3.11/3.13, wheel/sdist, installed-entry-point, Hermes/Codex host acceptance, and native Codex Windows/macOS matrices before release.

## 0.2.40 — 2026-09-09

- Slim the automatic extraction stage prompts without changing output schemas, parsers, evidence segmentation, model routing, the provider-neutral `thinking=low` policy, target/revision handling, idempotency, or commit semantics. Gate owns admission, atomic splitting, attribution and duplicate/update selection; Summary writes one already-admitted current-state memory; semantic review verifies fidelity; Core keeps deterministic validation and write safety.
- Remove repeated policy essays from Gate dynamic/evidence prompts, narrow coverage repair to unresolved evidence, remove Summary's repeated final evidence re-check and dynamic JSON example, and reduce CREATE/UPDATE semantic-review prompts to grounding, completeness, candidate-boundary and UPDATE target-preservation checks. Stable prompt data markers used by host callbacks remain compatible.
- On the same representative synthetic input, the Gate prompt input shrank from 27,478 to 8,157 characters (about 70.3%) and the Summary prompt input from 10,652 to 5,046 characters (about 52.6%); CREATE review system text shrank from 7,327 to 2,766 characters and UPDATE review from 6,539 to 3,004. These are static character-count measurements, not a claim of a specific reasoning-token or wall-clock reduction.
- Validation covers focused semantic regressions plus the full Linux Python 3.11/3.12/3.13, Windows Python 3.11/3.12/3.13, macOS Python 3.11/3.13, wheel/sdist, installed-entry-point and native Codex matrices. A same-input real DeepSeek Flash A/B was not run before release, so this release does not claim a measured replacement for the previously observed 159-second session.

## 0.2.39 — 2026-09-09

- Make `llm.thinking` a provider-neutral model policy instead of a DeepSeek-only request feature. Gate, summarize and compact continue to request `low` by default for every configured API model stage.
- Translate that policy through each supported protocol: OpenAI reasoning-capable Chat Completions use `reasoning_effort=low`; DeepSeek keeps its explicit thinking switch plus low effort; current Claude effort-capable Messages models use `output_config.effort=low` with adaptive thinking where the model generation requires it; Gemini 3+ uses the lowest supported thinking level (normally `low`, with documented `minimal` fallbacks where `low` is unavailable), while Gemini 2.5 maps low to the native 1,024-token thinking budget.
- Keep compatibility fail-safe for older or unknown models: memleaf does not send speculative reasoning fields that the model cannot accept. Per-call telemetry now distinguishes requested thinking mode from effective mode and the fixed provider control used, so unsupported/provider-default execution is visible instead of being mislabeled as low.
- Omit sampling temperature when an OpenAI reasoning request or current Claude effort request does not safely accept that parameter. Existing Markdown/Vault, extraction, review, retrieval and write semantics are unchanged.

## 0.2.38 — 2026-09-09

- Unify automatic project-Scope grounding: registered and newly named model-selected projects now use the same exact candidate-bound source check. Remove the later registered-name occurrence conflict scan that could misclassify an implementation platform/product mention as ownership and reject the correct new project.
- Strengthen final CREATE/UPDATE semantic review so a `project:<name>` Scope with `scope_source=model` is itself treated as an affiliation claim; product/platform/system/notification/implementation mentions cannot authorize project ownership, and an explicit contradictory owner defers instead of silently changing Scope.
- Add safe per-model-call telemetry with fixed operation classes (`gate_primary`, `gate_coverage_repair`, format repair, summarize/review/coordination variants), request duration, input/output size, provider token usage, DeepSeek cache-hit/miss tokens and reasoning-token counts when supplied. Prompt/response text and credentials are never persisted.
- Add explicit `llm.thinking` configuration for Gate/summarize/compact. The default is `low`, retaining reasoning at the lowest supported effort; users may select `disabled`, `default`, `high`, or `max` explicitly. DeepSeek OpenAI-format calls send the corresponding thinking controls.
- Reduce Gate input cost by removing a duplicated system-policy tail and replace coverage re-checks with a narrow unresolved-evidence protocol instead of rerunning the full Gate prompt. Deterministic validation does not claim a specific real-provider latency reduction.

## 0.2.37 — 2026-09-09

- Add a dedicated `memleaf-mcpw` GUI entry point for the Hermes public MCP on Windows. The GUI-subsystem launcher does not allocate a console window even when an older Hermes/MCP SDK starts it without `CREATE_NO_WINDOW`; it enters the same `memleaf.mcp_server:main` implementation and keeps the same stdio JSON-RPC protocol.
- Keep the Hermes MemoryProvider private MCP on `memleaf-mcp.exe` with the v0.2.36 `CREATE_NO_WINDOW` protection, so both Windows launch paths are covered without changing macOS/Linux behavior or the Markdown Vault architecture.
- Treat `memleaf-mcp.exe` and its sibling `memleaf-mcpw.exe` as the same installed memleaf runtime for preflight/runtime policy while still requiring the GUI launcher for the public Hermes MCP. Existing v0.2.36 direct entries in the same runtime migrate automatically; different-runtime and different-Vault protections remain fail closed.
- Add Windows acceptance that inspects the installed launcher PE Subsystem, starts `memleaf-mcpw.exe` with `creationflags=0`, performs real MCP initialize and tools/list over redirected stdio, confirms all 13 tools, and verifies clean EOF shutdown.

## 0.2.36 — 2026-09-08

- Prevent the Hermes MemoryProvider from opening a visible console/Windows Terminal window whenever it starts its private `memleaf-mcp` stdio child on Windows. Provider-owned MCP launches now use `subprocess.CREATE_NO_WINDOW`; stdin/stdout pipes, stderr suppression, timeout handling, process reuse and shutdown semantics are unchanged.
- Add deterministic regression coverage for the Windows creation flag, the non-Windows zero-flag path, and the exact `Popen` stdio contract.
- Correct the README release banner that was still displaying 0.2.34 after the 0.2.35 publication.

## 0.2.35 — 2026-09-08

- Add safe structural model-call telemetry across successful, deferred, and failed processing: call/retry/failure counts, model-request and stage wall-clock timing, input/output lengths, and maximum in-flight concurrency are retained without prompts, responses, memory bodies, credentials, provider secrets, URLs, or raw exception text.
- Keep Gate admission serial, then allow bounded candidate-summary and final semantic-review concurrency only on explicitly parallel-safe model transports. `process.model_concurrency` defaults to 3 and is bounded to 1..8; host callbacks and caller-owned HTTP openers remain serial. Same-target UPDATE work stays ordered, target reconciliation happens before parallel work, and model calls never run while a Vault write lock is held.
- Tighten semantic completeness so admitted memories preserve meaning-defining named subjects/entities, deliverables, concrete actions or states, required business/workstream context, and each number/code with its source-stated role. Final review now treats omission or over-generalization as a quality failure, preserves uncertainty, and must not invent owners, deadlines, statuses, completion meaning, or numeric roles.
- Separate customer/project ownership from product/platform/system implementation context. Current-turn evidence remains authoritative for new relationships; existing memories are comparison context only and cannot create a new ownership or project-affiliation fact. Independent work remains source-neutrally split, while unresolved aggregate candidates defer instead of being silently discarded.
- Validation for this release: deterministic regression, same-input synthetic serial-vs-parallel comparison, Linux Python 3.11/3.12/3.13, Windows Python 3.11/3.12/3.13, macOS Python 3.11/3.13, wheel/sdist, installed entry points, and native Codex Windows/macOS gates passed. One Windows Python 3.12 full-regression attempt had an isolated failure and its targeted rerun passed. Authorized real-model replay, production-conversation acceptance, and same-input real-provider latency comparison were not run, so this release does not claim a specific replacement for the previously observed 433-second session.

## 0.2.34 — 2026-09-08

- Restrict automatic memory extraction to the current turn's visible user input and final assistant reply. Raw tool output, attachments, web/file/terminal payloads and legacy tool-evidence bodies are not new source evidence; existing or retrieved memory remains comparison context rather than source authority. Explicit memory-write prohibitions still fail closed.
- Add persisted background processing jobs and the read-only `process_status` MCP tool. Accepted background work reports its job identity before completion, while succeeded, deferred and failed outcomes remain observable; failed or unresolved work stays retryable without advancing cleanup state.
- Tighten source-neutral Gate coverage, exact evidence bindings, assistant-report context, date grounding, target reconciliation, duplicate/no-op handling, same-target update coordination and semantic review before automatic CREATE/UPDATE writes. Preserve candidate-local deferral and idempotent partial retries so unresolved ownership, target, evidence or timing does not fabricate a write.
- Keep the copied Hermes provider aligned with Core on the conversation-only source boundary, background job polling, session aliasing and bounded failure/deferred diagnostics. Existing Markdown Vaults, shared Hermes/Codex retrieval and explicit remember/forget paths remain local and source-of-truth preserving.
- Validation for this release: deterministic regression suites ran 927 tests successfully with 2 skips, and isolated MCP transport checks passed. Semantic quality and processing latency remain model-dependent; no concurrency or performance improvement is claimed, and this release does not claim real-mail or customer-business acceptance.

## 0.2.33 — 2026-09-08

- Extend the Gate with bounded physical-evidence batches, exact unit/quote bindings, isolated cross-batch candidate IDs, same-target update coordination, and bounded model reconciliation for compatible CREATE proposals. Failed batches remain retryable and fail closed without advancing the watermark or cleanup.
- Align Core, HostRuntime, and the copied Hermes provider on document/attachment classification and effective capture-policy reporting: ordinary structural files follow the selected retention mode, while explicitly identified attachments still require attachment opt-in and bounded retention.
- Keep automatic UPDATEs as `NO_CHANGE` when current evidence only restates the target or adds provenance/source metadata; preserve the selected target for real semantic state changes. Add synthetic-document real-model lifecycle acceptance and regressions for coverage, deadlines, todo completion, capture policy, and lifecycle behavior.
- Harden automatic processing around source/date grounding, target reconciliation, update review, duplicate/no-op collision handling, and partial-retry idempotency so unresolved ownership, target, evidence, or timing stays deferred without fabricated writes.
- Isolate read-only/general queries from stale deferred automatic turns while preserving retries for assertions, explicit scopes, and external observations; extend Core/Hermes transport and evidence regressions for these boundaries.
- Validation for this release: the full suite ran 911 tests successfully with 2 skips. A four-phase synthetic-input real-model acceptance also passed; this does not claim real-mail or customer-business acceptance.

## 0.2.32 — 2026-09-07

- Add bounded `unknown_unit` Gate diagnostics that identify the exact response field and retain only allowlisted type/length/digest and expected-set summaries; raw invalid values, legal ID lists and model output remain excluded from normal logs and failed state.
- Give bounded correction attempts the failed constraint, field path and immutable legal-ID inventory used by validation, so coverage and evidence-binding references are repaired by regeneration rather than host-side ID substitution or relaxed evidence authority.
- Keep `invalid_evidence` compatibility, source-neutral physical evidence boundaries, and watermark/cleanup safety unchanged: a persistently failed Gate does not advance the watermark or trigger cleanup.

## 0.2.31 — 2026-09-07

- Unify Core and the copied Hermes provider on one idempotent UTF-8 evidence budget: at most 64 source records, 32 KiB per body, and 128 KiB of aggregate body text. Complete matched sources above the former 2,000-character/eight-record limits remain available within those bounds.
- Keep loss diagnostics bounded to 64 marker identities plus one aggregate marker. Per-record, aggregate, and marker overflow remains explicit incomplete evidence and cannot authorize a write or successful cleanup.
- Preserve metadata/off, attachment, redaction, and call-ID behavior, and consume metadata-mode pending evidence after a successful capture using the same effective policy on pending and inbox sides.
- Add provider-copy, capture-to-process, loss-defer, marker-capacity and lifecycle regressions. Deterministic tests do not claim real-model semantic quality, real-session replay, or customer acceptance.

## 0.2.30 — 2026-09-07

- Add a physical evidence projection for the Gate: the complete local inventory remains available for provenance, replay and audit, while only user-origin units and complete external observations become bindable model evidence. Assistant synthesis, retrieved memory, incomplete observations and metadata-only records remain context or unresolved ledger state.
- Require one unified Gate response with `candidates`, `coverage` and `evidence_bindings`, including explicit per-unit coverage and bounded correction for missing physical units. Preserve legacy candidate-only compatibility when exact or validated bound support already accounts for a unit.
- Preserve the public `invalid_evidence` failure category while adding safe, allowlisted `evidence_check` diagnostics for distinguishable coverage, binding and span failures. Diagnostics do not retain raw model output or error text.
- Add regression coverage and documentation for the evidence boundary, metadata-only capture behavior and cleanup/watermark safety. This release does not claim reproduction or repair of any earlier real-model session.

## 0.2.29 — 2026-09-07

- Clarify the source-neutral Gate contract for tool records retained as `metadata`: they are not evidence units, cannot be bound by metadata identifiers, and cannot authorize CREATE or UPDATE.
- Normalize coverage dispositions from their declared reasons so `query_only`, `assistant_restatement`, and other known no-write reasons close as `NO_CHANGE`, while unresolved evidence, ownership, target, or Scope ambiguity remains `DEFERRED`.
- Add a bounded `invalid_evidence` correction path and regressions for metadata-only one-off operations, failed watermark recovery, cleanup eligibility, idempotent retry, and protection against knowledge/history pollution. Keep Markdown as the sole source of truth without adding SQLite, FTS, or other runtime services.

## 0.2.28 — 2026-09-06

- Separate rebuildable derived data in `_index/` from correctness/runtime state in `_state/`. Existing Vaults migrate processed-event, agent activation, host-ingest, retrieval-gate and compaction state crash-safely and idempotently; conflicting or corrupt legacy/current state fails closed, and `rebuild-index` never rewrites runtime state.
- Normalize supported legacy configuration into current names, including `capture.include_tool_output` to `capture.tool_evidence_mode`, reject conflicting legacy/current settings, preserve historical safe defaults, and document 0.2.x compatibility/deprecation behavior in `docs/config-migrations.md`.
- Move host activation bookkeeping to `_state/agents.json`, update installation/status paths accordingly, and keep Hermes/Codex shared-Vault behavior, permanent-memory global visibility, provenance-only session/source metadata, and native Hermes memory coexistence unchanged.
- Remove the remaining Core urgency-word classifier for unscheduled todos so ordering stays source-neutral; model-owned business semantics remain outside deterministic Core validation.
- Add reproducible 1k/10k/50k long-run benchmarking across retrieval, writes, lifecycle, locks, RSS and disk. The 50k-active dataset is explicitly an extreme stress boundary rather than a normal steady-state assumption; normal retrieval remains `Scope Map -> scope-constrained search -> read`, while UPDATE/NO_CHANGE, todo retirement, bounded history and compaction control active-memory growth.
- Do not introduce SQLite/FTS, vector storage, external databases, daemons or new runtime dependencies. Markdown remains the sole source of truth and `_index/` remains fully deletable/rebuildable.

## 0.2.27 — 2026-09-05

- Remove application-, document-, tool- and business-specific semantic classifiers from the Core admission/target path. The model owns future-use and atomicity judgments; Core retains source-neutral evidence, Scope, type, target, date, revision and conflict validation.
- Bound per-memory provenance to 16 retained source rows while preserving cumulative source count/digest metadata, preventing repeated UPDATEs and copied history versions from growing `sources` without limit.
- Retire completed/cancelled todos from active `knowledge/` after a configurable grace period (30 days by default) while keeping them queryable through historical todo enumeration.
- Add bounded history retention (`3650` days and `32` complete versions per stable identity by default) with an explicit `keep_all` opt-out for audit-oriented Vaults.
- Preserve an existing canonical memory identity during compaction instead of generating `mem-compact-*` IDs; rollback remains journaled and crash-safe.
- Keep Markdown as the source of truth, retain zero runtime dependencies and no daemon, and preserve Hermes/Codex shared-Vault retrieval and write contracts.

## 0.2.26 — 2026-09-05

- Refactor automatic processing into explicit model execution, planning, update coordination, commit, journal and audit owners while preserving Markdown Vaults, memory IDs, Hermes MemoryProvider and cross-Agent retrieval contracts. No runtime dependency or independent service is added.
