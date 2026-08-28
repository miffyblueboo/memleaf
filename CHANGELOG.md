# Changelog

All notable changes to memleaf are documented here.

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

This version is intended for source installation from GitHub and is not a PyPI
release. Host behavior still depends on supported local Agent versions, user
trust/authorization, and an available model route.

## Planned

- v0.2: add Codex support.
- Gradually support more Agent tools through their official integration and
  authorization mechanisms. These are plans, not delivered v0.1 capabilities.
