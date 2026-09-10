# P0 correctness finalization — 2026-09-10

This note records the final deterministic follow-up on the unreleased
`fix/p0-correctness-baseline-20260910` candidate. It is not a release and does
not claim real-model performance.

The Gate coverage parser now enforces the exact decision-specific row shapes
already declared by the Gate prompt/protocol while preserving the existing
`coverage_terminal_witness` diagnostic priority. An executable decision/claim
matrix covers canonical quote claims, legacy explicit offsets, whole-unit
claims, CANDIDATE, NO_CHANGE, DEFERRED, terminal TODO witnesses, strict binding
rows, and rejection of cross-variant fields.

The stricter F02 contract invalidated one old F06 test fixture that had used a
`CANDIDATE` row with an ancillary `reason` field as though it were canonical.
The product parser was not relaxed. Instead, the F06 bool-vs-number lossless
repair regression was migrated to a canonical candidate boolean field. This
preserves the F06 type-safety assertion without depending on the pre-F02
permissive coverage shape.

The candidate also includes the response-vs-transport metrics correction:
provider responses rejected as `model_invalid_response` are failed + invalid
output, while ordinary transport failures are failed only. No retry budget,
prompt semantics, Markdown commit semantics, Vault sharing, Scope, forget, or
history behavior is changed by these final follow-ups.

A normal repository push CI run is required after this note. The P0 candidate
must not be treated as final until Linux, Windows, macOS, build/wheel/sdist, and
Codex-native jobs all pass on the resulting head SHA.
