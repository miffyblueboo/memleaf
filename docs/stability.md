# Memleaf 1.x stability policy

Memleaf 1.0.0 begins stable maintenance of its documented public interfaces.
Version numbers follow [Semantic Versioning](https://semver.org/): compatible
fixes use a patch release, compatible additions use a minor release, and
incompatible public API changes require a new major version. Stable maintenance
does not imply that every future conversation or model response will be correct.

## Public interfaces

The compatibility scope consists of:

- The names exported by `memleaf.__all__`, their documented constructors and
  functions, and the documented public methods of `Memleaf`, `Core`,
  `MemoryService`, `Memory` and `Vault`.
- The `memleaf`, `memleaf-mcp` and `memleaf-mcpw` entry points, documented CLI
  commands and options, and their documented JSON fields and error codes.
- The published MCP tool names, accepted parameters and documented result
  fields, within the selected profile. Clients should tolerate additional
  optional fields. The original `model` profile remains the default; the
  explicitly selected `host` profile has its own tools and authorization contract.
- Documented configuration keys and the Markdown/frontmatter memory format.
  Existing committed memories and persistent recovery state must retain their
  identity, provenance and interpretation across compatible upgrades. Any
  required migration must be documented before it is applied.

Module internals, names beginning with `_`, internal planner/reviewer protocols,
development harnesses and exact natural-language model wording are outside this
scope. An internal protocol change may fence pending model responses and require
an explicit recovery action; it must not silently reinterpret saved work, erase
unresolved outcomes or reset consumed request budgets.

## Upgrade to 1.0.1

The 1.0.1 release preserves the 1.0.0 public interfaces, existing Model Routes,
default entry points and Markdown/history formats. The new host-model MCP route
is optional and selected with `--profile host`; upgrading does not convert an
existing Codex or Hermes connection to it. No formal Vault migration is required.
The host contract identifier remains `memleaf-host-v2.0-rc1`; protocol identity
and package release numbers are separate.

The host route uses the running Agent model and does not require a separate
Memleaf model API key. A local Owner still supplies client credentials,
read/write scopes, permissions and acceptable source trust. `host-grant` emits
MCP configuration and a credential file path without changing Codex defaults.
Clients invoke the preparation/submission workflow deliberately; it does not
enable automatic capture or automatic processing on every turn. See the
[host contract](host-v2-contract.md) and the [Codex setup](../README.en.md#codex).

Each host work has a default limit of three accepted submits, including accepted
invalid proposals. Repair, replay and I/O recovery preserve consumed budgets and
successful items. This differs from the original model/native Hermes route,
where extraction and semantic review share up to five model requests per work.
Host `actionable` is independent of content type, and `waiting_on` identifies a
dependency separately from `assignee`. The original model extraction rule that
keeps dependencies in prose does not remove these host fields.

Reconnect or restart MCP processes after upgrading to load the installed code
and tool schemas. Update the copied native Hermes Provider with Core through
the existing installer when using that route, then restart Hermes. Compatibility
with existing data is covered by deterministic checks; no formal production
Vault migration or rollback drill was performed for this release.

## Upgrade to 1.0.0

The 1.0.0 runtime preserves the 0.2.96 production interfaces, configuration,
storage formats, prompts and five-request limit. It requires no new model calls
or Vault migration from 0.2.96. Earlier unsupported pipeline settings and pending
work retain their existing migration/recovery requirements; the version number
does not mark them complete.

Update Core and the copied Hermes Provider together through `memleaf install`,
then restart Hermes to load the installed files. The existing build/protocol
compatibility checks still apply. Codex connections must reconnect or restart
their MCP process after an upgrade.

The former `memleaf.acceptance` module, synthetic acceptance suite and query
benchmark are development tools. They are removed from the public package and
repository in 1.0.0 and retained only in the local development directory. Normal
usage examples, MCP discovery examples and artifact verification in CI remain.

## Release evidence

Each release builds one wheel and source distribution, verifies their payloads
and installs those same artifacts on the existing native CI matrix before
publishing. Local regressions and real-host/model acceptance are separate gates:
green installation checks do not prove model semantics, and a successful host
conversation does not replace package verification. Development tests and private
acceptance reports are kept outside the public source and distributions.

For 1.0.1, all 152 added deterministic checks passed. Live macOS acceptance
covered eight basic behaviors with Codex CLI / GPT-6.1 Sol and Hermes CLI /
DeepSeek v4.1 Flash, plus advanced Codex merge, project reopen/cancel,
whole-memory retract/restore and Owner-approved compaction. These runs made no
independent Memleaf model calls; host requests and token usage remain separate.
Corrections and explicit Owner-approved budgets were part of acceptance. This
evidence does not certify every model/client or live Windows/Linux model use;
native installation and package CI are distinct from real-model acceptance.
