# Memleaf memory provider for Hermes

This is a Hermes user-level memory-provider plugin for the local memleaf vault.
It is separate from the `memleaf-mcp` MCP server, but uses the same
`~/.memleaf` directory and executable.

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
  "command": "memleaf-mcp",
  "timeout": 5,
  "process_timeout": 300
}
```

Capture, stats, and context calls use the short `timeout`; model-backed
processing uses `process_timeout` so a slow local model does not get mistaken
for a dead MCP worker. A failed or timed-out request closes the MCP process;
the next provider call starts a fresh one.

Automatic processing is enabled by default and requires a configured memleaf
model route. Failures retain the inbox for retry. Storage remains local;
when an API model is selected, processing inputs are sent to that provider.
