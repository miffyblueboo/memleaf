# Configuration migrations

This document describes the configuration and Vault-layout compatibility boundary starting with memleaf v0.2.28.

## Current configuration

The current persisted top-level sections are `vault`, `agents`, `scopes`, `native_sources`, `process`, `history`, `capture`, and `llm`. Retrieval remains Scope Map -> search -> read; there is no configurable legacy injection mode.

`capture.tool_evidence_mode` is the current tool-evidence retention setting and accepts `bounded`, `metadata`, or `off`. `capture.include_attachments` is independent and defaults to `false`.

## Deprecated fields

- Top-level `inject` (`mode`, `abnormal_guard`) belonged to the pre-Scope-Map injection path. v0.2.28 reads and discards this section; it is not written again.
- `capture.include_tool_output` is replaced by `capture.tool_evidence_mode`.
- `Vault.processed_index_path` and `Vault.agents_index_path` remain narrow Python compatibility aliases for v0.2.28, but both resolve to `_state/`; internal code uses the new state names. They are candidates for removal in a future major cleanup.

## Automatic migration

When reading an older configuration, `capture.include_tool_output: true` becomes `tool_evidence_mode: bounded`; `false` becomes `metadata`. Saving the normalized configuration writes only the current field. If both legacy and current fields are present but disagree, loading fails closed instead of guessing.

The obsolete `inject` section is removed during normalization. No current runtime component consumes it.

On first v0.2.28 Vault use, runtime correctness state is migrated from `_index/` to `_state/` by one centralized layout owner. The new copy is atomically written and verified before a durable `_state/layout.json` completion marker is written; legacy files are removed only after that marker. Before the marker, old/new coexistence must be equivalent or startup fails closed. After the marker, `_state/` is authoritative and stale `_index/` state is cleanup debris, never merged or replayed.

## Incompatible cases

Migration fails closed for malformed runtime JSON, unsafe symlinks, divergent pre-marker old/new state, an invalid layout marker, or an old process still holding a legacy lock strongly enough to prevent cleanup. Stop older memleaf/Hermes/Codex processes and retry; do not delete `_state/` to force an upgrade.

## User action

Normal users do not need to edit their Vault or configuration. For a live upgrade, stop older processes that are using the Vault before starting v0.2.28. Backups remain recommended for any software upgrade. `_index/` is disposable and rebuildable; `_state/` is not.
