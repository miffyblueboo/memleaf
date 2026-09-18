# Real-host acceptance: inventory first, then an authorized isolated run

Use one exact candidate and its existing CI artifacts. This runbook is not a new
host adapter or a claim that a real host has already passed. Source tags such as
`hermes` and `codex`, a mocked host base class, and tool discovery are insufficient
proof of an installed host lifecycle. See the [evidence guide](acceptance-evidence-guide.md),
[Hermes runtime contract](hermes-mcp-runtime.md), and
[migration contract](migration-preflight.md).

## Stage A — local inventory, zero model execution

This stage may create only a new private evidence directory. Do not start a chat,
background extraction, worker, install, update, migration or configuration write.
Do not copy a live Vault or native notes into a fixture. Do not construct a
`Memleaf` instance on a live Vault merely to inspect it: initialization can have
state effects. Inspect configuration bytes selectively, not by dumping them.

Record the actual executable, version, native OS and Python/runtime environment
for Core and each existing host. Resolve executables from the local environment;
a missing CLI does not establish that a desktop application is absent. Read the
actual installed help or local documentation to identify supported isolation.
Do not invent `--profile`, home-directory flags or environment settings for a
host. If an inspection command might start a session or write live state, do not
run it under this stage; record that field as not yet verified.

For a known Core executable, the existing commands are:

```sh
memleaf-mcp --version
memleaf-mcp --provider-build
```

Use the resolved executable path rather than a different shell installation.
Both modes exit before constructing a Vault. The packaged Provider descriptor
is not the installed host's copied Provider and is not proof of what a resident
process has loaded. Record the copied resources separately, using the existing
Provider build contract; never rewrite them as part of inventory. Equal package
version strings are insufficient. See [Provider compatibility](hermes-provider-compatibility.md).

Record only safe settings: canonical Vault location (a local alias may be used
in shared reports), configured automatic/remember routes, whether credential
references resolve, and whether isolated host configuration is supported.
Record explicit per-call overrides separately from configured defaults; source
configuration alone cannot prove the route actually used by a queued job.
Do not return API keys, tokens, authentication headers, credential-bearing URLs,
full configuration, chat bodies, native-memory text or backups. Keep raw paths
and necessary private fingerprints in local evidence rather than public CI.

Prepare the existing public suite with the exact candidate Python:

```sh
python -m memleaf.acceptance --suite examples/incremental_acceptance.json --repeat 5
```

The suite path must point to the selected checkout; an installed wheel alone need
not place `examples/` in the current directory. Planning accepts no backend,
output or execution flags. Save stdout by the caller into the evidence directory,
not with the tool's `--output` option, which belongs to execution. Record both
file SHA-256 and the normalized suite digest in the result. This planning command
does not read a backend configuration, create a Vault or call a model.
`process --dry-run` is not a substitute; it can call the configured model.

A useful Stage A handoff contains:

| Field | Required observation |
|---|---|
| Candidate | Remote commit/tree, run/attempt, manifest and original artifact hashes |
| Core | Resolved executable, version, packaged Provider descriptor |
| Hermes / Codex | CLI or desktop form, actual version, isolation method and evidence; unknown if unverified |
| Copied Provider | Loaded-copy verification available or still pending; no assumption from the package version |
| Vault | Canonical target and a proposed independent synthetic path; production bytes unchanged |
| Routes | Configured automatic and remember values; effective result/payload not yet claimed |
| Model | Safe provider/model identity when visible; credential reference available/unavailable, never its value |
| Suite | Path/hash, plan result, repeats and proposed request limits; no approved spending inferred |
| Outcome | Ready for separate execution authorization, or named blockers |

## Stage B — isolated host sessions, separate authorization

Before starting, obtain approval for the actual host/model routes, synthetic or
private sample permissions, repetitions, output location and hard request limit.
Host-agent calls and Memleaf extraction calls are separate costs. The Memleaf
acceptance tool's cap does not limit arbitrary host-agent sessions. No session
may silently use a stronger substitute model to obtain a passing answer.

Use a verified independent host profile/runtime and a new empty synthetic Vault.
When isolation is not supported or not proven, stop that host's experiment and
report the limitation instead of editing live configuration. Use synthetic native
files for the initial share/read-only test. Real incident material and live native
notes are not implicitly authorized by this runbook.

After isolation is proven, select the two routes explicitly in that test Vault:

```yaml
process:
  automatic_pipeline: incremental
  remember_pipeline: incremental
```

Neither setting changes the other or the package defaults. Keep production
configuration unchanged. Verify the effective route returned by real operations
and any queue-bound controls; a configuration screenshot is insufficient.

Use the existing runtime and acceptance tooling rather than another harness.
Capture each observation on the exact installed candidate:

| Sequence | Required evidence | Do not infer |
|---|---|---|
| Real user input, intermediate response, final response | One eligible source work; actual source identity/time/final when provided | Missing source time is not now; role counts alone do not prove final |
| First durable action, then explicit completion | Original memory ID updated; body/state/deadline/assignee readable by another session | A second completion fact is not closure of the old active task |
| Automatic NO_MEMORY, later explicit retention | New real authorization may proceed; replay of the same authorization remains settled | Changing a transport label is not a new authorization |
| Native sharing and native-file revision | Original native bytes unchanged; read permission rechecked; old fragment identity invalidated | Local changes do not automatically rewrite or hide host-native memory |
| Reconnect/restart and late final after stop-recording | Original budget and operation identity preserved; forbidden old material not captured | A process restart grants neither recording consent nor a fresh budget |
| Other host reads and remembers | Actual second host/tool trace, same Vault, supported field projection and route | Merely writing a different source label is not a second-host test |
| Forget and a new explicit retention request | Old dependent work cannot revive the target; shared-source scope explained | Memory deletion is not guaranteed deletion of external logs/backups |

Semantic review checks each retained item against the actual allowed source,
including value, subject, project, deadlines, current status and minimal useful
expression. Include reverse cases: independent new obligations, late unrelated
facts, cancelled deadlines and a new genuine retention intent. A success exit
code or all-NO_MEMORY output is not a semantic pass. Keep every failure,
interruption and attempted request in the evidence; reserved and provider-billed
usage are different, and unknown usage remains unknown.

## Stage C — cold-switch rehearsal, not production activation

Use an isolated installation. Stop each known old host/MCP/worker and wait for
its actual exit; no live owner in a status file is insufficient proof. Inspect
and resolve or explicitly quarantine pending work without erasing its authority.
Preserve current state and verify the complete backup. `migration-check` only
observes readiness; `migration-backup --writers-stopped` accepts an operator
attestation and still checks known live work. Neither command stops old writers
or grants switching permission.

Upgrade the isolated Core and copied Provider together, restart, and verify the
loaded executable/resources, Vault binding, configured/effective routes and a
new permitted workflow. A compatibility refusal must remain visible rather than
being treated as successful capture or forgotten pending work.

Rehearse a specifically supported rollback choice. Keeping latest data with a
compatible read path is different from restoring an older snapshot. Do not
restore content forgotten after a backup merely because its checksums match.
Latest suppression/authority constraints must be available and applied under a
separate authorized restoration procedure; otherwise record rollback as blocked.
This release scope does not provide a general automatic restore engine.

## Closeout

For each original requirement, retain the exact tested subclauses, evidence and
remaining limitations. Final native-package, real-host, live-semantic and
migration conclusions remain separate. Documents do not turn a pending gate into
a pass. Merging main, changing versions, creating tags/releases or modifying the
production Vault requires the applicable separate authorization.
