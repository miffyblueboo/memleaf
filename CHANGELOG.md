# Changelog

All notable changes to memleaf are documented here.

## 0.2.26 — Unreleased

- Separate physical provenance from semantic role; validate exact candidate quotations and reject assistant-only/retrieved-memory authority. Remove whole-turn example suppression, lexical-overlap authorization and short-text bypasses.
- Add one bounded source-neutral coverage correction. Keep evidence accounting separate from candidate outcomes and never let a model erase known missing/truncated observations.
- Restrict automatic summarization to admitted spans and current admitted source keys, while preserving related memories as context.
- Preserve complete bounded structured records and source metadata across Hermes/Codex; report overflow and retention loss explicitly.
- Freeze complete write requests before mutation, replay without a fresh model call, verify target revisions under the Vault lock, and preserve actual UPDATE/scope-retirement audit outcomes.
- Add exact same-turn/commit-time deduplication that includes task titles; keep conflicting updates from overwriting newer shared state.
- Add local read-only audit and isolated process --dry-run, with bounded snapshots and concurrent-change detection.
- Migrate deterministic test stubs to the explicit evidence protocol without weakening their update/history/failure assertions; add adversarial quotation, transport, concurrency, replay and inspection tests.
- This is an unreleased candidate. Hosted-model semantic acceptance and cross-platform checks are reported separately, not inferred from scripted unit tests.

## 0.2.25 — 2026-09-04

- Keep global todo questions and cross-session recaps strictly read-only,
  without creating memories, appending sources, or producing history; an
  explicit user assertion before a follow-up question remains write-eligible.
- Recover project-grounded, explicitly counted repair items omitted from a
  mailbox extraction gate, split concrete detail lines into independent todos,
  and reuse an existing same-scope item when it is an update.
- Record compact candidate-level `CREATE`, `UPDATE`, `NO_CHANGE`, and
  `DEFERRED` dispositions in the processed-turn ledger for auditable coverage.

## 0.2.24 — 2026-09-04

- Make Hermes MCP installation reliably persistent: write `mcp_servers.memleaf` through Hermes' canonical `config set` interface, disable the entry while command/arguments are updated, then read `config.yaml` back and require an exact command plus `--vault` match before reporting success.
- Detect a same-Vault MCP entry that points at a different absolute memleaf runtime before changing Hermes. Add explicit `--mcp-runtime auto|current|existing` policies, prefer the current Python interpreter's scripts directory over `PATH`, and require an exact version check before retaining another environment.
- Snapshot Hermes `config.yaml`, `memleaf.json`, and `plugins/memleaf` during installation; on a later failure, attempt every rollback target even when one restore fails, and report the failed paths together with the installation stage and recovery commands.
- Improve human and JSON diagnostics for MCP persistence, runtime conflicts, lifecycle/test failures, and rollback results; document `memleaf-mcp --vault`, manual YAML, and the difference between Hermes connection discovery and final configuration persistence.
- Make Windows CI fail immediately after every native command and keep POSIX command-rendering assertions platform-specific, preventing intermediate Windows test failures from being reported as green.
- Leave memory extraction, CREATE/UPDATE/NO_CHANGE decisions, retrieval, injection, todo handling, and globally shared Vault visibility unchanged.

## 0.2.23 — 2026-09-03

- Add a strict `scope_correction` transaction for explicit cross-project corrections without weakening ordinary UPDATE scope isolation: uniquely recover the wrong active target, reuse its `memory_id` when appropriate, or retire it to history with `invalidated_reason: scope_correction` and `superseded_by` when a correct active survivor already exists.
- Deterministically split same-project `mixed_future_use` candidates after bounded model retries only when every clause is safely classifiable as durable project/fact state or an unfinished todo; ambiguous fragments remain deferred and valid siblings continue independently.
- Add bounded mail-tool evidence capture limited to message ID, subject, sender and sender domain, plus private per-scope domain identifiers; a unique domain/scope conflict defers extraction instead of writing a wrongly attributed project memory, while full tool output remains excluded.
- Preserve global `list_todos` pagination and unlimited aggregate managed reads from 0.2.20; no source/session ownership filter or aggregate read quota is reintroduced.
- Hermes still uses its existing Soft Gate in this package release. Generic fail-closed pre-final retrieval enforcement requires a Hermes host lifecycle hook and is tracked upstream in NousResearch/hermes-agent#101973.

