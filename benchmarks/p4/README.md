# P4 Responsibility Consolidation Experiment (B2)

Baseline: `experiment/p3-batched-model-stages-20260910` at `63dcf291eb4c0d4eaf66961d6ad479d5b85dc494`.

P4 is an isolated architecture experiment. It does not remove independent semantic review and is not a release candidate.

## Target responsibility graph

```text
validated visible turn
  -> stage 1: identify long-term candidates + evidence + initial scope
  -> Core: local lookup of relevant current/native memory
  -> stage 2: batch maintenance plan + final content
       CREATE / UPDATE / NO_CHANGE / DEFERRED
  -> Core: validate protocol, evidence, target, version, association
  -> independent batch semantic review (retained initially)
  -> writer / history / retrieval
```

The experiment must preserve the Markdown source of truth, shared Vault semantics, evidence/source authorization, history, native-memory cooperation, forget behavior, and writer/version checks.

## Phase 1: versioned maintenance-plan protocol

Protocol version: `p4-maintenance-plan-v1`.

Every result is associated by exact `candidate_id`; every candidate must appear exactly once; parser output is restored to original candidate order.

### Core lookup states

| lookup status | meaning | allowed result |
|---|---|---|
| `complete_no_target` | lookup explicitly completed and found no target candidate | CREATE or DEFERRED |
| `complete_candidates` | lookup explicitly completed with a bounded authorized local target set | CREATE / UPDATE / NO_CHANGE / DEFERRED |
| `too_many_candidates` | target set is not safely bounded/exhaustive, including an explicitly incomplete lookup | DEFERRED only |
| `search_error` | local lookup failed | DEFERRED only |
| `evidence_insufficient` | evidence is insufficient for a safe decision | DEFERRED only |

The three incomplete states cannot degrade to CREATE. A zero-row result is CREATE-safe only when Core separately supplies `lookup_complete=true`. UPDATE/NO_CHANGE targets must be copied from the candidate's authorized local target IDs.

A supplied `validate_summary(candidate_id, decision, target, summary)` callback remains authoritative for summary/evidence/date/type/scope/content validation, so P4 does not silently replace existing Core validators.

## Phase 2: bounded stage-two maintenance call

`maintenance_plan_stage.py` provides the side-effect-free stage-two model boundary.

- 1-4 candidates per invocation; exactly one `_complete_json_stage` call.
- Oversized batches/prompts fail before the call; there is no hidden splitting into additional heavy calls.
- Gate-era target/final-decision fields are excluded from the candidate projection.
- Only admitted visible `user`/`assistant` evidence can authorize new facts.
- Local related memories are comparison context and must match Core's authorized target set when lookup is complete.
- Native context is separate background and never target-authorizing.
- Output is parsed by `p4-maintenance-plan-v1` and still has no Vault/writer side effect.

## Phase 3: stage-one identification role

`candidate_identification.py` narrows the first model role so it stops performing maintenance work that belongs after lookup.

- Stage one identifies atomic future-use candidates, evidence and initial scope only.
- It explicitly does **not** decide CREATE/UPDATE/NO_CHANGE and cannot return `duplicate_memory_id` or `update_memory_id`.
- Existing Gate candidate validation is reused for source/type/scope/evidence shape; for compatibility, returned candidates temporarily carry fixed `duplicate=false` and `worth=true` flags.
- Non-future-use evidence is represented by coverage only instead of fake worth=false candidate rows.
- `run_identification_stage` uses the existing Gate transport/purpose and performs exactly one model call; existing coverage/binding orchestration can be reused when the experiment is integrated.

This removes target selection from the intended stage-one responsibility. Core lookup becomes the first point where target candidates are available, and stage two becomes the single place that decides CREATE/UPDATE/NO_CHANGE/DEFERRED and writes final content.

## Phase 4: Core adapter and no-write comparison harness

`maintenance_plan_adapter.py` translates existing Core-style lookup results into the versioned P4 contract without adding business inference.

- Lookup completeness is a required explicit input. Empty result sets never prove completeness on their own.
- Search failure and evidence insufficiency override any apparent completeness and force incomplete lookup states.
- More than the configured bounded local-target limit becomes `too_many_candidates`; the adapter does not truncate it into a false complete target set.
- Native, history and inactive records are excluded from local writable targets. Native context remains separately available as comparison context.
- `make_existing_summary_validator()` converts an existing per-candidate parser factory into the P4 summary-validation hook. The current summary parser remains authoritative for evidence/type/scope/date/target rules.
- `compare_shadow_outcomes()` compares candidate/decision/target parity only. It has no model or Vault side effects and deliberately does not pretend to judge semantic truth or final-summary quality.

`structure_audit.py` records derived zero-model-call call graphs. For 4 ordinary independent CREATE candidates that fit one identification call:

- P3: Gate 1 + CREATE Summary batches 2 + final review batch 1 = 4 semantic model calls.
- P4: identification 1 + maintenance-plan batch 1 + final review batch 1 = 3 calls.
- Derived structural reduction: 25% total calls.

For the conditional case where all 4 candidates require the current fresh-target reconciliation model step, P3 structurally reaches 10 calls while P4 remains 3. That 70% reduction is explicitly a **conditional call-graph scenario**, not an assertion about ordinary frequency, tokens, latency, quality or production throughput.

P4 continues to retain independent semantic review; no review omission is credited as a speedup.

## Current status and next step

- Phase 1 protocol: implemented/tested.
- Phase 2 stage-two prompt/call boundary: implemented/tested.
- Phase 3 stage-one role/parser wrapper: implemented/tested.
- Phase 4 lookup adapter, existing-parser adapter, no-write parity harness and structural audit: implemented/tested.
- Production `MemoryPlanner` integration: **not enabled**.
- Independent batch semantic review: retained.
- Writer/Markdown/history behavior: unchanged.
- Real-model latency/token/quality claim: none yet.

Next: build an isolated end-to-end P4 comparison runner that reuses the existing Gate coverage/evidence machinery and Core lookup fixtures, produces P3-vs-P4 disposition reports, and never writes the real Vault. Only after retained-set parity/quality checks should a production planner switch be considered.
