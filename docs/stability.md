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
  fields. Clients should tolerate additional optional fields.
- Documented configuration keys and the Markdown/frontmatter memory format.
  Existing committed memories and persistent recovery state must retain their
  identity, provenance and interpretation across compatible upgrades. Any
  required migration must be documented before it is applied.

Module internals, names beginning with `_`, internal planner/reviewer protocols,
development harnesses and exact natural-language model wording are outside this
scope. An internal protocol change may fence pending model responses and require
an explicit recovery action; it must not silently reinterpret saved work, erase
unresolved outcomes or reset consumed request budgets.

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
