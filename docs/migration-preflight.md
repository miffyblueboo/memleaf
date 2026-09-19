# Migration preflight and private local backup (G4e)

This increment implements the preparation portion of the controlled migration
contract (FS178-179, FS188, FS192 and FS196). It neither switches a pipeline nor
stops processes, rewrites business records, resets budgets or restores a Vault.
A verified backup is not permission to activate the new routes. The fixed model
prompt, request allowance, `legacy` defaults and package version are unchanged.

## Inspect before changing anything

```python
report = service.migration_preflight()
# Inspect report['blockers'] and report['required_external_checks'] separately.
```

```sh
memleaf migration-check --vault /path/to/vault --json
```

Preflight requires an existing Vault with `config.yaml`. It does not initialize
or migrate the layout, create a lock, repair an index, expire an owner, launch a
worker or call a model. It validates known configuration, processing, request
budget and job contracts, inspects pending legacy plans and compaction journals,
checks that enabled native-source paths needed by incremental planning are present
as readable regular files, and reuses the public Markdown scan and pipeline-progress
observation. It never reads or copies native-source content during this check. Compact
G4d receipts are decoded through their original validators, not approximated by
status names. A loaded run with missing consumption evidence is blocked rather
than assigned fresh allowance. Unknown control extensions remain opaque; this
is not a certificate that every file format in a Vault is supported.

The result separates:

- `local_status: clear|blocked`, `blockers`, and bounded counts of observed work;
- `snapshot_revision`, identifying the exact managed file bytes inspected;
- current configured routes and local implementation/packaged-Provider fingerprint;
- external checks not performed here, and `switch_authorized: false` in all cases.

No active owner is not proof that an old binary or external editor is stopped.
A `clear` report is only a local observation. Actual Core/Provider installations,
real Flash multi-turn acceptance, native host testing and restore against the
latest available Forget evidence still need independent verification. The
implementation fingerprint covers installed files, not a verified Git commit or
proof that an already-running host has loaded those exact files. The package
version alone cannot distinguish unreleased builds.

The CLI returns 0 for a clear observation, 2 for known readiness blockers and 1
for an operational failure. None of these exit codes grants switch permission.
The report does not print configuration secrets, arbitrary exception messages,
source text, memory bodies or offending file names.

## Preserve the data before repairing or migrating it

After stopping supported writers, the operator can explicitly create a backup:

```python
report = service.migration_preflight()
result = service.backup_for_migration(
    '/private/existing-parent/new-backup',
    expected_snapshot=report['snapshot_revision'],
    writers_stopped=True,
)
```

```sh
memleaf migration-backup --vault /path/to/vault \
  --destination /private/existing-parent/new-backup \
  --expected-snapshot <snapshot_revision> --writers-stopped --json
```

`writers_stopped=True` is an operator attestation, not process-discovery proof.
A known live processing owner or pending/starting/running queue blocks creation.
The flag does not bypass these observations. Operators must stop legacy workers,
MCP/host writer processes and external synchronization before relying on a backup.
No supported source `_state` directory means the operator must review the old
layout first; this command does not create a state directory to obtain a lock.

Unresolved or malformed content does **not** prevent exact-byte preservation when
physical inspection is possible and no known live owner/queue is found. Its
readiness blockers remain in the report and manifest. Otherwise users could be
required to repair data before obtaining the very backup that makes repair safe.
Corrupt ownership data cannot prove quiescence: a preservation-only backup still
requires the operator to stop all writers independently. Unreadable bytes cannot
be silently omitted and cause backup to fail instead of producing an incomplete
archive labelled complete.

The source must still match the preflight snapshot under the existing Vault lock.
Destination must be a new directory outside the Vault, with an existing parent;
it cannot overlap the source in either direction. Existing destinations are
never overwritten. Parent path aliases can be canonicalized; a symlink at the
final destination or in selected managed source paths is rejected.

## Backup contents and privacy

Selected content is `config.yaml` and all ordinary files below `knowledge/`,
`history/`, `inbox/`, `_state/`, and `_index/`. Keeping `_index/` also preserves
legacy-layout control data; it is not an instruction to use a stale index after
restoration. Only the four known advisory lock paths are omitted. Arbitrary files
ending in `.lock` are not presumed disposable. File bytes, including formatting,
unknown timestamps, opaque controls, cancellation and consumed-budget evidence,
are copied without semantic transformation.

The file-only snapshot does not promise original permissions, owners, timestamps,
hard-link identity or preservation of empty directories. Logs, unknown root-level
files, external native notes, host installation/configuration, and external
credential stores are explicitly outside this backup. A Vault backup is not a
backup of the entire Hermes installation or all dependencies. External notes and
credentials require their own operator-controlled preservation.

The destination uses private creation modes (directory 0700 and file 0600 where
supported). Native Windows ACL behavior has not been validated here. Backups
contain source prose and may contain credentials embedded in configuration:
compression, hashes and private creation modes are not encryption. Do not attach
real backups to issues, public repositories or diagnostic uploads.

## Completion and failure contract

Files are written exclusively into `<destination>/vault/` and flushed. Source
bytes are checked again before the versioned `manifest.json` is written **last**.
Its file list binds every size and SHA-256, source Vault locator, snapshot and
installed runtime fingerprint. Backup verification and a final source comparison
must then succeed. Source business/configuration/control bytes are not modified;
acquiring the existing advisory lock may create its disposable lock file.

