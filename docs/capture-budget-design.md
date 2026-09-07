# Tool evidence capture budget

## Problem and ownership

Capture permissions and capture capacity are separate contracts. `metadata`
deliberately removes source bodies; `off` removes observations. Neither setting
can produce new external facts for extraction. Enabling `bounded` authorizes
bounded body retention, but does not reconstruct previously discarded data.

The previous implementation also applied independent eight-record and
2,000-character limits in the Hermes adapter, provenance normalization and
pending-state processing. Early discovery and skill results could consume the
entire record allowance before later source results arrived. Larger plain-text
results were truncated before Core could account for their complete content.
This is an ingestion-capacity problem; changing Gate semantics cannot recover
those bytes.

## Required implementation contract

- Keep one shared, deterministic budget implementation for Core and the copied
  Hermes provider. The provider must continue to work without importing Core
  from the host's Python environment.
- Bound record count, each body and aggregate body size. Publish the limits as
  engineering capacity, not a guarantee of complete capture for arbitrary turns.
  The implementation target is 64 source records, 32 KiB of UTF-8 text per body
  and 128 KiB of total body text. Omission markers have a separate allowance of
  64 call identities and one aggregate marker. They do not consume source-record
  capacity or count themselves as newly lost observations on a later normalization
  pass. Marker bodies are fixed diagnostics, never retained source excerpts.
- Preserve complete, matched source results within those limits. Do not rank
  business topics or tool names, infer facts from assistant text, split arbitrary
  stdout into invented document records, or promote truncated prefixes to
  complete observations.
- Normalize retained evidence idempotently. Cache, capture, inbox read and
  planning must not each discard another part of an already bounded inventory.
- Keep explicit omission/incompleteness accounting when a limit is exceeded.
  Policy-authorized but incomplete observations remain unresolved and cannot
  authorize a write or successful source cleanup.
- Apply `metadata`, `off`, attachment exclusions and redaction before persistent
  writes. A capacity increase must not silently change capture permission.
- Keep the existing Gate/Summarize semantic responsibility and Markdown storage
  model. Larger source capacity is not a claim of successful model extraction.

## Acceptance

Use synthetic host messages shaped like an ordinary discovery/read turn: early
tool discovery, skills and memory search followed by nine later results, with
individual bodies ranging from hundreds to tens of thousands of characters.
The old path loses all nine later results; the revised path must retain complete
results when the full input is within its declared capacity.

Verify the same evidence across adapter, pending cache, capture and inbox read;
then verify a supported later-source candidate can pass the actual processing
path in an isolated Vault. Check record/body/aggregate overflow separately,
including repeated reads, accurate loss accounting, no write from incomplete
evidence, and no cleanup of unresolved turns. Retain permission, redaction,
cross-turn isolation and standalone installation coverage.

Tests must use synthetic data and a deterministic backend. Live model semantics,
changes to a user's capture permission, installation and release are separate
operations with separately reported results.

## Model capacity remains a separate limit

The capture limits bound retained source data, not the final prompt or model
token count. Evidence annotations, conversation context, retrieved context and
the output allowance also consume model capacity. The current `llm.context_window`
setting is not a pre-call tokenizer check. A source inventory within the capture
budget can still exceed a selected model's usable context.

Do not silently drop evidence to make that call succeed. Existing model-failure
handling must preserve the failed turn and prevent cleanup. Automatic batching
or model-specific token accounting needs its own design and is outside this
capture-budget change.
