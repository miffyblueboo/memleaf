# Hermes Provider build compatibility (FS171 / FS177 / FS178)

The package version alone cannot distinguish unreleased builds. The existing
Hermes stdio bridge now compares the exact behavior-resource bundle expected by
Core, without an additional tool or model request. It does not change the fixed
extraction prompt, model allowance, default pipelines or package version.

## Existing boundary, not a new memory pipeline

The seven fixed resources are `__init__.py`, `_shared.py`, `_mcp_client.py`,
`_provider.py`, `evidence_budget.py`, `provider_compatibility.py`, and `plugin.yaml`.
README text, bytecode caches and local paths do not define the bundle identity.
Core and the standalone directory use identical standard-library-only identity
helpers. The helper does not import Hermes or initialize/read a Vault.

The existing legacy MCP initialize request and reply carry the descriptor under
`_meta["io.memleaf/provider-build"]`: `{ "schema": 1, "digest": "<sha256>" }`.
This is vendor metadata, not an MCP protocol version or an authentication token.
Unknown/malformed descriptors never become a match. Core also advertises the
resource descriptor in modern discovery, but this batch gates the existing
initialize-based Hermes bridge, not every possible future Agent protocol.

Provider snapshots its resource identity at module import. Core snapshots the
packaged Provider identity at server import. Before a supported bridge mutation,
both peers re-read their respective fixed resources. An in-place replacement
cannot refresh a long-running process into a verified new copy: restart is
required. MCP reconnection repeats the handshake and does not reset source work,
request budgets or operation IDs.

Writes require all three observations to agree: import-time copy, current disk
copy, and the peer's expected bundle. This is conservative exact compatibility:
even a comment change in a behavior resource requires matching installation and
restart. It is not a claim that all Core implementation files have the same hash,
or that two different, unmatched adapters are semantically incompatible.

## Failure and continued availability

The existing safe error path carries `stage=compatibility` and one of:
`provider_build_unavailable`, `provider_restart_required`,
`core_build_unverified`, or `provider_core_mismatch`.

Capture, process, remember, forget, maintenance and other non-read tools are not
dispatched on a known mismatched/unverified bridge. Unknown future tools are not
implicitly considered read-only. Stats, process status, scope/search/read,
context and todo listing remain available through the normal contracts (which
may still update existing read counters or retrieval tokens).

The Provider status/prefetch surfaces show a bounded control notice separate from
memory. A refused capture is **not** described as an already captured pending
turn. `writes_allowed` reports only this compatibility gate; normal recording
permission, scope, source identity, revision and budget checks still apply.

A new Provider talking to an older server without metadata can read but cannot
write. A new server also blocks an old bridge declaring `hermes-memleaf` without
a verifiable descriptor. Unrelated MCP clients and direct Core APIs are not
forced into a Hermes resource contract. There is no guarantee against a caller
that changes its identity, bypasses the bridge, or writes Markdown directly.

## Installation and cold switching

The selected console command now supports `memleaf-mcp --provider-build`. This
probe outputs only the packaged descriptor and exits before constructing Memleaf,
reading model configuration, or initializing even an explicitly supplied Vault.
The explicit installer uses the selected executable, not PATH guesses, and
requires its descriptor to match before modifying a Vault or host configuration.
Missing support, malformed output, timeout or a different bundle stops that step.

Both first-copy directory replacement and existing-directory file replacement
verify the complete copied bundle. The package must also remain unchanged during
copy. The existing outer installation snapshots/rollback remain in use.
This is not a new installation transaction or automatic process terminator.

Stop supported old hosts/MCP processes, preserve required data/configuration,
install matching Core and copied Provider, verify persisted paths/Vault, and
restart. Do not activate a new pipeline just because the resource check matches.
The unreleased files are not delivered by installing the old published 0.2.65.

## Limits and verification

Each resource is bounded at 512 KiB and the bundle at 2 MiB. Reads use observed
file size plus a growth sentinel. Nonregular files, observed identity/length
changes and read errors produce unavailable. Path and handle ctime observations
are compared within their own API domains, not across incompatible Windows APIs.
Fingerprints contain no source prose, credentials, full paths or hidden reasoning.

These are import-time **resource observations**, not cryptographic attestation of
live Python bytecode. An arbitrary change-and-revert, stale externally supplied
bytecode cache or uncooperative edit in an unobservable loader race cannot be
certified by this mechanism. Cold switching and actual host acceptance remain
necessary. An old binary that never implements these checks cannot be retroactively
controlled by adding metadata.

Regression coverage includes same-version/different-build detection, new and old
bridge pairing, blocked write/no inbox mutation, continued reads, reconnection,
actual stdio processes, standalone loading without Core, bounded/mutating files,
installer copy readback, selected-runtime failure before mutation and diagnostics.
The Hermes base interface is a declared test stub. A real Hermes installation,
real Flash semantic acceptance and production activation are separate results.

MCP vendor metadata reference:
https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/draft/basic/index.mdx
