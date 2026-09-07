# Configuration migrations

This document describes the configuration and Vault-layout compatibility boundary starting with memleaf v0.2.28.

## Current configuration

The current persisted top-level sections are `vault`, `agents`, `scopes`, `native_sources`, `process`, `history`, `capture`, and `llm`. Retrieval remains Scope Map -> search -> read; there is no configurable legacy injection mode.

`capture.tool_evidence_mode` is the current tool-evidence retention setting and accepts `bounded`, `metadata`, or `off`. `capture.include_attachments` is independent, defaults to `false`, and gates only evidence explicitly identified as an attachment. Ordinary structural file/document results follow the selected mode.

## Deprecated fields

- Top-level `inject` (`mode`, `abnormal_guard`) belonged to the pre-Scope-Map injection path. v0.2.28 reads and discards this section; it is not written again.
- `capture.include_tool_output` is replaced by `capture.tool_evidence_mode`.
- The old internal `processed_index_path` / `agents_index_path` names are removed in v0.2.28; runtime code uses explicit `_state/` properties.

## CLI compatibility sunset

`memleaf init --no-codex` and `memleaf init --no-antigravity` are retained only as deprecated no-op argument compatibility for existing 0.2.x setup scripts. They do not select runtime behavior and are scheduled for removal in v0.3. The legacy `init --json` host result slots are likewise retained through 0.2.x so automation does not break during this maintenance release. New integrations must use `memleaf install --host ...` and the current host-state fields.
memleaf's own current installers no longer pass the two deprecated no-op flags; only external 0.2.x callers retain that compatibility surface.

## Automatic migration

When reading an older configuration, `capture.include_tool_output: true` becomes `tool_evidence_mode: bounded`; `false` becomes `metadata`. Saving the normalized configuration writes only the current field. If both legacy and current fields are present but disagree, loading fails closed instead of guessing.
A persisted older config with no `capture` section, or with a partial capture section that has neither evidence field, normalizes to `tool_evidence_mode: metadata`, preserving the previous safe metadata-only behavior. New Vaults still write an explicit `bounded` mode.

The obsolete `inject` section is removed during normalization. No current runtime component consumes it.

On first v0.2.28 Vault use, runtime correctness state is migrated from `_index/` to `_state/` by one centralized layout owner. Existing v0.2.27 Vault/retrieval lock files are acquired during migration so an already-running old worker cannot mutate the copied state concurrently. The new copy is atomically written and verified before a durable `_state/layout.json` completion marker is written; legacy files are removed only after that marker. Before the marker, old/new coexistence must be equivalent or startup fails closed. After the marker, `_state/` is authoritative and stale `_index/` state is cleanup debris, never merged or replayed.

## Incompatible cases

Migration fails closed for malformed runtime JSON, unsafe symlinks, divergent pre-marker old/new state, an invalid layout marker, or an old process still holding a legacy lock strongly enough to prevent cleanup. Stop older memleaf/Hermes/Codex processes and retry; do not delete `_state/` to force an upgrade.

## User action

Normal users do not need to edit their Vault or configuration. For a live upgrade, stop older processes that are using the Vault before starting v0.2.28. Backups remain recommended for any software upgrade. `_index/` is disposable and rebuildable; `_state/` is not.
