# Maintainer Handoff

Current release: **0.2.0**
License: **MIT**

This file is for maintainers and is intentionally not linked from the user-facing README.

## Shared host runtime

Host integration now has a shared lifecycle layer in `src/memleaf/host_runtime.py`.
Adapters translate native host events into this runtime; memory semantics remain
in the existing Core.

The shared runtime owns host-neutral orchestration only:

- visible user/assistant capture;
- retrieval-turn creation and binding;
- search observation and retry/degraded state;
- process triggering and pending state;
- host-ingest lifecycle state.

It must not implement separate extraction, CREATE/UPDATE/NO_CHANGE, Scope,
search/read ranking, history, or Vault semantics. Those remain authoritative in
the existing Core.

Codex hook events call `HostRuntime` in-process. Hermes remains an independent
plugin runtime and reaches the same lifecycle layer through `memleaf-mcp`:
the MCP `capture`, host-bound `scope_catalog`, and host-bound `process`
paths delegate to `HostRuntime`. Do not make the Hermes plugin import private
memleaf Core modules merely to share code.

Codex support is installed explicitly with `memleaf install --host codex`.
The installer registers MCP through the Codex CLI and merges lifecycle hooks
into `$CODEX_HOME/hooks.json` (default `~/.codex/hooks.json`); users must still review and trust those hooks in
Codex with `/hooks`. Do not report pending hooks as active.

Codex host integration and the process model are deliberately decoupled.
Automatic extraction uses an independent memleaf Model Route from the selected
Vault. Never copy credentials from Codex, change `model`/`model_provider`, or
fallback through the active Codex session. If no route exists, installation may
configure MCP/hooks but must report `processing_status=model_route_required`
and require an explicit memleaf model-route setup before automatic extraction is
considered ready. This keeps DeepSeek and other custom Codex providers untouched.

`tests/test_host_runtime_contract.py` is the host-neutral regression contract.
A future host should satisfy that contract without adding another copy of the
memory lifecycle.

## Current integration model

Hermes has two separate memleaf integration surfaces:

1. **MemoryProvider plugin**
   - automatic prefetch / Scope Map injection
   - automatic visible-turn capture
   - automatic process
   - `get_tool_schemas()` intentionally returns an empty list

2. **Hermes MCP registration**
   - exposes the 11 deliberate memleaf tools such as search, read, remember,
     forget, stats, and scope_catalog

Do not "fix" the log line `Memory provider 'memleaf' registered (0 tools)` by
returning the MCP tools from the provider. Zero provider-owned tools is
intentional and avoids duplicate Hermes tool registration.

## 0.1.5 Windows transport fix

0.1.4 used `select.select([subprocess.stdout], ...)` inside the provider's
`_MCPClient`. That works on POSIX systems but Windows `select()` only accepts
sockets, so real Hermes provider calls failed immediately with `OSError`.

0.1.5 uses:

```text
memleaf-mcp stdout
    -> daemon reader thread
    -> queue.Queue
    -> request thread queue.get(timeout=...)
```

This preserves timeouts without using Windows-incompatible pipe polling.

The only maintained provider implementation is:

- `src/memleaf/hermes_provider/__init__.py`

All installers and provider tests must use this packaged source directly; do
not add a second checked-in Python implementation under `integrations/`.

## Required Windows regression gate

Do not consider Windows support verified from package installation alone.

`tests/test_hermes_stdio_transport.py` must run on Windows and exercise the
real installed `memleaf-mcp` subprocess through the provider's `_MCPClient`.
It verifies `stats`, `scope_catalog`, both visible-turn capture calls, and
the resulting inbox file.

Windows CI currently runs this acceptance test on Python 3.11, 3.12, and 3.13.
Linux continues to run the full test suite.

## 0.1.6 upgrade invariant

The normal install command is also the upgrade command. Do not require users to
uninstall first, and do not move or recreate their memory data during a routine
upgrade.

Vault selection order is intentionally:

1. explicit `--vault`;
2. existing Hermes `memleaf.json` Vault;
3. `MEMLEAF_VAULT`;
4. default `~/.memleaf`.

The existing Hermes config is read before `Vault.initialize()`. If that config
exists but is malformed or contains an invalid Vault value, fail closed instead
of falling through to another path. This prevents an upgrade from appearing to
"lose" memories merely because Hermes was silently repointed to a fresh Vault.

`tests/test_upgrade_preserves_vault.py` is part of the Windows acceptance
matrix and must remain covered.

## Other Windows invariants

- Hermes default home: `%LOCALAPPDATA%\hermes`
- Hermes launchers may be under `bin\hermes.exe`, `bin\hermes.cmd`, or
  `hermes-agent\venv\Scripts\hermes.exe`
- memleaf console entry point is `memleaf-mcp.exe`
- `os.fchmod` is POSIX-only; memleaf file writes catch
  `AttributeError/OSError` and continue on Windows
- display-redacted Hermes credentials such as `***` or head/tail masks must
  never be accepted as real API keys

## Release workflow

A release commit on `main` must start with `release: v<version>`.
CI must pass Linux tests, Windows acceptance, and packaging before GitHub Release
creation. The separate `Publish to PyPI` workflow then publishes with PyPI
Trusted Publishing / OIDC.

Release documentation policy:

- Keep version history in `CHANGELOG.md`.
- Do not add per-version `RELEASE_NOTES_*.md` files to the repository.
- GitHub Release notes are generated from the matching `CHANGELOG.md` section.
- Keep `README.md` and `README.en.md` on the current public version only.
- Keep `RELEASE_CHECKLIST.md` as the reusable release SOP.

Before the next release, update the version in package metadata, both provider
manifests, version-sensitive tests, CHANGELOG, and both READMEs. The release
commit subject must be exactly `release: v<version>`; CI reads the version from
`pyproject.toml`, validates that subject, extracts the matching CHANGELOG
section, creates the GitHub Release, and then the separate OIDC workflow
publishes to PyPI.

PyPI versions are immutable. Never attempt to overwrite a published version.