## 0.2.22 — 2026-09-03

- Safely split valid multi-project `unscoped` aggregate candidates using only
  project names grounded in each candidate, so one ambiguous fragment no
  longer blocks valid project-local siblings.
- Persist explicit completion of an existing todo separately from new
  customer rework, preserving immutable targets and making replay idempotent.
- Require declarative completion language before closing todos, avoiding false
  completion for future actions such as needing to submit, waiting for
  feedback, or being asked to send materials.
- Keep Hermes from claiming permanent persistence before automatic processing
  succeeds; only a successful explicit remember/update result authorizes that
  claim.

## 0.2.21 — 2026-09-03

- Fix `memleaf install` for Hermes after the 12th MCP tool was added: the installer now uses the adapter's canonical tool-count constant instead of a stale private `11`.
- Add a regression invariant that compares the Hermes installer count with the actual public MCP tool tuple, preventing future tool additions from silently breaking installation self-checks.
- Correct the English README's stale 11-tool and 3-memory/6000-character descriptions so the published documentation matches the v0.2 retrieval contract.

## 0.2.20 — 2026-09-03

- Add `list_todos` for complete active todo retrieval across all scopes with stable pagination, date/status filtering, and current-turn retrieval gating.
- Remove the managed-turn 3-memory / 6000-character aggregate read block while retaining the 2000-character page limit and audit counters.
- Add first-class `todo.due_date` (`YYYY-MM-DD`) with evidence-grounded date normalization, update/history/compaction propagation, and legacy Markdown compatibility.
- Improve automatic extraction so concrete unfinished actions become atomic todos and durable rules remain project/fact memories; mixed rule/action candidates are split or safely deferred without blocking valid siblings.
- Keep permanent `knowledge/` globally visible to all sessions and supported Agents sharing the same Vault; provenance fields never filter active memory visibility.

## 0.2.19 — 2026-09-03

- Keep Hermes `<untrusted_tool_result>` search audit status synchronized with
  Core for `FOUND` and `NO_MATCH` results using bounded, strict decoding;
  malformed and error envelopes remain audit errors.
- After three `mixed_future_use` gate responses, defer only the affected
  candidate so valid candidates from the same turn can continue processing;
  mixed content is never persisted and unrelated schema violations still fail.

## 0.2.18 — 2026-09-02

- Register Hermes native `MEMORY.md` and `USER.md` as private, read-only native sources during supported install/upgrade flows while preserving custom native source configuration.
- Harden cross-turn deduplication and UPDATE/NO_CHANGE decisions: split independent future-use clauses, reuse one unambiguous same-scope active target, treat exact active duplicates as deterministic no-ops, and keep crash replay idempotent.
- Preserve the existing bounded Scope Map / metadata-directory retrieval flow without adding extra scope scans during automatic deduplication.
- Distinguish Hermes controlled read audit states: record `FOUND_READ` only after a current-token successful read, and use `FOUND_NO_READ_UNDETERMINED` when a FOUND result has no provable controlled read instead of overstating Soft Gate guarantees.

## 0.2.17 — 2026-09-02

- Preserve explicit user-confirmed completion or cancellation of one uniquely
  related active todo as an in-place state update, even when the gate omits the
  candidate or the same message asks what remains.
- Keep todo-state recovery deterministic and bounded: reuse the existing
  memory ID, anchor `completed_at` to the user event, archive the old version,
  and reject queries, negation, future intent, uncertainty, assistant-only
  claims, and ambiguous targets.

## 0.2.16 — 2026-09-02

- Keep read-only operational queries, derived overdue counters, and one-time
  execution receipts out of permanent-memory extraction; preserve explicitly
  confirmed durable facts and preferences.
