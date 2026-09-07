# Gate evidence boundary

## Design decision

Keep Markdown as the permanent-memory source of truth, the shared Vault,
`_index/` / `_state/` separation, scope-directed retrieval, and the separate Gate
and Summarize stages. Do not repair extraction failures by accepting unverified
evidence or by adding business-specific classifiers.

The Gate owns semantic decisions. The host owns physical evidence authority and
accounting. A model must not be asked to re-decide a physical constraint the host
already knows, such as whether text was written by the assistant or whether an
external observation has retained source content.

## Input and accounting responsibilities

Keep the complete evidence inventory for provenance, input digests, replay and
local audit. Project only physically admissible units to the model's evidence
list:

| Source | Model evidence | Local handling |
| --- | --- | --- |
| User text, including questions, examples and quotations | Yes | Model decides future value and coverage |
| Complete, matched external observation | Yes | Model decides future value and coverage |
| Assistant synthesis | No | Context only; no independent write authority |
| Retrieved memleaf/native memory | No | Context only; no new evidence |
| Missing, failed or incomplete external observation | No | Unresolved; retain the source turn |
| Intentionally metadata-only tool record | No | Honor capture policy; never fabricate source text |

Use the physical `can_support` boundary for this projection. `origin` and
`eligible` contain syntax hints and must not become semantic filters for user
text. A user can state a real fact inside a question or a code example; the Gate
must still see and evaluate that text. Conversely, an exact quote from user text
proves provenance, not the truth or future value of a proposed memory.

The Gate can retain user/assistant conversation context for pronouns and explicit
adoption of proposals. Context is not an additional source of bindable unit IDs.
Metadata call IDs, digests and tool names are not substitute source text.

## One Gate contract

The documented response contains `candidates`, `coverage` and
`evidence_bindings`. Coverage accounts for every model-visible evidence unit,
including a decision that no candidate is warranted. An empty candidate list is
not, by itself, proof of complete coverage.

The three empty lists describe a complete response only when there are no
model-visible evidence units. With units present, no-admission still needs
explicit `NO_CHANGE` or `DEFERRED` accounting. Examples must obey the same rules
as the validator and must not preclassify the first real input as a worthy fact.

Legacy candidate-only responses use the same evidence checks. Exact support or
validated bindings account for the supported units; remaining units take the
bounded coverage-repair path. Missing accounting must not
silently become permission to clean the source turn. Unknown unit IDs,
contradictory coverage, invalid spans and unauthorized sources remain errors.

## Diagnostics and recovery

Preserve the existing failure category, retry bound and watermark behavior.
Record a separate, allowlisted evidence-check identifier for the failing
constraint. Diagnostics must not contain raw model responses, message text,
credentials or arbitrary exception strings.

An unknown unit reference must identify the exact response field, such as
`coverage[2].unit_id` or `evidence_bindings[0].claims[1].unit_id`. Persist only
the field path, value type/length/digest and expected-set count/digest. The
invalid value and legal ID list do not belong in normal logs or failed state.

A bounded retry receives the failed constraint, exact field path and the legal
IDs from the same immutable model-visible inventory used by the validator.
These IDs are a reference constraint, not a suggested semantic decision. The
model must regenerate a consistent response; the host must not substitute a
nearby ID, guess a source, or relax evidence authority. Prompt examples must not
provide a literal placeholder that looks like a usable evidence reference.

A failed Gate cannot advance the turn watermark or create a cleanup deadline.
A successful no-change decision can advance the watermark and start the normal
retention period. Incomplete physical observations remain deferred even if other
parts of the turn finish. Replaying an already completed operation must remain
idempotent.

## Capture policy is a separate capability boundary

`metadata` intentionally excludes tool-result bodies from extraction. The host
having read a document does not mean the extraction model has its source text.
Do not change this policy implicitly, reconstruct removed bodies from assistant
prose, or re-read external systems merely to make a failed Gate pass.

`bounded` permits retained complete records; it does not promise complete
extraction from arbitrary bulk stdout. Record-count and body-size limits can
produce explicit omissions or partial observations. A large batch of results
inside one text blob is not equivalent to separately matched complete records.
That adapter/capture limitation must be reported independently of Gate success.

## Verification

- A long assistant answer does not expand the model's evidence coverage list;
  the full local audit inventory remains available.
- User questions, quotations and mixed question/assertion text remain in the
  model projection. No local keyword rule decides their future value.
- Complete external facts can be admitted; assistant, retrieved, metadata and
  incomplete observations cannot authorize independent writes.
- Missing coverage is corrected or retained as unresolved, not silently cleaned.
- Invalid unit IDs, duplicate coverage, invalid reasons, invalid spans and
  binding/coverage conflicts produce safe distinguishable diagnostics.
- Failure, retry, watermark, cleanup eligibility and replay are verified together
  in an isolated Vault without changing an existing user's Vault.
- Deterministic tests establish the protocol. Live synthetic tests establish
  behavior only for those samples. A synthetic success does not identify the
  cause of an earlier real-model failure; do not claim otherwise.

A successful replay is evidence about that new run. If an earlier raw response
was not retained, its precise invalid value cannot be recovered from an
`unknown_unit` category alone. Verify recovery with controlled invalid coverage
and binding references as well as ordinary live samples, and distinguish those
results from reproduction of the original incident.
