# Gate audit follow-up (unreleased local validation)

Base: `fix/gate-protocol-repair-v041` at
`b5d3b3cdd4eaa0295e464fe5d0c9543f6092906f` (package metadata remains 0.2.40).
This patch is not a release, a new published version, or evidence of live-model
performance. It adds no runtime dependency and does not change the permanent
Markdown source of truth, sharing semantics, prompts, or retry budgets.

## Coverage repair

The coverage-repair path must pass the mapping returned by `validate_bindings`
to `resolve_omitted_candidate_event_ids`. The original bindings list is retained
for envelope reconstruction and the existing final Gate parser. Do not catch
`AttributeError` broadly or require redundant event IDs as a workaround.

The regression covers successful siblings, failed corrections, retry of only
unresolved evidence, byte-preservation of already committed records, and no
extra history or duplicate creation. It stubs model responses; source binding,
planning, parsing, journal and Markdown operations are real.

## Gate protocol shape contract

Coverage rows now enforce the same canonical field sets already documented by
the Gate protocol instead of only rejecting globally unknown field names:

* `CANDIDATE`: exactly `unit_id`, `decision`, `candidate_ids`.
* ordinary `NO_CHANGE` / `DEFERRED`: exactly `unit_id`, `decision`, `reason`.
* `already_completed`: exactly the ordinary decision fields plus `memory_id`.

This closes a parser/prompt mismatch where a `CANDIDATE` row could carry a
recognized-but-inapplicable `reason`, or a non-candidate decision could carry an
empty `candidate_ids` list and still pass the earlier truthiness check. The
special `coverage_terminal_witness` diagnostics retain priority when
`memory_id` is missing for `already_completed`, appears on a candidate, or is
otherwise misplaced; generic cross-variant fields use `coverage_shape`.

`tests/test_gate_protocol_matrix_v041.py` is the executable decision/claim
matrix requested by the audit. It covers canonical quote claims, legacy explicit
offsets, whole-unit claims, CANDIDATE/NO_CHANGE/DEFERRED/terminal coverage rows,
cross-variant field rejection, strict binding-row shape, and preservation of the
terminal-witness diagnostic contract. The parser remains strict; no legacy
business semantic or topic heuristic was introduced.

## Metrics contract

`failed_calls` means the model call itself did not produce a usable text result
for the stage. Transport/timeouts/auth/rate-limit failures are failed calls. A
backend response that is received but violates the response contract (for
example invalid provider JSON/shape, empty text, or a non-text callback result)
is also a failed call.

Additive fields:

* `invalid_output_count` in total, stage, and operation buckets: a response was
  received from the backend but rejected either by the backend response-shape
  contract (`model_invalid_response`) or by `ModelOutputError` during JSON,
  schema/evidence, or lossless-repair validation. Count once per received
  response, including a swallowed coverage-repair validation failure. An
  ordinary valid semantic-review REJECT decision is not a protocol failure.
* `invalid_output` in each retained call row: the corresponding boolean.

This distinction is intentional: a provider response with invalid JSON may have
both `failed=true` and `invalid_output=true`, while a timeout/network/HTTP
failure has `failed=true` and `invalid_output=false`. A text response that reaches
our parser and then fails schema/evidence validation has `failed=false` and
`invalid_output=true`. This keeps transport failures, provider response failures,
and local semantic/schema rejections separately observable.

Counters are associated with the exact call index and protected by the metrics
lock; they remain correct when the bounded call-detail list has reached its
limit. Arbitrary backend exceptions do not increment `invalid_output_count`.
Only the sanitized `model_invalid_response` code is treated as proof that a
response was received but unusable. Direct `_complete` callers that do no
additional parser validation do not invent a parser outcome.

`gate_primary`, `gate_format_repair`, `gate_semantic_retry`, and
`gate_coverage_repair` survive durable-job projection, aggregation, and MCP
`process_status`. All other existing stage/operation names remain compatible.

Old stored attempts need not contain these additive fields; absence is not
proof of zero historical validation failures. Aggregation sums only available
counters, and does not backfill or reinterpret historical `failed_calls`.

The invalid-response follow-up adds focused regressions for three distinct
paths: a non-text normal backend return, a backend-raised
`model_invalid_response`, and an ordinary transport exception. The first two are
both failed + invalid output; the transport exception is failed only. This
follow-up changes metrics classification only; it does not change retry count,
model prompts, response parsing rules, or commit semantics.

## Structural diagnostics

Model-generated field names are untrusted content, even when they look like
ASCII identifiers. Only the program-defined diagnostic label `explanation`
may be named in `coverage_unexpected_fields`. Other unknown names are omitted,
while `coverage_unexpected_field_count` includes them. No field value is logged
by this shape diagnostic. Existing row index, JSON type, and fixed allowed-field
metadata remain available. This is content minimization, not a claim of
complete anonymity for every existing audit metadata field.

## Lossless shape repair

Both responses pass the existing strict JSON parser first, rejecting duplicate
keys and non-finite constants. Decimal decoding in a second pass avoids binary
float rounding collisions. Both inputs are bounded by the existing 64 KiB
repair size budget, and unsupported extreme decimal exponents fail closed.

Comparison preserves scalar types, every list order, and exact Unicode string
values. JSON object key order and equivalent escape spelling may vary. Integer
and decimal representations are distinct; equivalent decimal value spellings
such as `1e0` and `1.0` are allowed. This is exact parsed-value comparison, not
byte-for-byte serialization comparison or Unicode normalization.

Only unknown coverage fields may be deleted. Candidates, bindings, and every
existing allowed coverage field must retain their values and types. The repaired
response still passes the original Gate parser against the original evidence.
A rejected repair falls back within the existing bounded full-Gate retry flow.

## Validation

Run after installing console entry points in an isolated environment:

```sh
python -m unittest tests.test_gate_protocol_matrix_v041 -v
python -m unittest tests.test_model_invalid_response_metrics_v041 -v
python -m unittest tests.test_audit_followup_v041 -v
python -m unittest discover -s tests -p 'test_*.py' -v
python -m compileall -q src tests examples
```

The targeted suite includes the executable decision/claim matrix, independent
parser rejection tests, response-vs-transport metrics classification, real
diagnostic file writes, partial-failure recovery, concurrent metric attribution,
bounded metric retention, and durable failed/rerun/success attempts read through
MCP. No test substitutes a real model-quality or cross-platform CI result.
