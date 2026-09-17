# Public query integrity and progress (G4c)

This increment improves existing `scope_catalog`, `search_candidates`,
`read`/`read_page`, and `list_todos` boundaries. It does not activate the new
extraction routes, change prompts/model parameters, collect state, or publish a
release. Query results are observations, not semantic quality certificates.

## Separate result, scan and pipeline dimensions

The dictionary APIs retain their existing bodies, candidate-only directories and
paging fields. They add the following bounded metadata:

```json
{
  "knowledge_generation": "<64-character content fingerprint>",
  "scan_status": {
    "status": "complete",
    "issue_count": 0,
    "codes": {},
    "areas": ["knowledge"]
  },
  "pipeline_status": {
    "status": "pending",
    "scope": "vault",
    "pending_turns": 1,
    "incomplete_turns": 0,
    "pending_commits": 0,
    "unresolved_runs": 0,
    "queued_jobs": 0
  }
}
```

`found`/`no_match` is still about candidates; scan errors do not change a valid
FOUND search into a gate-level failure or authorize reading a non-candidate.
A complete scan means the declared local area and scope were inspected without
known errors. It is not recall@K, model correctness, or proof that the host has
sent every recent message. `pipeline_status` is Vault-wide because pending text
has not necessarily been assigned to a project. Its counts describe different,
potentially overlapping categories; do not sum them as unique business tasks.

`current` means no pending eligible work was found in the available validated
inbox/retained controls/queue observation. `pending` includes retained unresolved
work. `unknown` is used for damaged/unavailable controls, uninterpretable source
blocks, resource bounds, or detected changes during observation. It must not be
reported as zero pending work. No source text, memory body, filesystem paths or
raw exceptions are included in these metadata fields.

`knowledge_generation` is a content fingerprint, not a monotonic counter. It
covers the scanned local area, including error/conflict fingerprints, excluding
read accounting. Renames and changes in unrelated scanned scopes can invalidate
cursors conservatively. It is not a bearer permission or write revision.

## Current Markdown and conflict handling

A bounded read-only scanner supplies current records and an ephemeral tags index
to the public candidate API. Missing, corrupt or stale disk indexes do not cause
an empty answer or force an index rebuild in this path. The old relevance
predicates and candidate shape (`memory_id`, `title`) are retained. Scope and
candidate pages still fit their 2,000/4,000-character budgets, including the new
envelope. A page can therefore contain fewer items than before.

Bad files are isolated and counted. Diagnostic scope is narrowed only when its
frontmatter can be parsed and its scope independently validated. Unknown scope
is never guessed from a directory/title/customer word, so it affects all scoped
completeness claims. Independently known foreign-scope errors do not make a
precise local read fail. Error codes are bounded; offending prose is not echoed.

All claimants of a duplicate (case-insensitive) ID are quarantined, including a
malformed file with an independently identifiable ID. Exact read raises
`memory_id_conflict`; it does not select the first file or fall back to native or
history. Legacy search also stops choosing an ambiguous ID, while retaining its
list return type. This query quarantine does not hide files from explicit Forget
or repair tools. History/current records that improperly reuse an exact ID are
ambiguous too. Linked history with its own proper ID remains readable.

Scan limits are 20,000 Markdown paths per area, 8 MiB per file and 128 MiB read
content per snapshot. Links/nonregular files and enumeration/read failures are
reported, not followed or silently treated as empty. Oversized input leaves a
partial scan; it is never shortened into a full record. These limits are resource
bounds, not product sizing or performance guarantees.

A second bounded content scan detects changes before returning a public page;
`scan_changed` asks the caller to restart, without looping or writing. External
editors do not honor the Vault lock, so this is not an atomic filesystem-wide
snapshot and cannot detect arbitrary changes after the final check. The response
represents the validated observation, not permanent freshness.

Local query methods no longer trigger compaction recovery. Mutating entry points
retain their existing recovery boundary. The automatic legacy context interface
and native public-read/index behavior are not globally redesigned in this batch.

## Read pages and accounting

`offset == total_chars` is a valid empty last page, including empty retracted
heads when explicitly requested. `offset > total_chars` raises `invalid_offset`
consistently before accounting. Version mismatch also performs no hit write.
First-page hit accounting is retained for fully specified local records, but an
I/O failure returns the verified body with `read_accounting=unavailable`. History,
native and incomplete legacy metadata views are not rewritten for hit counting.
Unknown legacy creation/update times stay unknown in the transient query view;
querying does not manufacture and persist timestamps. Such old records may need
explicit normalization before a stable external write-revision workflow.

`read` keeps its `Memory | None` compatibility return type. `read_page(None)`/no
match remains `None`; completeness-aware discovery uses the dictionary query
APIs. MCP validates and preserves the new small envelope without forwarding
arbitrary nested controls or expanding candidate entries to bodies.

## Complete Todo enumeration and calendar

The existing Todo enumeration no longer applies the same-title relevance overlay:
a local and inherited task with the same title can be independent obligations.
Current retracted heads also prevent old retired Todo histories from masquerading
as current records. Retired compatibility remains read-only, not automatic history
migration or a general resolution of contradictory old versions.

Optional `as_of` (YYYY-MM-DD) and `timezone` (IANA name) pin a Todo query's calendar.
Without either, the first page uses an explicitly reported UTC day, not the
machine's hidden local timezone. The caller should supply its trusted user/Vault
timezone. UTC is always available; an unavailable IANA database/name fails
explicitly, never silently changes the zone or adds a runtime dependency.
The first-page clock is embedded in the cursor and reused by subsequent pages,
even across midnight. Changing it with an existing cursor is rejected. Old Todo
cursors without a clock require restarting. Query clocks are not capabilities.

`date_counts` reports unscheduled versus unresolved calendar constraints for the
status/scope-matching records before date exclusion. It still reports those
counts when `include_unscheduled=false` yields no matching date rows. The source
phrase remains available in `read_page.due_text`. Date bounds stay inclusive;
overdueness is derived and never writes completed/cancelled status.

## State retention preflight, not unsafe garbage collection

Existing processing health now includes `retention_inventory`: validated run and
commit counts, remaining run slots, per-status counts, live ownership and pending
commit count. It explicitly returns `collection_authorized=false`. Reading it
never releases a request budget, expires a lease, removes a cancelled suppressor,
replays a source or starts a job. Reading malformed state remains an explicit
health error; normal local factual queries report progress unknown and continue.

Time-window deletion of control decisions is **not implemented**. The explicit
`maintain-state` path now provides lossless completed-receipt compaction, documented
in `runtime-state-retention.md`; observing this inventory still never invokes it. A terminal/cancelled
row can still suppress replay, and a partial parent may be needed by cumulative
recovery. Deleting the oldest 128 records would reopen budgets or resurrect old
inputs. The next lifecycle change needs a coherent retention-window/tombstone
contract and fault-tested dependency release, not a list of terminal IDs called
safe-to-delete. Current capacity backpressure is preserved.

## Verification boundary

Deterministic tests cover 49 valid plus one bad file, duplicate IDs, inherited
same-title tasks, invalid UTF-8, symlinks and limits, current-index independence,
read-only progress, corrupted controls/queue, source revisions, partial results,
selected retention versus whole-turn processing, pagination budgets and midnight
clock continuity. Existing tests are retained. No live Flash, production Vault,
Windows/macOS native host, full state GC or default production switch is implied.