- Deduplicate Hermes tool observations across cumulative conversation payloads,
  preventing historical reads from being re-audited as uncontrolled while
  retaining diagnostics for genuinely mismatched current-turn reads.

## 0.2.15 — 2026-09-02

- Recover a candidate whose `update_memory_id` has the wrong type at the
  candidate level after bounded gate retries, without blocking unrelated
  candidates or changing the active target's immutable type.
- Require candidate-local Scope membership and future-use relevance before
  accepting a duplicate or update target, keeping cross-project targets
  deferred and preventing unsafe overwrites.
- Keep duplicate validation independent from UPDATE type validation, and
  safely defer unknown or same-use type-mismatch targets instead of creating
  a sibling memory or failing the whole inbox turn.

## 0.2.14 — 2026-09-02

- Reject a permanent-memory candidate that combines a durable project rule or
  implementation-plan change with an independent dated todo, while preserving
  multiple dated milestones that belong to one project plan.
- Normalize explicit implementation plans and plan adjustments to `project`
  without promoting adjacent mail, attachment, meeting, or archive records.
- Keep the gate-selected type immutable through summarization for both CREATE
  and UPDATE, preserve multiple legal update candidates, and expose the new
  `mixed_future_use` diagnostic through the Hermes provider.

## 0.2.13 — 2026-09-02

- Preserve the full active project-plan body, stable title, and retrieval
  metadata when a customer suggestion only adds constraints; explicit
  replacements continue to replace conflicting state normally.
- Reject attachment, document, presentation, material, and issue-list items
  described only by transport metadata or a generic follow-up at both the gate
  and summary boundaries.
- Keep concrete attachment-backed actions eligible when they include a
  remediation, owner, deadline, delivery rule, deployment, or migration.

## 0.2.12 — 2026-09-02

- Remove empty ASCII or Chinese parentheses left after an absolute date is
  normalized, so permanent memory never stores values such as
  `2026-09-03（）`.
- Reconcile a same-scoped implementation-plan candidate with its unique active
  project-plan memory even when the model labels the candidate as a fact or
  omits `update_memory_id`; preserve the target's immutable type and history.
- Exclude sent-mail, attachment, archive, and meeting records from automatic
  plan-target inference, and defer instead of guessing when multiple project
  plan targets remain.
- Reject combined mailbox or daily-report count/status digests as permanent
  memory while keeping separately emitted atomic actions eligible.

## 0.2.11 — 2026-09-02

- Document one complete macOS/Linux upgrade command that upgrades the core
  package and refreshes the Hermes Provider together; report and verify both
  versions after installation instead of silently accepting a stale copy.
- Compare the copied Provider manifest with the MCP server version at Hermes
  startup and emit the same actionable one-line upgrade command when they
  differ or cannot be verified.
- Normalize supported one-off Chinese and English relative dates
  deterministically from their current evidence timestamps before strict
  summary validation, including conflicting parenthesized numeric dates.
- Keep ambiguous or unsupported relative-date candidates retryable without
  guessing, while allowing valid sibling candidates from the same turn to
  commit and preserving the source inbox.

## 0.2.10 — 2026-09-01

- Keep strict candidate-local project Scope grounding for the first two gate
  attempts, then isolate a persistently invalid candidate instead of failing
  the entire mailbox turn.
- Correct a final-attempt model Scope only when the candidate text identifies
  exactly one registered project name or alias; defer zero-match and
  multi-project candidates as insufficient context without writing memory.
- Preserve valid sibling candidates, new explicitly named project Scopes, and
  the existing update-target relevance guard when Scope and target errors occur
  together.

## 0.2.9 — 2026-09-01

- Reject update and duplicate targets that are relevant only to another item
  in the same aggregate turn, preventing cross-project memory overwrites.
- Retry an unrelated gate target with an explicit correction, then recover
  deterministically after the bounded final attempt: preserve unrelated
  updates as independent candidates and discard unsafe duplicate claims.
- Query related memories from each candidate's own complete topic instead of
  the whole mailbox turn, while allowing the remaining valid candidates to be
  processed without holding the retained inbox turn hostage.

