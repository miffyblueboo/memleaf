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

## Metrics contract

`failed_calls` keeps its existing backend failure meaning. It is not a measure
of whether a received response passed schema/evidence validation.

New additive fields:

* `invalid_output_count` in total, stage, and operation buckets: a text response
  received from the backend but rejected by `ModelOutputError` during JSON,
  schema/evidence, or lossless-repair validation. Count once per response,
  including a swallowed coverage-repair validation failure. An ordinary valid
  semantic-review REJECT decision is not a protocol failure.
* `invalid_output` in each retained call row: the corresponding boolean.

Counters are associated with the exact call index and protected by the metrics
lock; they remain correct when the bounded call-detail list has reached its
limit. Backend exceptions do not increment the new counter. Direct `_complete`
callers that do no output validation do not invent a validation outcome.

`gate_primary`, `gate_format_repair`, `gate_semantic_retry`, and
`gate_coverage_repair` survive durable-job projection, aggregation, and MCP
`process_status`. All other existing stage/operation names remain compatible.

Old stored attempts need not contain these additive fields; absence is not
proof of zero historical validation failures. Aggregation sums only available
counters, and does not backfill or reinterpret historical `failed_calls`.

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
python -m unittest tests.test_audit_followup_v041 -v
python -m unittest discover -s tests -p 'test_*.py' -v
python -m compileall -q src tests examples
```

The targeted suite includes independent parser rejection tests, real diagnostic
file writes, partial-failure recovery, concurrent metric attribution, bounded
metric retention, and durable failed/rerun/success attempts read through MCP.
No test substitutes a real model-quality or cross-platform CI result.
