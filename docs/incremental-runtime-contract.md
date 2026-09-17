# Configured incremental runtime facade

This reconciles the G3c delivery with the independently committed runner in
017f69d3. It does **not** replace its receipt format or install a second model
pipeline. `run_incremental()` and `resume_incremental_run()` remain available;
`process_incremental()` delegates to these same functions and budget identities.

```python
result = service.process_incremental(
    source="hermes", session_id="session", turn_id="turn",
    scope="project:Atlas",
)
if result["retry_available"]:
    result = service.process_incremental(
        source="hermes", session_id="session", turn_id="turn",
        scope="project:Atlas", recover=True,
    )
```

Without an injected model, the facade selects the configured fixed API route.
Host/auto fallthrough is rejected: one reservation cannot hide multiple backend
attempts. `model=` and `router=` are mutually exclusive. Existing model, thinking,
timeout and output settings are retained. No new external dependency is added.

A transport failure uses its remaining allowance only with `recover=True`.
Saved responses, frozen commits and terminal receipts need no model configuration
and are recovered without redispatch. The lower-level run-ID API is also explicit
recovery authorization. Both interfaces share a two-reservation maximum.

Results retain the canonical runner schema (`run_id`, nested `commit`,
`reserved_requests`, `model_calls_this_invocation`). `reservations` and
`retry_available` are facade aliases. `model_calls_known` counts returned backend
outcomes, not unobserved provider execution or billing after a process crash.
A unique terminal selection can return its receipt after source cleanup; ambiguous
revision receipts require the original run ID, never dictionary-order selection.

The earlier unpushed delivery used a different ledger schema and lease policy.
Those competing implementations are intentionally not overlaid on existing runs.
Existing indefinite-live-process fencing is retained. Original 286 tests remain;
14 facade integration tests cover shared identity, routing, explicit recovery,
cleaned source receipts and saved-response recovery. Read-only native comparison
is connected by G3d; default host-route activation remains staged. There is no version bump/release.

## Selected explicit retention

`remember_incremental()` now binds an actual user request to selected immutable
captured source refs and the same canonical runner. It does not consume the whole
automatic turn. See [selected-source retention](incremental-selected-retention.md)
for identity, privacy, retry and staged legacy-route limits.

## Explicit partial continuation

`recover_incremental_partial(run_id, mode="repair" | "replan")` reuses the same
canonical run and remaining allowance. Ordinary `recover=True` keeps its existing
transport behavior and does not silently replan partial results. See
`incremental-partial-recovery.md` for the versioned partial basis and one-round limit.

## Scope discovery (G3g)

`process_incremental()` and `remember_incremental()` also accept the strict
boolean `allow_new_scopes=False`. The canonical runner binds it into the original
request; changing it during retry conflicts rather than granting new authority.
Scope changes, shared post-head registration and zero-call recovery follow
`incremental-scope-registration.md`. The default production routes do not change.


## G4a automatic entry-point integration

The existing `process()` / CLI / MCP / detached worker can now explicitly select
this same runner via `pipeline="incremental"` or `process.automatic_pipeline`.
The configured default remains legacy and the text remember API is unchanged.
See [automatic-processing-route.md](automatic-processing-route.md) for immutable
queue controls, batch/backlog semantics, read-only status and cold-switch limits.
This is not production activation or native/live-model acceptance.
