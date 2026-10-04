# Hermes host contract v1

This change spans Memleaf and the Hermes host. `host-contract.patch` is the
reviewable Hermes-side patch, based on Hermes commit `36dfe5858a`; it does not
modify plugins, credentials, configuration, or a Vault. It adds generic host
extension points with Memleaf as their current consumer.

Apply only to a compatible checkout after `git apply --check`. Future Hermes
updates may require rebasing the patch. Restart a host to load changed Python
code; identical on-disk files do not prove a running process loaded them.

## Current retention integration

The patch below documents the trusted external-dispatch extension. Deployed
Hermes versions may not invoke that optional hook. Memleaf 0.2.87 therefore uses
its native `memleaf_remember` tool for explicit retention: it binds and queues
an intent, then retains original captured user events when the turn completes.
The installer excludes standalone MCP `remember` from Hermes' model tool surface.
This route does not depend on the optional external-dispatch hook; bare MCP and
Python clients retain their existing APIs. Restart Hermes after installation.

The bound native route also handles conversational corrections, completion,
cancellation, reopening and withdrawal. A successful full-turn retention receipt
settles automatic extraction without another model request. Partial selections
remain separate. MCP lifecycle updates do not accept model-supplied source times;
trusted Python callers keep their explicit metadata API. Restart Hermes after
upgrading to load the current Provider.

## Boundaries

- The host records a stable turn ID and each visible user's durable message UID.
  The clean body and message metadata are snapshotted at admission, before
  API-only prefixes or multimodal memory/plugin injections. A correction stays
  a separate user event. The manager structurally copies
  messages and the context before queueing background sync.
- The Provider verifies enumerated UID/role/order membership, then captures
  the admission snapshots and the final assistant.
  A completed context is bound to the final real reply UID while the turn is
  still active, using the finalizer's independently saved turn identity. Its
  admission events survive lossy API compaction. Missing provenance or mismatched
  completion is rejected. Legacy composite input remains
  unknown; independently verified assistant time can survive a user mismatch.
- `source_time` comes only from that message. There is no processing clock or
  database repair in extraction. Missing event sequence remains unknown instead
  of allocating `turn * 2` positions to a variable-size turn. List order, shared
  turn ID and the declared final assistant establish the local turn boundary.
- A trusted dispatch context binds existing MCP search/read/list_todos and
  scope_catalog identity. `remember` additionally receives the real originating
  turn and a repeatable operation intent. Model identity arguments are replaced
  at that boundary; concurrent batches freeze identity before worker dispatch,
  and a bare/stale Core token remains strictly rejected.
- No duplicate tool surface is introduced. Managed models receive business-only
  guidance. MCP `read` has a transport-neutral schema; its runtime still requires
  the token supplied by the host or by a bare client.
- Explicit writes become validated comparison context, with the saved target
  prioritized. They do not cover every new event, authorize similar-title merges,
  skip independent facts, or loosen calendar validation. Snapshot and prepared
  commit recovery bind the source receipt and target revisions. Same-turn
  compression rotation follows only verified source-scoped child-to-ancestor
  lineage and stable host turn receipts.
