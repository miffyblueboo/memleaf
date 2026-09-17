# Lossless runtime-state retention (G4d)

This batch supplies an executable, explicitly authorized maintenance operation,
not another model phase or a silent cleanup hook. It addresses two distinct
bounds: 128 full incremental run receipts and 128 active request-budget works.
It does **not** delete old business memory or claim complete time-window garbage
collection. The normal prompt, two-request incremental allowance, legacy default
routes, configuration and package version are unchanged.

## Operator contract

```python
preview = service.compact_runtime_state()  # read-only; no file lock or initialization
result = service.compact_runtime_state(
    dry_run=False, expected_revision=preview["state_revision"], max_records=64,
)
```

The same operation is available without a new MCP tool:

```sh
memleaf maintain-state --vault /path/to/vault --json
memleaf maintain-state --vault /path/to/vault --apply --expected-revision <preview-hash> --json
```

Preview is the default. The apply request checks the current processed and budget
file bytes, batch size and retention-policy version against that preview and
takes the existing Vault lock. Changing `max_records` requires a new preview. A changed
source receipt, concurrent execution, Forget or budget reservation invalidates
it. Preview does not initialize/migrate a Vault, create an index, finalize a
budget, expire a process owner or call a model. A currently live incremental or
legacy processing owner blocks maintenance. Active unresolved siblings are not
converted to success just to obtain capacity.

`max_records` defaults to 64 and allows 1 through 128, **per kind** (runs,
commits and budget works). The report distinguishes selected counts, skipped
reasons, full versus compact slots, current physical bytes, and projected bytes.
It returns neither payloads nor user text. It contains no semantic quality score.
Each apply requires a fresh exact state revision; status polling cannot authorize
it. This is a trusted local administrative interface, not a permission system
against arbitrary programs that can edit the Vault.

## What is compacted

A full run is eligible only when it is completed, its commit receipt is fully
settled with a current index, its request allowance is durably finalized, and
its budget count is at least its recorded reservations. It must no longer carry
a request, response, retention request or partial-recovery seed/basis. Missing,
corrupt or ambiguous control data is not reconstructed. Failed, blocked,
retryable, cancelled and unresolved runs are not newly compacted by this batch.

Completed commit receipts, and a fully settled parent proved resolved by an
exact cumulative child, can use the compact representation. No pending
`prepared`/`applied` operation or before/after body can enter it. Parent-child
relations and all accepted operation IDs stay intact. A compressed parent is
not removed or relinked; the same parent validation still operates after reload.

Compaction preserves the **exact original serialized payload and checksum**
using standard-library zlib/base64. This is deliberate: replacing a receipt by a
few guessed fields would change the contracts of replay, Forget, recovery and
source ownership. No new memory store, tombstone index or parallel recovery
ledger is introduced. Compression is not encryption or deletion; retained
control metadata remains sensitive. Never claim it removes all source metadata
or implements a privacy erasure request.

The run wrapper uses outer version 2; the commit wrapper uses outer version 3
and carries its original inner version (1 or 2). Ordinary uncompressed wrappers
retain their existing versions. Both sizes, stream termination, checksum and the
original payload schema are checked on read. Declared sizes cannot trigger an
unbounded decompression. Restored nonterminal states are rejected. A supported
Forget can still update a compact completed receipt into a compact cancelled
receipt, removing its normal cached result through the existing cancellation
path. It cannot reopen the old operation.

## Budget authority is never freed

A linked, completed budget can leave the active pool after its run is safely
sealed. It stays in the existing budget file with all original turn counts,
limits and completion state, marked `retired`. Existing opaque control extension
fields are preserved by maintenance. A retired work cannot accept a new turn or
reserve another request. A genuinely new authorized intent still obtains its
own work and is deduplicated against current memory normally.

The previous capacity path deleted the oldest all-completed work. A later call
at the low-level budget boundary could then reserve again for the same identity.
Capacity pressure now retires an exact completed **source-work** identity instead
of deleting it. This keeps ongoing ordinary source-based processing available
without dropping authorization. Old job-keyed counters may still be used for
another turn: their completion is not inferred from one completed row. Ambiguous
legacy jobs cause backpressure or the existing explicit migration path, not
silent erasure. Active or partial work is never selected for eviction.

