# Memleaf memory provider for Hermes

This is a Hermes user-level memory-provider plugin for the local memleaf vault.
It is separate from the `memleaf-mcp` MCP server, but uses the same Vault and
must use a version-compatible memleaf runtime.

The native provider supplies a bounded Scope Map (no memory titles or bodies),
captures visible Hermes turns, and automatically processes complete turns.
Hermes uses MCP `search` and `read` with the current `retrieval_id` for recall.
MCP also supports explicit `remember`, `forget`, and maintenance operations.
This is a Soft Gate, not a guarantee that every answer has performed retrieval.

Hermes discovers this directory when it is installed as:

```text
~/.hermes/plugins/memleaf/
```

Then activate it with:

```bash
hermes config set memory.provider memleaf
hermes memory status
```

The optional provider config is `~/.hermes/memleaf.json`:

```json
{
  "vault": "~/.memleaf",
  "command": "/absolute/path/to/memleaf-mcp",
  "timeout": 5,
  "process_timeout": 300,
  "auto_process": true
}
```

The separate persisted MCP entry uses the same executable and Vault:

```yaml
mcp_servers:
  memleaf:
    command: /absolute/path/to/memleaf-mcp
    args:
      - --vault
      - /absolute/path/to/vault
    enabled: true
    lazy: true
    idle_timeout_seconds: 60
```

Use `python -m memleaf install` to configure both entries. The installer writes
the MCP entry through `hermes config set`, reads `config.yaml` back, and tests
that all 12 tools are discoverable before it reports success. When two memleaf
virtual environments are present, use `--mcp-runtime current` to migrate to the
runtime executing the installer or `--mcp-runtime existing` to retain an
already configured executable after an exact version check. See the
[Hermes MCP runtime guide](https://github.com/miffyblueboo/memleaf/blob/main/docs/hermes-mcp-runtime.md)
for the full multi-environment and manual-recovery procedure.

Capture, stats, and context calls use the short `timeout`; model-backed
processing uses `process_timeout` so a slow local model does not get mistaken
for a dead MCP worker. A failed or timed-out request closes the MCP process;
the next provider call starts a fresh one.

Automatic processing is enabled by default and requires a configured memleaf
model route. Failures retain the inbox for retry. Storage remains local;
when an API model is selected, processing inputs are sent to that provider.