## 0.2.8 — 2026-09-01

- Let the automatic summarizer independently reject an over-admitted gate
  candidate with an exact `NO_CHANGE` response, without writing memory,
  history, or Scope state; explicit remember remains write-oriented and does
  not permit this response.
- Ground every model-attributed project Scope in the candidate's own project
  name or registered alias, and prevent the summarizer from drifting away
  from the gate-selected Scope.
- Register session and Vault Scopes only for committed memories or trusted
  active duplicate targets, so temporary, deferred, and no-change mailbox
  items cannot pollute Scope state.

## 0.2.7 — 2026-09-01

- Distinguish an invalid gate type enum from a candidate type that conflicts
  with its selected active update target, while preserving the strict,
  immutable target-type constraint.
- Give each gate failure a bounded, actionable retry instruction: use a legal
  memory type, preserve the target type for the same future use, or split a
  genuinely different future use into an independent atomic candidate.
- Preserve the dedicated `update_target_type_mismatch` diagnostic across Core,
  MCP, and Hermes; repeated failures still write nothing and retain the inbox
  for a later retry.

## 0.2.6 — 2026-09-01

- Reject one worthy memory that spans multiple independent project scopes and
  retry with an explicit instruction to split actionable project topics while
  discarding temporary mailbox-sweep status items.
- Tighten automatic admission so aggregate mailbox/daily-report snapshots,
  one-off meetings without durable outcomes, pending PM acceptance, and
  awaiting-feedback states do not become a cross-project permanent memory;
  keep independent risks and actionable todos eligible.
- Prefer `UPDATE` or `NO_CHANGE` for an existing future-use topic, preserve
  unimplemented customer requests as proposed or pending, and recognize the
  Chinese relative-date forms `今日`, `明日`, and `昨日` in strict retries.

## 0.2.5 — 2026-09-01

- Detect multiple gate candidates that target the same active memory before
  batch writing, then retry with an explicit instruction to merge the
  candidates while preserving all supporting evidence.
- Keep gate-selected update targets and active-memory types stable through
  summarization, with bounded corrective retries for target or type drift.
- Preserve `duplicate_update_target` and aggregate preflight diagnostics across
  Core, MCP, and Hermes while retaining the inbox and zero-write transaction
  boundary after repeated failure.

## 0.2.4 — 2026-09-01

- Give `relative_time` retries explicit, actionable correction instructions so
  the summarizer recalculates one-off dates from evidence timestamps, removes
  relative wording, and resolves conflicting parenthesized dates before the
  strict validator accepts the memory.
- Preserve the `relative_time` validation detail across the Hermes provider
  boundary instead of degrading it to `other_schema_violation` in diagnostics.

## 0.2.3 — 2026-09-01

- Preserve capture event timestamps in extraction prompts and resolve one-off
  relative calendar dates against the supporting event timestamp as
  `YYYY-MM-DD`; reject unresolved relative dates with bounded retries while
  retaining the inbox turn after repeated failure.

## 0.2.2 — 2026-09-01

- Publish GitHub Release and PyPI from the same wheel and source distribution
  artifact verified by the CI build job; the PyPI workflow accepts only a
  successful push from this repository's `main` branch.
- Make GitHub Release publication idempotent: an existing tag must resolve to
  the current release commit before its three assets are replaced or completed.
- Normalize resolved paths in the partial-delete regression test so the
  macOS `/var` and `/private/var` aliases exercise the intended failure path.

## 0.2.1 — 2026-08-31