The budget format changes to version 2 only when a retired counter is stored;
v1 files remain readable. An older budget reader rejects v2 instead of silently
throwing away the marker. Already-completed budget replay no longer rewrites the
same completion flag. Corrupt JSON, duplicate keys, invalid retired rows and
oversized state fail explicitly. No API call resets the consumed count.

## Bounds and guarantees

The full working pools stay at 128. At most 4096 compact receipts of each kind
and 4096 retired budget works are retained. Existing serialized run/commit byte
limits remain; decoded receipt payloads also have a 64 MiB per-ledger bound, and
the request-budget file has an 8 MiB bound. Maintenance reads at most 32 MiB per
control file and refuses larger files. These are resource limits, not a promise
of optimal performance for a particular Vault size.

The historical pool is finite and **not aged out automatically**. At that bound,
backpressure remains explicit; increasing retention is not permission to forget
old inputs. This batch therefore extends useful capacity and reduces payload
storage without pretending to implement indefinite throughput, infinite
exactly-once, or full TTL garbage collection. Source event heads, recording
permissions, automatic processing watermarks, pending legacy plans, partial
bases, knowledge, history, native files and user configuration are not deleted.
A future horizon/erasure design must first establish trustworthy outside-window
admission; neither a hash nor an old message timestamp alone supplies that proof.

## Crash and concurrency behavior

Only `_state/processed.json` and `_state/extraction_request_budget.json` can be
replaced by maintenance. Both replacements are individually atomic; this is not
a cross-file transaction. Processed receipt compaction precedes budget retirement.
If the process exits between them, all original decisions and counts still exist.
A fresh preview sees only the remaining physical compaction work. It never invokes
a model, allocates memory IDs or rolls back a later business update.

An I/O failure reports `interrupted` with the files whose replacement could be
verified. The old preview cannot be blindly replayed; inspect again. A failed
readback leaves the physical outcome uncertain rather than claiming nothing
changed. A second exact comparison before each write detects edits to either
file during preparation. Uncooperative external editors can still write after
that comparison; the existing Vault lock is not a universal OS write barrier.

### Final observation and interrupted receipts

After apply begins, read/validation failures between replacements or at final
verification return `RuntimeRetentionError.result`, including already confirmed
`applied_files`. They do not discard that history behind a plain I/O exception.
The public code is `runtime_state_observation_failed`; raw exceptions, local paths
and credentials are not copied into the CLI result.

The final observation must equal the intended post-compaction bytes, including
when there was no physical work to perform. A detected change reports
`runtime_state_changed`; it is not accepted merely as a new completed revision.
External bytes are preserved. This final comparison still cannot detect an edit
after it completes, and it does not make the two files atomic.

When a write raises and readback cannot establish its outcome, `uncertain_files`
lists the affected control-file labels. An empty `applied_files` alone is not
proof that nothing was written. `changed_files` remains the planned write set,
not the confirmed set. On interruption, inspect again: `state_revision` does not
certify a new completed state. Fresh preview/apply can finish only the remaining
physical work, while original memory decisions and replay responses stay intact.

## Compatibility and verification

New codecs are supported by all current run/commit readers, so the same public
historical results, old explicit intents, automatic ownership, native NO_CHANGE,
partial-child links and Forget behavior remain available. Capacity inventory
reports compact counts separately from full working slots. Query/status reads
never compact implicitly. Legacy parsers that do not recognize the outer version
fail closed at that boundary; this does not retroactively constrain every old
binary or user script. Upgrade all supported writers in a controlled stop/backup/
upgrade/restart sequence before enabling this administrative operation on a real
Vault. No production Vault is migrated by this code-delivery task.

Tests cover exact decoded equality, independent new authorization, old allowance
suppression, working-pool reuse, stale previews, damaged/oversized streams,
parent-child recovery, preserved native files, Forget after compaction, missing
source replay, concurrent applies and real process exits between file replacements.
Model responses are deterministic substitutes. Real Flash semantic acceptance,
Windows/macOS/Hermes installation, full time-window erasure and default activation
remain separate acceptance work. Do not convert test count into a quality rate.
