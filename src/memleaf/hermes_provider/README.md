# Memleaf memory provider for Hermes

This is a Hermes user-level memory-provider plugin for the local memleaf vault.
It is separate from the `memleaf-mcp` MCP server, but uses the same Vault and
must use a version-compatible memleaf runtime.

The native provider supplies a bounded Scope Map (no memory titles or bodies),
captures visible Hermes turns, and automatically processes complete turns.
Automatic capture and processing use only the visible user and assistant text.
Tool calls and results, email or attachment bodies, and other hidden payloads are
not automatic memory input.
Hermes uses MCP `search` and `read` with the current `retrieval_id` for recall.
The deferred MCP `save_turn` tool queues explicit retention bound to the current
turn; the completed capture supplies the original user messages and source times.
A queued response is not a saved memory. The installer excludes standalone MCP
`remember` in Hermes while preserving other configured tool choices. MCP still
supports `remember` for other clients, plus `forget` and maintenance operations.
This is a Soft Gate, not a guarantee that every answer has performed retrieval.

The bound MCP route also handles conversational corrections, completion,
cancellation, reopening and withdrawal. A successful full-turn retention receipt
settles automatic extraction without another model request. Partial selections
remain separate. MCP lifecycle updates do not accept model-supplied source times;
trusted Python callers keep their explicit metadata API. Restart Hermes after
upgrading to load the current Provider.

## Setup when installed from the Hermes catalog

The catalog installs the Hermes provider, not the separate Memleaf Core
executable. Core allows provider writes only when the Provider build packaged
in Core (`memleaf-mcp --provider-build`) matches these plugin files; otherwise
capture and processing are blocked and only search and read work. Install the
Core release with the same version as this plugin's `plugin.yaml`, in a
separate environment (not in the Hermes-managed Python environment):

```sh
pipx install "memleaf==<plugin.yaml version>"
memleaf init --no-hermes --no-model-discovery --defaults --vault /absolute/path/to/vault
```

Use the same Vault for the provider and the MCP entry. For a fresh Hermes profile,
put the following in that profile's `memleaf.json` (default:
`~/.hermes/memleaf.json`). Replace the paths with the actual absolute paths on
the machine running Hermes. On Windows, use forward slashes in the JSON or
escape backslashes.

```json
{
  "vault": "/absolute/path/to/vault",
  "command": "/absolute/path/to/memleaf-mcp",
  "auto_process": false
}
```

Add this entry under `mcp_servers` in the same profile's `config.yaml`.
Merge it with any other entries; do not replace the configuration file.

```yaml
mcp_servers:
  memleaf:
    command: /absolute/path/to/memleaf-mcp
    args: ["--vault", "/absolute/path/to/vault"]
    enabled: true
    lazy: true
    idle_timeout_seconds: 60
    tools:
      exclude: [remember, update_memory]
```

Keep the server name `memleaf` so the provider can bind its MCP calls to the
current Hermes turn. Excluding these two legacy tools leaves the source-bound
`save_turn` route available. Then select the provider with `hermes memory setup`
and choose **memleaf**, or run `hermes config set memory.provider memleaf`.
Restart Hermes and check `hermes memory status` and `hermes mcp test memleaf`.
If Hermes reports that the MCP Python SDK is missing, install Hermes MCP support
through `hermes setup` and retry. The MCP connection test lists the server's
advertised tools, not the assistant-visible set after tool exclusions.

If a standalone Memleaf install already occupies the same Hermes plugin
directory, do not remove or overwrite it blindly. Back up the active profile
configuration and Vault, keep using the same Vault, and review the catalog
installation before replacing the old provider. Do not run the standalone
`memleaf install` on a catalog-managed profile: it can copy provider files
over the directory Hermes manages and its default model discovery can inspect
host credential files.

### Updating a catalog install

Update Core and the plugin separately, then restart Hermes:

```sh
pipx install --force "memleaf==<new plugin.yaml version>"
hermes plugins update memleaf
```

Do not use `python -m pip install -U memleaf && python -m memleaf install` for
a catalog-managed plugin; the second command overwrites the plugin directory.
If Hermes logs a provider/core mismatch, writes stay blocked until both sides
match again.

### Data and execution disclosure

The provider starts the explicitly configured local `memleaf-mcp` process over
stdio with a minimal environment: basic system variables (path, home,
locale, time zone, temporary directory, Windows system paths, HTTP proxy and
CA bundle settings) plus, only when the Vault's `llm.api_key_env` names one,
that single model key variable. Other Hermes environment variables are not
passed. The Vault holds Markdown memories and local capture/index state. Selecting
Memleaf enables capture of eligible completed Hermes turns: visible user and
assistant text, not hidden tool results or attachment bodies. The setup above
leaves automatic processing off and does not discover host credentials. Basic
search and read of existing memories do not need an inference model. Enabling
processing with a separately configured cloud route sends eligible processing
inputs to that endpoint; choose that route and its scope deliberately. No
telemetry is enabled by this setup.

## Standalone setup

Hermes discovers this directory when it is installed as:

```text
~/.hermes/plugins/memleaf/
```

Then activate it with:

```bash
hermes config set memory.provider memleaf
hermes memory status
```

The status output includes the effective Vault capture policy from the
read-only MCP `stats` result. If MCP is unavailable, the policy is shown as
unknown rather than inferred from `config.yaml`. Capture policy fields for
tool evidence apply to explicit MCP capture requests; the automatic Hermes
turn path does not send tool output, email, attachment, or document bodies to
capture or processing.

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
that all tools declared by the installed MCP server are discoverable before it
reports success. When two memleaf
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