- Fixed the finalized `search → read` contract in both supported hosts: Codex `PostToolUse` and Hermes diagnostics now accept only `memory_id + title` search candidates, so a real `found` result no longer degrades to an error before `read`.
- Enforced current-turn `retrieval_id` validation at the MCP boundary for both Codex and Hermes, preventing a still-present historical token from being reused for managed search/read after a newer turn begins.
- Made Codex model discovery fail closed. Codex remains a host only; memleaf no longer reads or reuses Codex provider/model/auth configuration even if legacy discovery APIs are called explicitly.
- Made multi-host Vault reuse compare physical paths rather than raw path strings, reducing false conflicts from equivalent host paths on Windows/macOS while still rejecting genuinely different Vaults.
- Hardened forget failure recovery: linked history is deleted before active knowledge, partial filesystem failures rebuild derived indexes, and the active memory remains a retry anchor until linked history deletion succeeds.
- Standardized Hermes installer subprocess decoding on UTF-8, matching the provider/Codex host boundaries and preventing non-ASCII CLI output from being corrupted by a Windows default code page.
- Removed dead Codex-discovery helpers after the host/model boundary was made fail closed.
- Re-ran the full Linux 3.11/3.12/3.13, Windows 3.11/3.12/3.13, macOS 3.11/3.13, native Codex Windows/macOS, package-build, wheel-install, and sdist-test release gates.

## 0.2.0 — 2026-08-31

- Hardened the supported Codex integration against the official Codex CLI 0.151.0 on Windows and macOS, including `CODEX_HOME`, npm `codex.cmd` discovery, UTF-8 CLI output, idempotent `mcp add/get`, and quote-free Windows lifecycle commands carried through a UTF-16LE PowerShell `EncodedCommand` payload.
- Aligned Codex `PreToolUse` argument rewriting with the current hook contract by returning `permissionDecision=allow` together with `updatedInput`, preserving the current-turn `retrieval_id` binding.
- Restored the finalized automatic retrieval contract: the bounded Scope Map exposes only Scope metadata, `search` candidates expose only `memory_id + title`, and selected memory bodies enter context only through bounded `read` calls under the existing retrieval gate and budgets.
- Added durable shared-Vault acceptance for Codex → Codex, Hermes → Codex, and Codex → Hermes so cross-session and cross-host memory reuse is verified without duplicating CREATE/UPDATE/NO_CHANGE, history, deduplication, or processing semantics outside the Core.
- Kept Codex installation explicitly opt-in and fail-closed: existing Codex model/provider, profiles, sandbox/approval settings, MCP entries, hooks, and custom providers remain untouched; conflicting host Vaults are rejected rather than guessed.
- Decoupled the Codex host from the extraction model. Automatic processing uses an independent memleaf Model Route, reports `processing_status=model_route_required` when no complete route exists, and never copies Codex credentials or silently consumes the active Codex session model.
- Promoted native Codex Windows/macOS acceptance, cross-host memory roundtrips, Linux/Windows/macOS host tests, and packaging checks into release-blocking CI gates.

## 0.1.8 — 2026-08-31

- Fixed Windows UTF-8 stdio handling across the MCP server, Hermes MemoryProvider, and Codex host-event entry point so Chinese and other non-ASCII memory content can be captured, searched, read, and injected without depending on the system code page.
- Added native Windows coverage for Codex executable discovery and Hermes MCP subprocess execution, including Unicode paths and payloads.
- Strengthened Windows and macOS release gates to exercise Hermes and Codex installation, host lifecycle, bounded Scope Map injection, controlled `search` → `read`, retrieval budgets, memory extraction/update semantics, and session-lineage behavior before release.

## 0.1.7 — 2026-08-31

- Added supported Codex integration through the explicit
  `memleaf install --host codex` command, including MCP registration through
  the Codex CLI and merge-safe lifecycle hooks that remain subject to Codex's
  user review and trust flow.
- Added the Codex V2 memory lifecycle: bounded Scope Map injection, controlled
  `search` → `read` retrieval, visible-turn capture, automatic processing, and
  compact-session restoration without injecting memory bodies up front.
- Preserved Hermes conversation lineage across context compaction so complete
  source turns remain available to extraction instead of summarizing only the
  post-compaction fragment.
- Improved memory maintenance to keep durable business facts and constraints,
  discard incidental execution details, and apply sequential state updates to
  one stable memory with prior versions in history.
- Consolidated the Hermes MemoryProvider into one packaged implementation and
  made every installer and test consume that source.
- Added macOS Codex lifecycle CI and expanded Linux/Windows packaging and host
  regression coverage.

