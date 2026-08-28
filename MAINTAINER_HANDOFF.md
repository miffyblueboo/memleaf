# Maintainer Handoff

Current release target: **0.1.5**  
License: **MIT**

This file is for maintainers and is intentionally not linked from the user-facing README.

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

Keep these two provider copies synchronized:

- `src/memleaf/hermes_provider/__init__.py`
- `integrations/hermes/memleaf/__init__.py`

## Required Windows regression gate

Do not consider Windows support verified from package installation alone.

`tests/test_hermes_stdio_transport.py` must run on Windows and exercise the
real installed `memleaf-mcp` subprocess through the provider's `_MCPClient`.
It verifies `stats`, `scope_catalog`, both visible-turn capture calls, and
the resulting inbox file.

Windows CI currently runs this acceptance test on Python 3.11, 3.12, and 3.13.
Linux continues to run the full test suite.

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

Before the next release, update the version in package metadata, both provider
manifests, tests, CI release filenames/tag/title, release notes, CHANGELOG, and
both READMEs.

PyPI versions are immutable. Never attempt to overwrite a published version.
