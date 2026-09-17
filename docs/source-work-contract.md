# Source event and extraction-work contract

Memleaf separates three kinds of identity and two kinds of time. These fields
are control data; message content remains untrusted evidence.

## Capture envelope

- `event_id` is a delivery receipt identity retained for compatibility.
- `message_id` is the host's stable message identity. `message_revision`
  identifies one immutable version of that message. A replacement supplies
  `previous_message_revision` matching the current version; stale or forked
  revision callbacks fail closed instead of winning by arrival order.
- `source_sequence` is the host order. `turn_index` remains only Memleaf's
  local inbox order. `previous_message_id` can preserve an explicit host link.
- `source_time` is the original message time and must include a timezone.
  `captured_at` is the local durable-capture time. Missing `source_time` stays
  unknown and is never replaced by `captured_at` for date normalization.
- `final` is a trusted adapter boundary. A turn using this contract is
  processable only when exactly one assistant event declares `final=true` and
  it is the last ordered event. Legacy inbox blocks without the field retain
  the older one-assistant compatibility rule.

A repeated `message_id` + `message_revision` is idempotent. A different
revision supersedes the older visible message, fences pending work produced
from the old revision, and schedules any previously affected memory IDs as
priority coordination targets. Revision labels are opaque: arrival does not
infer a business timestamp or compare revision strings lexically.

## Extraction work identity

Model-request work is keyed from request kind, trusted intent, Vault source,
session, turn, and the complete set of event/message revisions. It is not
keyed from synchronous versus background transport or from a transient job ID.

Automatic processing uses the `automatic` intent for a frozen source-revision
set. Explicit `remember` accepts an `intent_id`; retries of one authorization
must reuse it, while a genuinely new authorization uses a new ID. If omitted,
Core creates a new intent and returns it in the result.

The durable request ledger reserves before dispatch. A completed row remains
terminal instead of being deleted and silently reopened. When the bounded
ledger is full, only completed work may be evicted; active or recoverable work
is never removed merely because it is oldest. Legacy counters above the
current limit are accepted as exhausted, not treated as corruption or reset.

## Completion time

`status=completed` does not imply a known `completed_at`. A source/message time
proves when completion was reported, not necessarily when it occurred.
`completed_at` is retained only when evidence explicitly establishes the
actual completion time or an existing trusted value is preserved.
