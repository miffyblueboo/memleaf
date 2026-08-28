# Changelog

All notable changes to memleaf are documented here.

## 0.1.4 — 2026-08-28

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
  MIT in 0.1.4. Its original license remains valid for copies of 0.1.2.
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
