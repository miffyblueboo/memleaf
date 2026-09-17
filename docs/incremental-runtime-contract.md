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
cleaned source receipts and saved-response recovery. Native comparison and default
host-route activation remain separately staged. There is no version bump/release.
