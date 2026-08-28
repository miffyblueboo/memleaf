# memleaf

> A local-first, Markdown-driven shared memory core for AI agents.

[中文](README.md) · [PyPI](https://pypi.org/project/memleaf/) · [GitHub](https://github.com/miffyblueboo/memleaf)

> **Version: v0.1 (Python package version 0.1.0).**
> The core library, Vault, stdio MCP server, initialization CLI, model routing, memory extraction, controlled retrieval protocol, and host adapters are implemented. memleaf 0.1.0 is published on PyPI.
> **v0.1 supports only Hermes.** Codex and Antigravity are not detected, installed, configured, or scanned for models.

## Project scope

memleaf stores an AI agent's long-term memory as local Markdown files owned by the user, and lets multiple agents share one Vault.

- No vector database, embedding service, or resident daemon is required.
- No memleaf account, hosted service, cloud sync, or telemetry is required.
- Markdown files under `knowledge/` are the source of truth for active memories.
- The files can be inspected and edited with Obsidian, VS Code, Vim, or any other editor.
- A local stdio MCP server provides deliberate retrieval, reading, and maintenance operations.
- The runtime uses only the Python standard library and requires Python 3.11 or newer.

memleaf does not automatically put the entire Vault or a whole conversation history into the model context. The current v2 automatic path is “Scope Map → candidate directory → controlled body reads.”

## Current workflow

```text
Visible user/assistant conversation
        │
        ├─ capture: redact and write to inbox/<source>/<session>.md
        │
        ├─ automatic retrieval entry: inject only a bounded Scope Map
        │       │
        │       └─ Agent chooses a Scope and Query
        │              └─ search: return a candidate directory
        │                    └─ read: load only selected memory bodies
        │
        └─ process / remember: model decides what to persist in knowledge/
                                └─ old versions move to history/ on updates
```

### Automatic injection and retrieval

Automatic injection contains only Scope identifiers, parents, aliases, and retrieval protocol instructions. It does not contain memory IDs, titles, bodies, or the full conversation history. Each Scope Map page is limited to 20 items and approximately 2,000 characters.

For a normal user message, the intended flow is:

1. The Agent uses the full current conversation and the Scope Map to choose a scope and query.
2. It calls `search` at least once.
3. `search` returns only candidate `memory_id`, title, and Scope metadata; it does not return the body.
4. If a business fact is needed, the Agent calls `read` with the `retrieval_id` from the same turn.
5. The Agent answers from the selected bodies instead of reading every candidate.

Current limits:

- Scope Map: at most 20 items and approximately 2,000 characters per page;
- search candidates: at most 20 items and approximately 4,000 characters per page;
- one read page: at most 2,000 body characters;
- managed turn: at most 3 distinct memory IDs and 6,000 body characters in total;
- `retrieval_id` must belong to the current turn, and a successful `search` is required before `read`;
- `found`, `no_match`, and tool errors are distinct states; an error must not be reported as no match;
- legacy `context()` and Python `search(view="full")` remain available for compatibility, but are not part of the new automatic injection path.

Hermes uses a native MemoryProvider for lifecycle handling and obtains the Scope Map through MCP; its retrieval gate is a Soft Gate and cannot promise to block every answer that skipped retrieval.

## Memory admission and maintenance

memleaf does not save every sentence. When a complete visible user + assistant turn is processed, the model first evaluates whether it has concrete future reuse value:

- `CREATE`: no related active memory exists, so create one atomic, self-contained memory;
- `UPDATE`: an active memory for the same future use needs new information, so keep its `memory_id` and move the old version to `history/`;
- `NO_CHANGE`: the turn is a duplicate, query, temporary state, test, audit, diagnostic, or otherwise has no stable reuse value, so append nothing.

Additional rules:

- A normal turn typically produces 0–1 memory rather than one memory per sentence.
- New memories need a stable title, self-contained body, and appropriate Scope.
- If project or ownership attribution is unclear, defer the memory instead of guessing `global`.
- Prefer UPDATE or NO_CHANGE over creating a sibling for the same future use.
- An explicit user request can call `remember`, but the content is still normalized, checked, and deduplicated.
- Model, parsing, write, or index failures keep the inbox and processing watermark retryable.
- Automatic cleanup has a 24-hour safety period; a failed processing attempt does not delete the original capture.

## Installation

memleaf 0.1.0 is available on PyPI. If you only need the core library, Python API, CLI, and MCP server, install it directly:

```bash
python -m pip install memleaf
```

The PyPI wheel provides the core library, the `memleaf` / `memleaf-mcp` commands, and the MCP server, but it **does not perform the full Hermes host integration**. For v0.1, use the GitHub source `install.sh` when you want automatic Hermes MemoryProvider installation and activation, MCP configuration, model-route discovery, and default Vault initialization:

```bash
git clone --branch v0.1 --depth 1 https://github.com/miffyblueboo/memleaf.git "$HOME/memleaf"
cd "$HOME/memleaf"
./install.sh
```

If the source is already present, skip `git clone` and first ensure it matches v0.1. The full Hermes install defaults to `$HOME/memleaf`, without an extra `work` layer.

Default locations:

```text
$HOME/memleaf/              # source and editable installation
$HOME/memleaf/.venv/        # memleaf virtual environment
$HOME/.local/bin/memleaf   # user command entry point
$HOME/.local/bin/memleaf-mcp
$HOME/.memleaf/             # data Vault, separate from the source tree
```

The installer does not create an additional `work` layer. It links the source and creates command wrappers in a dedicated virtual environment using only the standard library, without pip/setuptools or changes to unrelated Python packages. macOS/Linux are supported. It finds Python 3.11+ automatically; use `MEMLEAF_PYTHON=/path/to/python3 ./install.sh` if needed.

For an intentionally isolated test from a non-standard source directory, explicitly set the installation root to that checkout:

```bash
MEMLEAF_INSTALL_ROOT="$PWD" ./install.sh
```

### Host setup during installation

`install.sh` initializes `$HOME/.memleaf` and supports only Hermes. If an executable `hermes` is detected, it completes two independent Hermes integrations:

1. Install and activate the official native Hermes `MemoryProvider` plugin.
2. Configure `memleaf` through Hermes' official MCP CLI and verify that the server exposes 11 tools.

Hermes paths:

```text
$HOME/.hermes/plugins/memleaf/   # user-level provider plugin
$HOME/.hermes/memleaf.json       # Vault, absolute MCP command, and timeouts
```

Restart Hermes after installation. If no complete callable chat-model route is found, an interactive terminal asks for model configuration and writes it directly to the Vault configuration file; non-interactive execution returns an explicit failure instead of reporting a false success.

Codex and Antigravity are outside v0.1's scope. Neither installation nor `memleaf init --all` detects them or modifies their MCP, hook, or model configuration. Existing installations are left untouched; legacy adapters in the source tree do not imply support in this release.

### Initialization commands

Preview changes first:

```bash
memleaf init --dry-run --json
```

Then initialize:

```bash
memleaf init --all --defaults
```

Common options:

```bash
memleaf init --vault /path/to/vault
memleaf init --no-hermes
memleaf init --no-model-discovery
memleaf init --json
```

`--no-codex` and `--no-antigravity` are compatibility no-ops. Host configuration is changed only when detection evidence is reliable, the structure is understood, and there is no conflict. Backups are created before changes; unknown or conflicting configuration is left unchanged.

## Hermes integration

Hermes' native Provider and the MCP server are separate entry points, but they share the same `$HOME/.memleaf` Vault:

- The Provider captures visible Hermes user/assistant turns and triggers `process` after a complete turn.
- The Provider supplies only the Scope Map; it does not inject memory bodies.
- The Agent uses MCP `search` to find candidates and `read` with the current `retrieval_id` to load selected bodies.
- MCP remains the deliberate interface for `search`, `remember`, `forget`, and maintenance.
- A failed MCP request closes the broken connection so a later request can create a fresh one.
- `process` uses a discovered or user-configured model route for admission and memory summarization; failed data remains in the inbox.
- cron, flush, and subagent contexts do not automatically recall, capture, or process memory.

Check the native provider after installation with:

```bash
hermes memory status
```

After restarting Hermes, the native Provider and MCP entry should be independently available. The installer changes Hermes configuration only when an executable Hermes binary is detected; otherwise it skips Hermes and prints a diagnostic.

## MCP server

Run the local stdio server directly:

```bash
memleaf-mcp --vault "$HOME/.memleaf"
```

Or use the module entry point:

```bash
python -m memleaf.mcp_server --vault "$HOME/.memleaf"
```

Without `--vault`, the server uses `~/.memleaf`; `MEMLEAF_VAULT` can also specify the Vault. In normal use the server does not need to be kept running manually: Hermes starts it on demand. stdout contains only JSON-RPC messages so logs do not corrupt the protocol stream.

The server currently exposes 11 tools:

| Tool | Purpose |
| --- | --- |
| `capture` | Capture one explicitly supplied visible conversation event into the inbox |
| `context` | Bounded legacy directory; not used by v2 automatic injection |
| `scope_catalog` | Return Scopes, parents, and aliases without memory bodies |
| `search` | Return a bounded candidate directory and `found`/`no_match` status |
| `read` | Read a selected memory body in pages using the current `retrieval_id` |
| `process` | Process complete inbox turns under the admission rules |
| `remember` | Create or update memory after an explicit request |
| `forget_memory` | Delete one memory by exact ID |
| `forget_about` | Forget an unambiguous topic; return candidates when ambiguous |
| `rebuild_index` | Rebuild local derived indexes |
| `stats` | Return Vault counts and diagnostic statistics |

Search results are clues; a title alone is not a business fact. Managed retrieval must use the same `retrieval_id` for `search → read`. Tool errors, Scope conflicts, and exhausted read budgets must be handled as such.

## Python API

The core library has no third-party runtime dependencies:

```python
from pathlib import Path

from memleaf import Memleaf

service = Memleaf.initialize(Path("~/.memleaf").expanduser())

memory = service.create_memory(
    title="User preference",
    body="The user prefers local Markdown for long-term memory.",
    tags=["preference", "memleaf"],
    scopes=["global"],
    type="preference",
)

for item in service.search("Markdown"):
    print(item.memory_id, item.title)
```

Common interfaces:

```text
capture()             Capture a visible event
process()             Process complete inbox turns; requires a model route
remember()            Explicitly save a memory; requires a model route
create_memory()       Directly create a Markdown memory
search()              Local retrieval without updating hit counts
context()             Compatibility API returning a light directory
read() / read_page()  Read a memory or a body page
forget_memory()
forget_about()
rebuild_index()
stats()
compact()             Compact low-priority memories; requires a model route
```

The offline example uses a temporary Vault by default. It does not write to `~/.memleaf` or access the network:

```bash
python examples/basic_usage.py
python examples/basic_usage.py --vault /path/to/your/vault
```

MCP stdio example:

```bash
python -m memleaf.mcp_server --vault /path/to/your/vault \
  < examples/mcp_stdio.ndjson
```

## Model routing

Capture, indexing, directory retrieval, and reading work offline. `process()`, `remember()`, and `compact()` require a usable model route.

`llm.mode` supports:

- `auto`: prefer an explicitly injected host backend, then fall back to a complete API route;
- `host`: use only a host callback explicitly supplied to the Python API;
- `api`: use only the locally configured HTTP API.

`memleaf init` looks for complete chat-model routes only in readable Hermes configuration, filters out non-chat models, and deterministically selects a lightweight model for extraction. An existing valid memleaf route is reused when discovery finds none; use `--no-model-discovery` to preserve a custom route without scanning. OAuth-only routes and model names without a callable route are not treated as usable.

API configuration example:

```yaml
llm:
  mode: api
  provider: deepseek
  protocol: openai
  base_url: https://api.deepseek.com/v1
  api_key: your-api-key
  model: your-chat-model
  request_timeout: 120
  diagnostic_logging: false
```

`api_key_env` is also supported. New routes may store the API key directly in the Vault's `config.yaml`; the file mode is `0600`, and logs and status output never print the key. When a third-party or cloud model is selected, extraction inputs are sent through that provider's call path; this is not memleaf cloud synchronization.

Supported HTTP protocols include OpenAI Chat Completions, Claude Messages, Gemini `generateContent`, and compatible endpoints.

`llm.diagnostic_logging` is disabled by default. When enabled, it writes bounded structural diagnostics without prompts, model bodies, field values, secrets, URLs, or exception bodies.

## Native memory sources

An Agent-maintained text or Markdown file can be registered as a read-only native source in `config.yaml`:

```yaml
native_sources:
  hermes_notes:
    agent: hermes
    path: /absolute/path/to/hermes-notes.md
    share: true
    enabled: true
    format: markdown
```

Rules:

- The native file remains the source of truth. memleaf does not edit it or copy it into `knowledge/` by default.
- Each source must point to a unique file; the current per-file limit is 5 MiB.
- `share: true` allows other Agents to read it; otherwise only the owning Agent can access it.
- memleaf builds a bounded index and revalidates the current source file before reading a body.
- The index refreshes when the file changes; missing or invalid content does not produce stale bodies.

## Vault layout

The default Vault is `$HOME/.memleaf`. A local alternative can be selected with `--vault` or configuration:

```text
$HOME/.memleaf/
├── config.yaml                  # Vault, Agent, model, and processing config
├── README.md                    # Vault notes
├── inbox/                       # Redacted visible conversation captures
│   └── <source>/<session>.md
├── knowledge/                   # Active memories; Markdown is the source of truth
│   └── <memory_id>.md
├── history/                     # Versions before updates or compaction
│   └── <memory_id>--<version>.md
├── _index/
│   ├── tags.json                # Tag, alias, keyword, and link indexes
│   ├── processed.json           # Processing watermark and idempotency state
│   ├── native_sources.json      # Native-source read-only index
│   ├── agents.json              # Host detection and configuration state
│   ├── host_ingest.json         # Host lifecycle cursors
│   ├── retrieval_gate.json      # Bounded retrieval ledger; no query or body
│   ├── retrieval_gate.lock
│   ├── vault.lock
│   └── compaction.json          # Compaction recovery ledger
└── logs/                        # Created only when diagnostic_logging=true
    └── model-diagnostics.jsonl  # Bounded structural diagnostics
```

`knowledge/` and `history/` are human-readable data. `_index/` contains derived indexes and runtime state. Deleting or editing it directly can lose processing watermarks, host cursors, or the current retrieval ledger; use `rebuild_index()` when a rebuild is needed.

Directories are normally created with mode `0700`, and files are stored as plaintext by default. memleaf has no built-in encryption layer; protect the Vault, backups, and model credentials yourself.

## Privacy and security boundaries

- By default, capture accepts only explicitly supplied visible user/assistant text; it excludes system prompts, developer prompts, hidden reasoning, raw tool output, and full attachment bodies.
- Common API keys, Bearer tokens, cookies, JWTs, and private keys are redacted on a best-effort basis before capture is written. Redaction is not encryption and cannot detect every secret.
- Path validation, symlink checks, Vault locks, same-directory temporary files, fsync, and atomic replacement protect local writes.
- memleaf does not upload the entire Vault and has no hosted backend, telemetry, or account system.
- If an API or cloud model is selected, model-processing input leaves the machine and is sent to that provider.
- Legacy `context()` and clients without a host-bound retrieval turn only have per-page limits; they cannot claim v2 cross-turn hard budgets.

## Development and verification

Python 3.11+ is required. Run locally:

```bash
PYTHONPATH=src python3.11 -m unittest discover -s tests -p 'test_*.py' -v
python3.11 -m compileall -q src tests examples
git diff --check
```

GitHub Actions is configured to test Python 3.11, 3.12, and 3.13, build wheel/source distributions, and run tests from the source archive. Configuration is not evidence that remote CI has passed. Build packages with:

```bash
python -m pip install build
python -m build --wheel --sdist
```

## Roadmap

- **v0.2: add Codex support**, retaining the shared Vault, Scope Map, and on-demand reading design.
- More Agent tools will follow gradually, using each host's official integration and authorization mechanisms and opening support after verification.
- These are future plans, not v0.1 capabilities; no release date is promised yet.

## Current boundaries

The following should not be interpreted as delivered capabilities:

- The PyPI package provides the core library, CLI, and MCP server; full automatic Hermes integration still uses the source `install.sh`.
- v0.1 supports only Hermes. Codex and Antigravity are not detected, installed, or configured.
- Hermes retrieval gating is a Soft Gate and does not guarantee that every answer performed retrieval.
- Without a model route, memleaf can capture and retrieve but cannot perform automatic extraction, explicit model-backed memory, or compaction.
- Long-running real-host behavior still depends on the local Agent version, configuration, restart, and model availability.
- There is no Obsidian plugin, web management UI, cloud sync, or transparent encryption layer.
- Routine retrieval never performs batch history cleanup; deletion requires an explicit maintenance operation.

Design and staged implementation notes:

- [v0.1 release checks](RELEASE_CHECKLIST.md)
- [V2 implementation plan](V2_IMPLEMENTATION_PLAN.md)
- [Historical implementation plan](IMPLEMENTATION_PLAN.md)
- [Examples](examples/README.md)

## License

MIT; see [LICENSE](LICENSE).

**memleaf**

*Your memories, in files you own.*
