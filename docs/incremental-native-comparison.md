# Incremental native comparison (G3d, opt-in)

This increment connects configured native notes to the existing incremental
preview, runner, compiler and frozen commit/recovery. It adds no new model stage,
writer, background service or native-note mutation. The normal system prompt,
model settings, default process/remember/Hermes routes and package version stay
unchanged. This is not live-model semantic acceptance or default activation.

## Reading and candidate selection

The source Agent is supplied by the trusted capture/caller boundary. Its own
native sources may be read even when `share=false`; another Agent's sources
require `share=true`. Disabled or nonshared foreign sources are excluded before
opening their content. This reuses the existing NativeIndexer sharing policy;
it is not a new identity or multi-tenant authorization system.

Configured Markdown/text files use the existing source, heading/chunk and
`native-...` fragment identity rules. The helper reads each eligible file once
per snapshot/guard pass; it does not refresh the native index or change any
shadow map. Candidate selection shares the existing total limit (default 12,
maximum 20) across local/native lanes and message queries. Explicit required IDs
are pinned. Top-K is not exhaustive semantic deduplication. A required fragment
that cannot fit is blocked, never silently shortened and then treated as complete.

Bounded staging limits: at most 64 configured sources, 5 MiB per native file,
8 MiB total eligible native bytes, and 4096 native fragments. These are local
resource limits, not the model's context limit. The existing 128 KiB full request
limit still includes the system prompt, evidence, selected targets and retry
suffix. Unavailable eligible files, invalid UTF-8, nonregular paths, unstable
reads or limits produce explicit errors before a model dispatch; they are not
`NO_MEMORY` or a successful no-match. Ordinary local read/search stays available.

Only selected native bodies reach this explicitly requested planner. They are
not added a second time to the Agent's public conversation injection. The model
uses existing `mN` references with `writable=false`, plus `native=true` and the
owning Agent name. Filesystem paths, real native IDs, configuration fingerprints
and revision tokens stay outside the model projection.

## Allowed decisions and durable guards

A native target may be referenced by `NO_CHANGE`. The compiler and journal
validate its trusted native binding; `UPDATE`, including retraction, is denied.
Native targets never reach MemoryWriter. A native `NO_CHANGE` settles evidence
without creating a local fact/history, rewriting the native note, appending local
provenance to it or counting it as a committed local memory. New independent local
facts and changes still use the existing scope/CAS/grouped-write contracts.

A snapshot binds all eligible native file hashes and sharing/path/format
configuration fingerprints, including files that yielded no selected fragment.
This matters because a change can alter the duplicate status of a proposed
CREATE. Dispatch, accepted-response application and unapplied commit recovery
recheck the current native context. File/locator or sharing changes invalidate
old proposals; same-length edits cannot evade checks by retaining mtime.

Guards store hashes and source locators, not native bodies. Completed/failed/
cancelled run receipts discard request/response plaintext. Recoverable G3f partial
results retain a bounded snapshot capsule; revocation never permits resending its
old native body, and completion or Forget erases the capsule. A fully completed result is a
historical receipt: replay does not reopen native files, call a model or assert
that today's native state is still identical. Old unguarded pending work is not
allowed to ignore newly eligible native sources. Already-applied local operations
are recognized before rejecting remaining stale operations; they are not rolled
back because a native file later changes. Index/receipt-only recovery remains
zero-call. These checks do not turn uncooperative external file writes into an
atomic cross-file transaction; they provide bounded precommit validation.

No replacement relation is inferred from textual similarity. Existing legacy
native index/shadow data is left untouched; this new comparator reads eligible
current notes rather than trusting a disposable index-only shadow flag. Automatic
native conflict resolution or reconstructing missing replacement provenance is
not implemented. A conflicting local CREATE does not mean Hermes' own injected
native note has been updated or hidden. Results disclose this limitation.

## Results and source retention

`native_comparison` reports `available` or `no_eligible_sources`, the selected
fragment count, `selection=bounded_candidates` and `read_only=true`. Old retained
receipts use `not_evaluated`, not a fabricated historical validation result.
`available` says eligible files were read, not that all semantic matches were
found. Local/native identity collisions are explicit errors, never a writable
alias. Native `NO_CHANGE` operation projections carry `native=true`.

The batch also repairs context retention at the shared run/commit boundary.
Pending work protects the prior/subsequent source blocks it actually uses, not
just its selected turn. Context protection does not claim those turns for the
new pipeline. Existing event-key receipts are resolved through capture metadata;
if a dependency lost its mapping, known same-session context is conservatively
protected. Completed work releases this protection. Partial/cancelled control
state GC remains a separate policy, not an unlimited-retention promise.

## Validation boundary

Public deterministic tests cover own/private/foreign sharing, missing and changed
files, index-independent read-only preview, existing fragment identities, budget
selection, same-mtime edits, revoked sharing, local/native ID collision, forbidden
writes, same-call NO_CHANGE, partial local success, frozen recovery, real process
exit, no re-dispatch and source-context cleanup. Native files are synthetic and
all model responses are local substitutes. Real Hermes/Flash and native OS
acceptance, legacy remember routing, generalized scope merging and default routing
are still separate work. Limited explicit partial repair/replan is described in
`incremental-partial-recovery.md`.

G3g connects bounded scope registration to the same shared commit path; see
`incremental-scope-registration.md`. Discovery is opt-in and never expands an
explicit write boundary.


## Native Windows observation correction

The first installed-artifact Windows run of `5e707c2f` executed 932 contracts,
with two failures and seven errors. Eight unsuccessful cases shared an early
`native_source_changed_during_read`; another was a CRLF test-fixture error.
This is not a successful native validation and is not explained away by a slow
runner or by the independent migration-inspection allocation optimization.

In CPython v3.12.10, `win32_xstat()` in `Modules/posixmodule.c` maps ctime back
to birthtime for `stat`/`lstat`, whereas `_Py_fstat_noraise()` in
`Python/fileutils.c` obtains metadata ChangeTime through the handle. Once a file
has been edited those values can differ for the same stable file. Requiring all
four ctimes to be identical incorrectly rejects legitimate native content.
Reference implementation (the version recorded by that Windows runner):

- https://github.com/python/cpython/blob/v3.12.10/Modules/posixmodule.c
- https://github.com/python/cpython/blob/v3.12.10/Python/fileutils.c

The reader now checks path-before against path-after, and handle-before against
handle-after, retaining both timestamp checks without equating their ctime
meaning. Device/inode, size and mtime must still agree at open. A path or handle
change, nonregular file, unexpected length, growth, truncation or changed
metadata remains an error. Reads request the opened file size plus one growth
sentinel, never the entire file allowance for every tiny note. Source hashes,
sharing guards and commit-time snapshot validation are unchanged. There is no
sleep, ignored error, automatic retry or fallback to a stale native index.

The newline-only reuse test explicitly establishes LF bytes before converting
to CRLF. A Windows text write may already create CRLF; blindly replacing those
LFs creates CRCRLF and is not a formatting-only transformation. The test keeps
its inventory ID, one-reparse assertion and snapshot-equivalence check.

Focused regressions simulate only differing timestamp observations and retain
real file I/O. They also test real edits, errors, sizes and changes through
either observation channel. Passing these on Linux does not establish native
Windows acceptance. The corrected exact candidate must complete the existing
five-cell installed-artifact matrix. Production Vaults, real models, schemas,
prompts, request allowances and default routing are not changed by this fix.