Each file is flushed; directory durability uses the existing platform helper.
This is not an atomic filesystem-wide snapshot or a guarantee against arbitrary
power failure. Noncooperating writers can still change data after the final
check, or perform undetectable change-and-revert cycles. Detected changes and I/O
failures never authorize rollback, overwrite or automatic repair.

A failed destination is left for inspection. Without a manifest it is incomplete;
if the manifest exists but a final observation failed, the result requires fresh
verification. Absence of a success return does not prove no destination bytes
were written. Original files are preserved. Retry uses a fresh observation and a
new destination, not forced replacement of a partial backup.

Snapshots reuse the bounded inspection helper: 100,000 selected paths (including
directories) and 256 MiB total selected bytes. The manifest is limited to 16 MiB.
Unreadable enumeration, nonregular managed files, source links, changed file
identity or size growth are explicit failures. These are resource bounds, not
performance guarantees. Verification scans add local I/O but no model calls.

## Verify without initializing or restoring

```python
from memleaf.migration import verify_migration_backup
verification = verify_migration_backup('/private/existing-parent/new-backup')
```

```sh
memleaf migration-backup --verify /private/existing-parent/new-backup --json
```

Verification needs neither a source Vault nor model configuration. It checks the
strict manifest version, exact file set, byte sizes, checksums and stable observed
content. It rejects injected files, missing files, changed bytes and source links;
manifest names are not interpreted as paths to extract. It never constructs a
Memleaf instance on the copy, which could initialize or migrate control state.
Checksums detect changes relative to a manifest, not malicious replacement of the
manifest and files together; this is not a cryptographically signed backup.

There is deliberately no apply/restore/pipeline-switch command in this increment.
Restoring an old snapshot may restore content forgotten later. A future restore
must require separate authorization, stop writers, preserve current data, apply
available later Forget suppressions, and explicitly handle unavailable authority.
It must not merge two Vaults, reset counters, reactivate stale plans or use this
manifest as a permanent capability.

## Related control-state correction and validation

The old `session_lineage()` setter could catch a damaged processed ledger, replace
it with an empty object, and then save only its new session link. It now reuses
the same strict processed reader as capture/execution. Invalid JSON, duplicate
keys, wrong containers and unsupported versions fail without replacing the
original bytes. This is a deliberate fail-closed correction to the legacy setter,
not a model-rule change.

Tests use temporary synthetic Vaults and existing deterministic backends. They
cover private exact-byte backups, compact receipts and retired budgets, pending
and damaged controls, live owners/queue states, stale preview, source mutation,
read/write failures, traversal/links/bounds, backup tampering, CLI codes, Forget,
concurrent creation and a real process exit before manifest publication. All
existing tests remain. Real Flash semantics, native OS/host installation, actual
shutdown discovery, restoration and production activation are not claimed.

## Per-file inspection allocation (native-validation follow-up)

The 256 MiB selected-content bound is a **whole snapshot limit**, not a request
buffer for each small file. Inspection checks the path and opened regular-file
identity/size, then reads the observed file size plus one sentinel byte. The
sentinel and a descriptor metadata recheck reject detected growth/truncation or
in-read modification rather than accepting a prefix as a complete backup file.
The original second enumeration/read and final exact-byte comparison remain.
No persistent cache, index shortcut, file omission, source rewriting or extra
model phase is introduced. Snapshot fingerprints and manifests for unchanged
files are byte-for-byte compatible; no data or schema migration is required.

A local Linux/Python 3.13.5 synthetic measurement (five warm-cache repetitions,
1,001 files / 12,003 payload bytes) kept the same snapshot fingerprint while
reducing median tracemalloc peak from 268,627,603 to 204,981 bytes. This is Python
allocation tracking, not process RSS, native Windows latency, a production-Vault
benchmark or a guarantee that arbitrary backup sizes use constant memory. The
snapshot still retains all selected bytes within its original total bound.

The preceding interrupted Windows test trace was in this reader. That identifies
an avoidable allocation at the observed point, not proof that it is the only
reason for the platform run's duration. Native installation checks must be rerun
on the resulting candidate. Read metadata is also not an atomic filesystem-wide
snapshot: uncooperative edits after the final check and undetectable edit/revert
cycles remain outside the guarantee. Source links, nonregular paths, failed
reads, total size/path bounds and final manifest checks retain their existing
failure behavior. No model/default/prompt change or production activation occurs.

## Development closeout: binding, mutations and rollback choice

Preflight now includes persistent local binding state, required-but-missing
request/processed controls, and validated explicit update/retraction journals.
Pending mutations block cutover; malformed authority is not discarded. Older
unbound Vaults remain usable on their compatibility route, but final incremental
cutover requires explicit local binding after other preflight blockers are
resolved. A provided `writers_stopped` value is still the operator's declaration,
not independent process discovery or proof that arbitrary programs have stopped.

The first release scope remains a controlled cold procedure, not an automatic
migration/restore engine. Stop and wait for each known old host/MCP/worker;
finish or explicitly quarantine pending work; verify a complete current backup;
upgrade Core and copied Provider together; restart; inspect actual binary,
resource digest, binding, configuration and Vault; then test a new permitted
work. Restarting an old writer against newer state is not supported.

For rollback, keep current data and use a tested read-only compatibility path
when possible. Restoring a pre-Forget backup is not safe merely because its
checksums match: later suppression and deleted-content requirements must also
be available and applied under separate authorization. No restore command was
added by this closeout. A private Core-only stop/backup/reopen exercise is not
real Hermes/Codex lifecycle verification or a production recovery claim.
