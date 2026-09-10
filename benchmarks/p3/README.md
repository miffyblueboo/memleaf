# P3 bounded batching evidence

This directory records zero-real-model structural evidence for P3. The product changes are experimental and remain layered on top of the P2 prompt/input-slimming candidate.

## Safety boundary

Batching changes transport granularity, not the per-item semantic contracts.

- Automatic CREATE summaries may batch in pairs only when the selected backend explicitly exposes `structured_batch_safe=true`.
- UPDATE summaries remain on the existing keyed single-item scheduler so same-target ordering is unchanged.
- Final semantic reviews may batch up to four independent rows, again only for a `structured_batch_safe` backend.
- Host/custom callback paths without that explicit capability continue to use the original single-item calls.
- Batch results are associated by stable item/review IDs and each associated result is validated by the existing single-item parser.
- One invalid associated row falls back only that row to the original single-item path.
- A malformed or un-associable structured batch output may fall back to the corresponding legacy singles.
- A transport/provider `ModelError` does **not** fan out into legacy singles. It propagates through the existing fail-closed path, preventing one failed batch from becoming `1 + N` additional model calls.

The independent final semantic review remains present. P3 does not implement conditional review removal.

## Structural call-count evidence

`batch_call_audit.py` measures the CREATE-summary batching shape with a synthetic fake executor. Four independent CREATE summaries change from four model calls to two, a 50% reduction. Its static system+user UTF-8 byte proxy changes from 20,839 bytes to 13,837 bytes, a 33.6005% reduction.

`review_call_audit.py` measures final semantic-review batching with a synthetic fake executor. The structural review call counts are:

- 1 review: 1 -> 1
- 2 reviews: 2 -> 1
- 4 reviews: 4 -> 1
- 5 reviews: 5 -> 2
- 8 reviews: 8 -> 2

Thus four independent CREATE candidates that reach both post-Gate stages change structurally from eight calls (four summaries plus four reviews) to three calls (two summary batches plus one review batch), a derived 62.5% reduction for those two stages. This is not a real-model token, latency, or semantic-quality claim.

## Scope intentionally not changed

P3 does not change Gate admission/evidence/coverage semantics, UPDATE ordering, writer commit semantics, the default `thinking=low` setting, or the requirement for final semantic review. API pricing is not an acceptance blocker; the active optimization targets are repeated token/input overhead, model-call count, failure amplification, and unnecessary reasoning work.
