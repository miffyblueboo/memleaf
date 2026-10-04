# Hermes source and comparison context

## Message times

`source_time` comes from the corresponding message's `source_time` or
`timestamp`. Missing or invalid values stay unknown. Capturing or processing
an event does not create a source time.

The legacy callback checks the final assistant and the nearest user. It has no
32-message lookback limit. It does not borrow a matching user from an older turn.

Some Hermes versions persist a reply before adding their file-mutation verifier
footer. The Provider accepts that display transformation only when the installed
Hermes formatter reconstructs the entire footer from uniquely associated,
current-turn file-tool receipts and the full resulting reply matches. Arbitrary
prefixes, suffixes, unsupported formatters and ambiguous receipts remain unknown.
Tool receipts supply no times or memory facts. The displayed reply remains the
captured assistant content.

For legacy corrections, the Provider verifies the real user rows, native replay
scaffold and the exact full merged callback string. It never splits arbitrary
user text by a separator. A bounded chain must have a queued original admission;
then each source row is captured separately with its own time. It does not assign
the final correction's time to the combined user text. A repeated source delivery
is recognized before consuming another same-text admission. Host-v1 admission
snapshots remain the preferred interface for compaction and transformations that
legacy callbacks cannot verify. The list is copied at callback invocation; this
cannot recover a host snapshot already lost before the callback arrives.

## Successful reads as comparison targets

Hermes uses the retrieval identity frozen at `on_turn_start` for final capture.
The assistant capture includes `retrieval_id` and `retrieval_turn_id`; the latter
must equal its `turn_id`. Core checks that the existing gate belongs to exactly
the declared source, session and turn. It never resolves a session's latest gate
on behalf of an older callback. Live search and read still reject stale tokens.

Core freezes only successfully read memory IDs into `comparison_context` on the
assistant event, including the immutable capture receipt and payload digest.
It accepts no caller-supplied list of IDs through this interface. The capsule is
comparison metadata, not factual evidence, and its token is not projected into
the model request. Automatic preparation gives these required targets priority
alongside explicit-write receipts. There is no 20-ID truncation or target-count
ceiling: required targets expand the preferred candidate count; optional recall
only fills remaining slots. A missing target or an oversized request still blocks
preparation instead of silently dropping required context. The existing request
byte budget remains; no model call is added to capture. Legacy capsules already
marked overflow remain incomplete and cannot be silently reconstructed from a
later live gate.

A delayed callback can use its own unexpired gate even after a later turn starts.
Once the capsule is captured, gate expiry cannot erase it or break a duplicate
capture. If Inbox was saved but receipt persistence failed, retries first recover
the capsule from Inbox before consulting a live gate. An uncaptured, expired or
mismatched gate cannot supply comparison context.

The extraction prompt also requires structured deadline maintenance before
NO_CHANGE when current evidence makes an existing unstructured deadline
verifiable. Unchanged wording does not mean a missing due_date is complete.
This rule advances the semantic protocol to v11.

This supplies the model with the old targets. It does not force semantic UPDATE,
merge similar titles, or automatically alter historical memories. Recovery of
existing production runs and deleted records is a separate operation.
