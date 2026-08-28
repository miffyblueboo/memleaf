# memleaf 0.1.5 — Windows Hermes stdio transport

## Fix

memleaf 0.1.4 could install successfully on native Windows and the standalone
`memleaf-mcp` server worked, but the Hermes MemoryProvider's private stdio
client used `select.select()` to wait on a subprocess stdout pipe. Windows
`select()` only supports sockets, so provider calls such as `stats`,
`scope_catalog`, and `capture` failed immediately with `OSError`.

0.1.5 replaces that wait path with a background stdout reader thread and a
timeout-aware queue. The MCP protocol, request timeout behavior, and provider
fail-open behavior remain unchanged.

## Verification

CI now starts the real installed `memleaf-mcp` executable through the same
`_MCPClient` used by the Hermes MemoryProvider and verifies:

- `stats`
- `scope_catalog`
- user capture
- assistant capture
- durable inbox output

The test runs on Windows with Python 3.11, 3.12, and 3.13, in addition to the
full Linux regression suite.

## Windows

```powershell
irm https://raw.githubusercontent.com/miffyblueboo/memleaf/main/install.ps1 | iex
```

Re-running the command upgrades an existing installation to 0.1.5 and refreshes
the packaged Hermes provider. Restart Hermes afterwards.
