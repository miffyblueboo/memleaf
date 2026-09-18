# Acceptance evidence guide — fixed candidate, separate gates

This is an operator/evidence guide, not a new runtime, model prompt, test harness,
or permission to change a live Vault. Keep the exact acceptance commit and its
CI artifacts fixed while evaluating it. If an actual defect requires a code
change, record a new commit and rerun affected gates; do not mix their results.

## Gates and completion evidence

| Gate | Evidence required | What it does not establish |
|---|---|---|
| Repository identity | Actual remote commit, full tree, parent, branch | Passing tests, installation or release |
| Source and native artifacts | Source report; each existing native CI cell's wheel/sdist reports; matching commit, manifest, full test inventory and payload hashes | Real Hermes/Codex lifecycle or model semantic fidelity |
| Deterministic requirement clauses | Original requirement, exact test method and assertions, executed evidence on the fixed candidate, explicit limitations | Entire requirement signed off merely because a related module ran |
| Real installed host | Executable/version, copied Provider build, actual test Vault and effective routes, capture/final/source-time flow, reconnect, privacy and shared retrieval | Model quality based only on a stub response or tool discovery |
| Live Flash semantics | Approved configured route, permitted suite and holdout provenance, total reservation limit, all attempted repetitions, structural results plus human/source-based semantic review | A JSON-only pass or exact provider billing from a reserved slot |
| Controlled migration/rollback | Known writers actually stopped, verified current backup, retained authority and latest Forget boundary, isolated rehearsal | Automated restore, distributed consistency or production authorization |

A test that intentionally expects a refusal validates that protection, not a
successful extraction. NO_MEMORY may be correct, but cannot be used as a blanket
way to eliminate necessary memories. Keep pending, scan completeness, semantic
quality and successful writes separate. Never mark all FS001–FS200 complete from
the suite count or from module-name references.

## Current route semantics

Both `process.automatic_pipeline` and `process.remember_pipeline` default to
`legacy`. Selecting one does not switch the other. A successfully installed
candidate is not proof that either new route is active. Use the exact installed
configuration and returned route/state when recording acceptance; no instructions
here modify production defaults or grant a production switch.

After isolation and authorization, the supported opt-in values are `incremental`
for both keys. They are test configuration choices, not edits to package defaults.
Existing queued work retains its route/authority contract. Do not erase old work,
failed receipts or request counters to get a clean-looking preflight result.

## Preparation with no paid calls

Use the existing entry point on a known candidate installation:

```sh
python -m memleaf.acceptance --suite examples/incremental_acceptance.json --repeat 5
```

This is plan-only: it validates the suite and reports hashes and request bounds;
it does not load a backend configuration or create a Vault. `process --dry-run`
is different and may call the configured model. Do not use it as a free preview.

The shipped suite is public synthetic regression material. Take case count, turn
count, repetitions, normalized suite hash, prompt hash and request bounds from
this command on the selected candidate; do not inherit numbers from an older
report. Also record the original suite file SHA-256: it is not the normalized
semantic suite hash. The reported bound is not a spending authorization. A private
held-out suite requires its own provenance, permission and count. Merely adding
a label `holdout` does not prove the examples were unseen in tuning.

Do not execute until the actual route/model, local credential reference, suite
hash, repeats and total allowance are approved. Keep credentials local. Do not
paste API keys or credential-bearing URLs into chat, public issues or logs. All
trials, including failed, interrupted and budget-limited ones, remain in the
report. Starting a new output directory is not an unmetered retry.

## Bind evidence without manufacturing a pass

Keep an external acceptance record containing repository commit and tree, CI run
and attempt, manifest digest, wheel/sdist digests, executed test inventory, exact
requirement clauses, host identities, effective routes and suite/budget approval.
A rerun has a new attempt; an assertion change has a new source identity even
when test IDs/count are unchanged. Never substitute another attempt's missing
stage report. A green workflow summary is not a replacement for the reports.

A documentation-only follow-up still has a new repository tree and sdist bytes.
Previous CI remains evidence for its original commit. Record that the runtime and
test bytes are unchanged, but do not relabel that CI as a run of the follow-up.
Do not overwrite a published artifact with a same-version development artifact.

A requirement record should name its original assertion, tested subclause, exact
test method, source digest, execution report, result and remaining boundary. A
failed platform cell cannot be signed off because another platform passed.
Classify untested semantic/host assertions explicitly rather than converting them
to a static-code pass. Link any later repair to its new test evidence.

## Host preparation and privacy

Read the installed host's actual help and configuration; do not invent isolation
flags. Inventory versions and safe effective settings before starting sessions.
A host may make model requests during a session, so no live session or extraction
is implied by a request for zero-call preparation. If host profiles cannot be
isolated without touching production, stop that part and report the limitation.

Prepare an independent environment and an empty synthetic test Vault only after
its path and isolation are clear. Do not copy a formal Vault, native notes or
real chat archive merely to make a fixture. Use synthetic native files for initial
share/read-only tests. Private incident material must be separately approved.

For real acceptance, source labels alone (`hermes`/`codex`) do not prove the actual
host ran. Record actual process/configuration and behavior. Test one capture per
turn, authentic available source metadata, final versus intermediate messages,
stop-recording and late callbacks, shared search/read, repeat/new intent, same-ID
state changes and restarts. Missing original time stays unknown, not fabricated.

## Migration and release boundaries

`migration_preflight()` is an observation. `writers_stopped=True` is an operator
attestation, not process-discovery proof. Preflight/backup verification do not
perform a full apply/restore or stop old programs. Known writer shutdown and
reopening must be verified independently on the isolated installation.

A valid old backup may contain content forgotten later. Do not restore it for
service without preserving and applying the available later Forget/authority
constraints under explicit permission. A code rollback that keeps current data
and uses a verified compatible read path is not a full snapshot restore.

Full destructive authority TTL, cross-Vault merge, general multi-ID semantic
merge, automatic native-conflict resolution and cross-machine coordination
remain outside the first-enabled scope. Keep those limits explicit; they cannot
be reported as implemented features. Release/tag/version changes, main merge
and production operations each retain their own authorization boundary.

The [host acceptance runbook](host-acceptance-runbook.md) separates inventory,
authorized sessions and cold-cutover rehearsal. It does not require a new
installer, adapter or verification framework.
