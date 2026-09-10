# B3 Single-pass Memory Planner

Baseline: P3 `63dcf291eb4c0d4eaf66961d6ad479d5b85dc494`.

B3 deliberately replaces repeated model responsibilities with one semantic planning/writing call:

```text
current visible evidence
  -> Core local retrieval (0 model calls)
  -> one B3 model call
       discover atomic memories
       CREATE / UPDATE / NO_CHANGE / DEFERRED
       final content
       exact evidence claims
  -> deterministic Core validation
  -> writer
```

P3 remains the rollback point. B3 does not use the P4 identification + maintenance two-model-stage design.

## Contract principles

- Current visible user/assistant evidence is the only authority for new facts/state changes.
- Existing local/native memory is comparison context only.
- Native memory is never an UPDATE/NO_CHANGE target.
- Existing local target IDs are authorized by Core, not invented by the model.
- CREATE can be disabled by Core when retrieval was not complete enough to safely prove no target.
- The model does not repeat deterministic output fields inside the memory body object: Core supplies type/scope/source/target/source references where it already has authority.
- Every evidence unit is either claimed by at least one memory action or accounted for exactly once as `no_memory`.
- Exact quote/whole-unit validation reuses the existing memleaf evidence-binding validator.
- Normal B3 execution is one model call. Existing `_complete_json_stage` may still perform its bounded format-repair behavior on malformed JSON; there is no second semantic review call in the B3 contract.

## Current phase

Phase 1 implements the side-effect-free single-pass protocol, compact prompt builder and deterministic parser/target/evidence boundaries. It is not yet wired into production `MemoryPlanner` and does not change `main` or release behavior.
