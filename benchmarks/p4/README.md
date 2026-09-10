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
| `complete_no_target` | lookup completed and found no target candidate | CREATE or DEFERRED |
| `complete_candidates` | lookup completed with a bounded authorized local target set | CREATE / UPDATE / NO_CHANGE / DEFERRED |
| `too_many_candidates` | target set is not safely bounded/exhaustive | DEFERRED only |
| `search_error` | local lookup failed | DEFERRED only |
| `evidence_insufficient` | evidence is insufficient for a safe decision | DEFERRED only |

The three incomplete states cannot degrade to CREATE. UPDATE/NO_CHANGE targets must be copied from the candidate's authorized local target IDs.

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

## Current status and next step

- Phase 1 protocol: implemented/tested.
- Phase 2 stage-two prompt/call boundary: implemented/tested.
- Phase 3 stage-one role/parser wrapper: implemented/tested.
- Production `MemoryPlanner` integration: **not enabled**.
- Independent batch semantic review: retained.
- Writer/Markdown/history behavior: unchanged.
- Real-model latency/token/quality claim: none yet.

Next: build an adapter around the existing Gate coverage/evidence machinery and existing Core `_related_query` results, then run the P4 path in an isolated comparison harness. Only after CREATE/UPDATE/NO_CHANGE/DEFERRED parity and retained-set checks pass should any production switch be considered.
