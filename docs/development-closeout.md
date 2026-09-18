# Development candidate and final-test boundary

This document is the current engineering scope for the v4.1 design closeout.
Historical phase documents remain provenance, not additional online prompts.
The accompanying candidate manifest, test inventory and requirements ledger bind
implementation to its exact local tree/commit; this file alone is not a passing
CI, semantic or production acceptance certificate. Version remains 0.2.65.

## Supported entry points

| Entry | Authority and state | Recovery / side effects |
|---|---|---|
| process / worker / CLI / MCP incremental | Trusted captured source revision, fixed scopes and durable work | One normal request; total at most two, frozen response/commit recovery uses zero new requests |
| remember / selected retention incremental | Real explicit intent and selected sources, not global source-terminal reuse | Same-request replay, new actual intent distinct, no bypass of target/scope checks |
| preview_incremental / apply_incremental | Bounded complete current snapshot; explicit selected proposed output | Same target group atomic intent; full revision and source/scope revalidation |
| update_memory (Python) | Exact whitelist patch, expected_revision and old/new authorized_scopes | Frozen explicit journal, shared history/head writer; never upsert |
| retract_memory (Python) | Exact identity/revision and explicit withdrawal | Versioned existing journal; current invalid head, history preserved |
| create_memory / raw overwrite=False | Provably absent logical identity and free destination | No replacement; errors cannot be mistaken for replay receipts |
| raw write/save/add overwrite=True | Trusted whole-document administrative replacement | Deliberately no normal CAS/history guarantee; no new MCP tool |
| compact | Separate explicit single-ID body maintenance | Protected fields immutable; existing rollback journal retained |
| retention / maintain-state | Explicit maintenance or existing bounded mechanical path | Pending dependency protection; no business-identity deletion or destructive TTL |
| forget / forget_about | Exact confirmed logical target; fuzzy candidate is not confirmation | Cancel pending plaintext and old source replay before deleting authorized memory copies |
| search / read / list_todos | Shared committed Markdown, query integrity and pending freshness | No model dispatch; read statistics are not write revisions |
| migration_preflight / migration_backup | Operator-declared stopped writers; private full snapshot | Verify only; no automatic production switch/restore or owner rebinding |

## Closed engineering gaps

The cumulative candidate retains Provider late-rejection feedback, create-only
identity checks and strict revision/retraction recovery from previous batches.
The final closeout adds exact structured edits, shared writer target rechecks,
case-insensitive Forget cancellation, confirmed deletion boundaries, journal
inventory/freshness/retention protection, missing-authority invariants, persistent
new-Vault binding and bounded/provenance-aware planner snapshots. All are local
contracts; no customer-specific extraction rule, extra semantic pass, database,
daemon, vector service, new host adapter or new dependency is introduced.

## Explicit first-scope choices, not silently unfinished enabled features

General cross-Vault merge/import, multi-ID semantic merge/redirect creation,
automatic native-memory contradiction resolution, cross-machine synchronization,
full historical semantic repair and new multi-instance host routing are not
first-scope enabled features. Trusted raw import is explicit replacement only.
Old retired records without reliable unique current ownership stay history-only.
No arbitrary old record is resurrected by mtime. Full TTL deletion of authority
is disabled; finite retained receipts and backpressure are the supported policy.
Large-library cache/heat optimizations remain measurement-led, not prerequisites
that justify a new store. Legacy regular paths are removed only after actual
acceptance and authorized cutover; legacy pending recovery remains supported.

## What final testing must still establish

1. Place the exact accumulated candidate in the authorized writable repository;
   obtain the same-commit existing five native installed-artifact CI cells.
   An older green main, Linux-only runs or a local commit do not substitute.
2. Freeze the user's real Flash provider/endpoint/model configuration, disabled
   thinking request settings, permitted synthetic/private holdout suites and
   total request allowance. Run existing acceptance tooling, report all failures
   by category, real usage/latency and independent semantic review. No stronger
   substitute model and no leaked expected answers in model input.
3. On actual supported Hermes and Codex hosts, verify real executable and copied
   Provider, source_time/final flow, one Vault, native-memory sharing, process
   restart/reconnect, stopped-recording late-final behavior and legacy compatibility.
4. Exercise cold upgrade and rollback choices with actual known writer processes,
   current backups and latest Forget boundaries. Keep production operations and
   publishing separately authorized.

The requirements ledger covers all FS001–FS200 and E01–E20 by ID. Its source and
test links are engineering traceability, not 220 independent passing tests.
Real semantic, native-host, private-data and performance rows remain explicit
final-test gates. Checksums and receipts prove byte consistency, not semantic
truth, multi-user authorization, an atomic multi-file transaction or protection
from arbitrary uncooperative filesystem writers.
