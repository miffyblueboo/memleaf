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
Core derives a stable intent from a supplied legacy event/turn receipt, or
creates a new intent when neither is supplied, and returns it in the result.
Same-intent retries must preserve their content and scope. Distinct explicit
intents may share a host turn or delivery event without sharing a frozen plan.

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


## Retry and cleanup boundaries

Capture receipts bind content, role, source time/order, predecessor, revision
and final metadata. Changing any of them requires a new message revision;
a transport retry cannot silently change them. A small payload fingerprint and
revision lineage survive inbox body cleanup. Revision labels are opaque.
New source envelopes without a trusted assistant final signal are not complete;
legacy callers without source metadata retain their existing completion rule.
The first arrival of a predecessor after its reply is not a revision.

Inbox append is the durable source record. After an interruption between append
and receipt save, capture or process reconciles the missing receipts and fences
old work before processing or cleanup. Commit also verifies the actual current
inbox snapshot under the Vault lock. Cleanup uses the settled event revisions,
not every block sharing a turn ID; unresolved source revisions are retained.
Source revisions retain known committed and pending-operation target IDs for
later coordination. This does not claim the legacy model protocol can already
retract every assertion removed by a source edit.

Explicit remember persists its original observation time and payload binding.
A retry never rebases a relative date to its retry time. Old explicit work whose
receipt no longer proves these values requires explicit migration or a genuinely
new authorization; the system does not invent evidence to bypass this boundary.

A successful partial commit is not terminal authorization. It may use only the
remaining durable request allowance, subject to the existing finite retry policy.
Unchanged legacy automatic events migrate matching job-keyed counters once when
first reserved under their source-work key; explicit new intent is not charged
against an old automatic decision. Completed work remains terminal while its
receipt is retained. Ledger retention does not promise unlimited historical replay.

Frozen plans now tag their input digest format. An unversioned pre-source digest
is accepted only when the actual events contain no new source metadata; known
source time, revision or final cannot be discarded to make an old digest match.
This is separate from target-memory revision compatibility.

## Staged integration, not full redesign completion

These changes do not replace the legacy extraction/maintenance model protocol.
The current automatic path retains its existing three-dispatch ceiling; explicit
remember retains two. The planned normal-one-plus-one-recovery budget belongs to
the later unified planner and must not be claimed here. No extra dispatches were
introduced by these fixes.

Core can accept original message timestamps, but an adapter must actually provide
them to obtain that guarantee. Missing Hermes source timestamps remain unknown;
local capture time is not an alternative source timestamp. Real model semantics
and Windows/macOS host acceptance require separate validation.

## Follow-up: ordered windows, ambiguous migration and host metadata

For one source/session, an available window with complete, unique source
positions and non-overlapping turn ranges is processed in source order. Local
`turn_index` is never rewritten. If an earlier known turn is incomplete, a later
turn in that ordered window does not overtake it. Unknown or overlapping source
positions retain the compatibility ordering: this is not a global event clock
or a promise to order unseen messages. The local progress watermark advances
only through consecutive settled indices; a source-ordered higher index cannot
cause a capped batch or crash recovery to skip a lower pending index.

A legacy job counter matching the current local turn must be considered before
reserving a new automatic request. For proven legacy events, consumption is
merged exactly once, including when a source-work row already exists. If new
source metadata prevents establishing which revision the legacy counter belongs
to, reservation fails with a migration cause; neither a new allowance nor a
semantic NO_MEMORY decision is invented. A genuinely new explicit remember
intent remains independent. This conservative case needs explicit migration
or new user authorization, not automatic counter reset.

Hermes' public `sync_turn` API supplies visible strings and an OpenAI-style
message list, but does not guarantee original timestamps or revision metadata.
When present, Memleaf copies an allowlist from the exact visible tail pair only:
`message_id` (or `id`), `message_revision`, `previous_message_revision`,
`previous_message_id`, `source_sequence`, and timezone-aware `source_time`
(or `timestamp`). Both host message IDs must be available to use host identity;
otherwise the compatibility role IDs remain. Host sequence and synthetic turn
positions are not mixed within a pair. Stable host user identity also binds the
capture turn so body/final revisions do not create an unrelated turn. Raw tool,
attachment, system and developer contents remain excluded. Missing metadata is
unknown, never inferred from text or capture time. Hosts omitting revision
metadata still cannot claim native edit support, and native Hermes acceptance
remains a separate test from these adapter contract tests.

The legacy summary prompt now agrees with the completion-time contract above:
only evidence of actual completion time (or a trusted existing value) supplies
`completed_at`. The report's observation timestamp alone is not enough. This
changes no normal model-call count and does not switch to the future items
planner.
