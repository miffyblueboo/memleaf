# Changelog

All notable changes to memleaf are documented here.

## 0.1.1 — 2026-08-28 (GitHub tag: v0.1.1)

- Added a first-class `memleaf install` command for complete Hermes setup
  after installing the package from PyPI.
- Packaged the Hermes MemoryProvider inside the Python distribution, removing
  the need for a Git checkout during normal installation.
- Added safe migration from the v0.1 source-installed provider symlink to the
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
- Added the v2 Scope Map → directory search → bounded read protocol, including
  per-turn retrieval state, pagination, and read budgets.
- Added a Hermes-native MemoryProvider plus MCP setup. v0.1 supports only
  Hermes; Codex and Antigravity are not detected, configured, or scanned for models.
- Added packaging metadata, examples, offline local installation, and CI for
  Python 3.11–3.13.

Version 0.1.0 was subsequently published to PyPI; its full automatic Hermes
setup still required the source installer and is superseded by 0.1.1. Host behavior still depends on supported local Agent versions, user
trust/authorization, and an available model route.

## Planned

- v0.2: add Codex support.
- Gradually support more Agent tools through their official integration and
  authorization mechanisms. These are plans, not delivered v0.1 capabilities.
