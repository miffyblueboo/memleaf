# Changelog

All notable changes to memleaf are documented here.

## 0.2.0 — 2026-08-31

- Hardened the supported Codex integration against the official Codex CLI 0.151.0 on Windows and macOS, including `CODEX_HOME`, npm `codex.cmd` discovery, UTF-8 CLI output, idempotent `mcp add/get`, and quote-free Windows lifecycle commands carried through a UTF-16LE PowerShell `EncodedCommand` payload.
- Aligned Codex `PreToolUse` argument rewriting with the current hook contract by returning `permissionDecision=allow` together with `updatedInput`, preserving the current-turn `retrieval_id` binding.
- Restored the finalized automatic retrieval contract: the bounded Scope Map exposes only Scope metadata, `search` candidates expose only `memory_id + title`, and selected memory bodies enter context only through bounded `read` calls under the existing retrieval gate and budgets.
- Added durable shared-Vault acceptance for Codex → Codex, Hermes → Codex, and Codex → Hermes so cross-session and cross-host memory reuse is verified without duplicating CREATE/UPDATE/NO_CHANGE, history, deduplication, or processing semantics outside the Core.
- Kept Codex installation explicitly opt-in and fail-closed: existing Codex model/provider, profiles, sandbox/approval settings, MCP entries, hooks, and custom providers remain untouched; conflicting host Vaults are rejected rather than guessed.
- Decoupled the Codex host from the extraction model. Automatic processing uses an independent memleaf Model Route, reports `processing_status=model_route_required` when no complete route exists, and never copies Codex credentials or silently consumes the active Codex session model.
- Promoted native Codex Windows/macOS acceptance, cross-host memory roundtrips, Linux/Windows/macOS host tests, and packaging checks into release-blocking CI gates.

## 0.1.8 — 2026-08-31

- Fixed Windows UTF-8 stdio handling across the MCP server, Hermes MemoryProvider, and Codex host-event entry point so Chinese and other non-ASCII memory content can be captured, searched, read, and injected without depending on the system code page.
- Added native Windows coverage for Codex executable discovery and Hermes MCP subprocess execution, including Unicode paths and payloads.
- Strengthened Windows and macOS release gates to exercise Hermes and Codex installation, host lifecycle, bounded Scope Map injection, controlled `search` → `read`, retrieval budgets, memory extraction/update semantics, and session-lineage behavior before release.

## 0.1.7 — 2026-08-31

- Added supported Codex integration through the explicit
  `memleaf install --host codex` command, including MCP registration through
  the Codex CLI and merge-safe lifecycle hooks that remain subject to Codex's
  user review and trust flow.
- Added the Codex V2 memory lifecycle: bounded Scope Map injection, controlled
  `search` → `read` retrieval, visible-turn capture, automatic processing, and
  compact-session restoration without injecting memory bodies up front.
- Preserved Hermes conversation lineage across context compaction so complete
  source turns remain available to extraction instead of summarizing only the
  post-compaction fragment.
- Improved memory maintenance to keep durable business facts and constraints,
  discard incidental execution details, and apply sequential state updates to
  one stable memory with prior versions in history.
- Consolidated the Hermes MemoryProvider into one packaged implementation and
  made every installer and test consume that source.
- Added macOS Codex lifecycle CI and expanded Linux/Windows packaging and host
  regression coverage.

## 0.1.6 — 2026-08-29

- Made the normal install command the supported upgrade path for existing memleaf installations.
- Preserve the Vault already configured in Hermes `memleaf.json` during upgrades, including custom Vault locations used by early releases.
- Added deterministic Vault precedence: explicit `--vault` → existing Hermes config → `MEMLEAF_VAULT` → default `~/.memleaf`.
- Fail safely when an existing Hermes memleaf configuration is malformed instead of silently switching the user to a new default Vault.
- Added Linux and Windows regression coverage for upgrade Vault selection.


## 0.1.5 — 2026-08-29

- Fixed the Hermes MemoryProvider stdio client on Windows by replacing
  `select.select()` on subprocess pipes with a background stdout reader and
  timeout-aware queue.
- Added a real cross-platform Provider → `memleaf-mcp` integration test that
  exercises `stats`, `scope_catalog`, and visible-turn `capture` through
  the actual stdio subprocess boundary.
- Windows CI now runs the real Hermes provider transport acceptance test on
  Python 3.11, 3.12, and 3.13.
- Kept provider tool registration intentionally empty: the MemoryProvider owns
  automatic recall/capture/process while the separately configured MCP server
  owns the 11 deliberate tools.

## 0.1.4 — 2026-08-28

- Reject Hermes display-redacted API credentials such as `***` and head/tail masks instead of treating them as callable keys.
- Fall through from a redacted direct credential to configured environment variables and Hermes `.env` values.
- Reject masked credentials in existing memleaf routes and at runtime so an old bad route cannot remain silently usable.
- Add regression coverage for masked Hermes credentials on Linux and Windows.

## 0.1.3 — 2026-08-28

- Restored the MIT License for the current release.
- Added native Windows Hermes path discovery using `HERMES_HOME` or the official
  `%LOCALAPPDATA%\hermes` default.
- Added discovery for the official Windows Hermes launchers under
  `bin\hermes.exe`, `bin\hermes.cmd`, and
  `hermes-agent\venv\Scripts\hermes.exe`.
- Added a one-line PowerShell installer that uses Hermes' managed Python when
  available, installs memleaf from PyPI, and completes Hermes integration.
- Made atomic file writes and Windows command-launcher detection portable.
- Added Windows GitHub Actions coverage for Python 3.11, 3.12, and 3.13.

## 0.1.2 — 2026-08-28

- This release was published under AGPL-3.0-only before the project returned to
  MIT in 0.1.3. Its original license remains valid for copies of 0.1.2.
- Product behavior otherwise remained aligned with 0.1.1.

## 0.1.1 — 2026-08-28 (GitHub tag: v0.1.1)

- Added a first-class `memleaf install` command for complete Hermes setup
  after installing the package from PyPI.
- Packaged the Hermes MemoryProvider inside the Python distribution, removing
  the need for a Git checkout during normal installation.
- Added safe migration from the source-installed provider symlink to the
  packaged provider.
- Added `python -m memleaf` so the complete one-line install works even when
  the console-script directory is not already on PATH.
- The installer initializes the Vault, discovers or preserves a model route,
  activates the Hermes MemoryProvider, configures MCP lifecycle settings, and
  verifies the 11-tool MCP surface.

## 0.1.0 — 2026-08-28 (GitHub tag: v0.1)

- Added the local-first, dependency-free Markdown vault core with capture,
  memory creation, deterministic search/context retrieval, statistics, and
  rebuildable indexes.
- Added safe vault paths, file locking/atomic writes, frontmatter handling, and
  secret redaction for captured text.
- Added model-backed admission, extraction, state updates with history,
  compaction, retryable processing, and scope-aware maintenance without adding
  runtime dependencies.
- Added the `memleaf` initialization CLI and the `memleaf-mcp` stdio entry
  point.
- Added Scope Map → directory search → bounded read retrieval with per-turn
  retrieval state, pagination, and read budgets.
- Added a Hermes-native MemoryProvider plus MCP setup.
- Added packaging metadata, examples, offline local installation, and CI for
  Python 3.11–3.13.
