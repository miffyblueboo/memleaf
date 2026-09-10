# P4 Responsibility Consolidation Experiment (B2)

Baseline: `experiment/p3-batched-model-stages-20260910` at `63dcf291eb4c0d4eaf66961d6ad479d5b85dc494`.

This directory documents the isolated P4/B2 experiment described in the product optimization plan. P4 is not a release candidate and does not remove independent semantic review.

## Goal

Reduce repeated semantic work currently split across candidate admission, target reconciliation, summary generation, and coordination. The target architecture is:

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

P4 must not weaken the Markdown source of truth, shared Vault semantics, evidence/source authorization, history, native-memory cooperation, forget behavior, or writer/version checks.

## Phase 1: versioned maintenance-plan protocol

Protocol version: `p4-maintenance-plan-v1`.

Every model result is associated by exact `candidate_id` and the parser returns results in the original candidate order. Every candidate must appear exactly once.

### Core lookup states

| lookup status | meaning | allowed maintenance result |
|---|---|---|
| `complete_no_target` | lookup completed and Core found no target candidate | CREATE or DEFERRED |
| `complete_candidates` | lookup completed and exposed a bounded authorized target set | CREATE / UPDATE / NO_CHANGE / DEFERRED |
| `too_many_candidates` | target set is not safely bounded/exhaustive | DEFERRED only |
| `search_error` | local lookup failed | DEFERRED only |
| `evidence_insufficient` | evidence is insufficient for a safe maintenance decision | DEFERRED only |

The three incomplete states intentionally cannot degrade to CREATE. This prevents fixed Top-K truncation, lookup failure, or insufficient evidence from being misinterpreted as “no old target exists.”

### Decision contract

- `CREATE`: requires final `summary`; no target field.
- `UPDATE`: requires one `target_memory_id` authorized by the candidate's complete lookup plus final `summary`.
- `NO_CHANGE`: requires one authorized `target_memory_id`; no summary.
- `DEFERRED`: requires an allowlisted reason; incomplete lookup states further restrict the allowed reason class.

The protocol parser does not take ownership of summary semantics. A supplied `validate_summary(candidate_id, decision, target, summary)` callback remains authoritative for evidence/date/type/scope/content validation. This keeps P4 from silently replacing the existing summary validator while the architecture is still experimental.

## Phase 2: one bounded stage-two model call

`maintenance_plan_stage.py` adds the side-effect-free model-call boundary for stage two.

- One invocation accepts 1-4 candidates and performs exactly one `_complete_json_stage` call.
- Oversized batches/prompts fail before the model call; the helper never silently splits one heavy request into additional heavy requests.
- Gate-era `update_memory_id`, `duplicate_memory_id`, `duplicate`, `worth`, and arbitrary extra fields are deliberately excluded from the stage-two candidate projection. Final target/maintenance choice belongs after Core lookup.
- Admitted evidence accepts only visible `user`/`assistant` rows. Tool/raw external records cannot enter the new-fact authority channel.
- Local related memories are comparison context and must match the Core-authorized target set for a complete lookup.
- Native context is projected separately and is never target-authorizing.
- Output is parsed by `p4-maintenance-plan-v1`; there is no writer/Vault side effect in this phase.

The stage-two system contract states that current admitted evidence is the only authority for new facts or state changes; local/native memory is comparison/background only. UPDATE must preserve still-valid prior obligations and use an authorized local target. Incomplete lookup states must DEFER.

## Why this is separate from P3

P3 only changes transport granularity: CREATE summaries and final reviews can share bounded model calls. P4 changes **responsibility boundaries**. The current production path remains the comparison arm until B2 is separately integrated and validated.

## Current experiment status

- Phase 1 protocol: implemented and tested.
- Phase 2 stage-two prompt/call boundary: implemented and tested.
- Production `MemoryPlanner` integration: **not yet enabled**.
- Independent batch semantic review: retained and will remain the first integration safety baseline.
- Writer/Markdown/history behavior: unchanged.
- Real-model latency/token/quality claim: none yet.

The next integration step is to adapt existing Core lookup results and existing per-candidate summary validators into this stage-two contract, run it in an isolated planner path, and compare CREATE/UPDATE/NO_CHANGE/DEFERRED behavior against the P3 candidate before considering any production switch.
