# Changelog

All notable changes to memleaf are documented here.

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