## 0.1.6 — 2026-08-29

- Made the normal install command the supported upgrade path for existing memleaf installations.
- Preserve the Vault already configured in Hermes `memleaf.json` during upgrades, including custom Vault locations used by early releases.
- Added deterministic Vault precedence: explicit `--vault` → existing Hermes config → `MEMLEAF_VAULT` → default `~/.memleaf`.
- Fail safely when an existing Hermes memleaf configuration is malformed instead of silently switching the user to a new default Vault.
- Added Linux and Windows regression coverage for upgrade Vault selection.


## 0.1.5 — 2026-08-29

- Fixed the Hermes MemoryProvider stdio client on Windows by replacing
  `select.select()` on subprocess pipes with a background stdout reader and
  timeout-aware queue.
- Added a real cross-platform Provider → `memleaf-mcp` integration test that
  exercises `stats`, `scope_catalog`, and visible-turn `capture` through
  the actual stdio subprocess boundary.
- Windows CI now runs the real Hermes provider transport acceptance test on
  Python 3.11, 3.12, and 3.13.
- Kept provider tool registration intentionally empty: the MemoryProvider owns
  automatic recall/capture/process while the separately configured MCP server
  owns the 11 deliberate tools.

## 0.1.4 — 2026-08-28

- Reject Hermes display-redacted API credentials such as `***` and head/tail masks instead of treating them as callable keys.
- Fall through from a redacted direct credential to configured environment variables and Hermes `.env` values.
- Reject masked credentials in existing memleaf routes and at runtime so an old bad route cannot remain silently usable.
- Add regression coverage for masked Hermes credentials on Linux and Windows.

## 0.1.3 — 2026-08-28

- Restored the MIT License for the current release.
- Added native Windows Hermes path discovery using `HERMES_HOME` or the official
  `%LOCALAPPDATA%\hermes` default.
- Added discovery for the official Windows Hermes launchers under
  `bin\hermes.exe`, `bin\hermes.cmd`, and
  `hermes-agent\venv\Scripts\hermes.exe`.
- Added a one-line PowerShell installer that uses Hermes' managed Python when
  available, installs memleaf from PyPI, and completes Hermes integration.
- Made atomic file writes and Windows command-launcher detection portable.
- Added Windows GitHub Actions coverage for Python 3.11, 3.12, and 3.13.

## 0.1.2 — 2026-08-28

- This release was published under AGPL-3.0-only before the project returned to
  MIT in 0.1.3. Its original license remains valid for copies of 0.1.2.
- Product behavior otherwise remained aligned with 0.1.1.

## 0.1.1 — 2026-08-28 (GitHub tag: v0.1.1)

- Added a first-class `memleaf install` command for complete Hermes setup
  after installing the package from PyPI.
- Packaged the Hermes MemoryProvider inside the Python distribution, removing
  the need for a Git checkout during normal installation.
- Added safe migration from the source-installed provider symlink to the
  packaged provider.
- Added `python -m memleaf` so the complete one-line install works even when
  the console-script directory is not already on PATH.
- The installer initializes the Vault, discovers or preserves a model route,
  activates the Hermes MemoryProvider, configures MCP lifecycle settings, and
  verifies the 11-tool MCP surface.

## 0.1.0 — 2026-08-28 (GitHub tag: v0.1)

- Added the local-first, dependency-free Markdown vault core with capture,
  memory creation, deterministic search/context retrieval, statistics, and
  rebuildable indexes.
- Added safe vault paths, file locking/atomic writes, frontmatter handling, and
  secret redaction for captured text.
- Added model-backed admission, extraction, state updates with history,
  compaction, retryable processing, and scope-aware maintenance without adding
  runtime dependencies.
- Added the `memleaf` initialization CLI and the `memleaf-mcp` stdio entry
  point.
- Added Scope Map → directory search → bounded read retrieval with per-turn
  retrieval state, pagination, and read budgets.
- Added a Hermes-native MemoryProvider plus MCP setup.
- Added packaging metadata, examples, offline local installation, and CI for
  Python 3.11–3.13.
