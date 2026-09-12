# Extraction latency and timeout policy

## Performance target versus failure timeout

The automatic extraction objective is to **successfully finish ordinary turns
within 10 seconds**, without lowering memory quality or discarding valid work.
Ten seconds is not a cancellation deadline or a condition for permission to
write. Missing that objective is a performance miss, not proof that extraction
failed or that there was nothing to remember.

The fixed 6-second primary cap, 8-second shared model window, and 10-second
pre-commit veto introduced in 0.2.42 were the wrong implementation of this
objective. They could abort a valid request before any model output despite an
explicit `llm.request_timeout: 120` configuration.

Automatic B3 extraction now leaves timeout handling to the configured transport:

```yaml
llm:
  request_timeout: 120
  thinking:
    single_pass: disabled
```

- `request_timeout` is passed to the HTTP transport for each actual request.
  It is not silently shortened to meet the performance objective. An injected
  Python/host callback owns its own cancellation behavior; memleaf cannot
  manufacture a transport timeout for caller-owned code.
- One successful structured model call is the normal path. A format/structure
  failure may use one repair call. Fixed routes still reserve at most two
  outbound requests per logical work item and turn, including worker restarts;
  the repair uses the configured transport timeout, not the remainder of an
  eight-second window. No hidden host-to-API fallback is introduced.
- A valid response taking more than 10 seconds still goes through the existing
  evidence, target/revision, permission and commit checks. Duration alone does
  not invalidate it. Actual transport timeouts, invalid output, conflicts and
  write failures still report failure or deferral and retain unprocessed inbox
  data; they are never relabeled as `NO_CHANGE` or success.
- A slow or restarted worker does not invalidate a frozen plan merely because
  it is old. Forward recovery still uses the durable plan/Markdown/journal
  checks and does not need another model call. Legacy request-counter and
  timestamp ledger formats remain readable. The timestamp is informational;
  it neither expires a turn nor resets consumed request attempts.

## What continues to reduce work

The B3 single-pass planner, non-thinking extraction default, bounded comparison
context, structured output, per-turn durable commits, and separation of
compaction from ordinary extraction remain in place. Removing an artificial
cutoff restores correctness; it does **not** by itself speed up the provider.

Use the existing `model_metrics` per-call/stage durations, request/retry counts
and token counters, plus background job start/completion timestamps, to inspect
slow work. These contain structural measurements rather than conversation
bodies or credentials. A quick failure must never count as a successful
sub-10-second extraction. Real-provider latency and quality acceptance remain
separate from deterministic regressions; no P50/P95 result is claimed here.

## Existing failed captures

Upgrade the core before retrying. A turn that previously failed at the artificial
six-second cap still needs an explicitly requested processing attempt against
the **same Vault and source/session**. Upgrading does not automatically replay
failed jobs or claim their memories have been created. Do not delete the inbox,
processed journal, or budget ledger to force a retry. A normal retry reuses the
existing Core idempotency/recovery rules; a genuinely exhausted work item must
not obtain extra requests by resetting its counter.
