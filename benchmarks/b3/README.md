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
- CREATE, UPDATE and NO_CHANGE are allowed only when Core marks the bounded related-memory lookup complete; otherwise the model may only DEFER or account evidence as no-memory.
- UPDATE normally inherits target type/scope from Core. A cross-Scope correction may supply a replacement scope only when the existing deterministic Scope-correction contract independently authorizes it.
- Core supplies deterministic source references, memory IDs, target revision guards and other writer metadata after validating the model's evidence claims.
- Every evidence unit is either claimed by at least one memory action or accounted for exactly once as `no_memory`.
- Exact quote/whole-unit validation reuses the existing memleaf evidence-binding validator.
- Native shadows and Scope maintenance continue through the existing deterministic writer/maintenance contracts; B3 does not create a second semantic stage for them.
- Normal B3 execution is one model call. Malformed/invalid output may receive at most one bounded repair call, so the B3 semantic stage has a hard maximum of two model calls.
- Explicit `remember()` keeps the proven legacy path because it already uses a single summarize call.

## Activation and fallback

`Processor` constructs `SinglePassMemoryPlanner`, but B3 activates only when the resolved backend advertises `single_pass_safe=true`:

- fixed built-in API route: B3 single-pass;
- `auto` with no host callback and a safe built-in API route: B3 single-pass;
- host callback, custom callable or `auto` with a host route: automatic fallback to the P3 planner path.

This keeps host-owned callback behavior and legacy retry semantics unchanged while allowing the normal direct API route to use the lower-call architecture.

## Current phase

The B3 candidate is production-shaped on `experiment/b3-single-pass-planner-20260910` and is wired into `Processor` behind the backend capability gate. Repository-level focused tests cover CREATE/UPDATE/NO_CHANGE/DEFERRED, evidence accounting, retrieval completeness, revision freezing, Scope correction, native shadowing, Scope maintenance, diagnostics, retry budgets and P3 fallback.

It remains an unreleased experiment branch. `main` is unchanged. Before any merge/release decision, B3 still requires final repository CI plus real-model quality/performance validation against the retained P3 baseline.
