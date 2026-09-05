# Shared tool-evidence retention — refactor phase 3

One deterministic policy governs direct capture, HostRuntime hooks and the copied
Hermes MemoryProvider. It grants no semantic write permission and adds no service,
runtime dependency, storage engine or model call.

## Modes and compatibility

| Effective setting | Bodies | Observation metadata |
| --- | --- | --- |
| bounded | Redacted, bounded | Retained |
| metadata | Excluded | Retained |
| off | Excluded | Excluded |

New Vaults explicitly choose bounded; existing files without a mode preserve the
legacy boolean (false/absent -> metadata, true -> bounded). An explicit new mode
wins over the legacy boolean. Invalid strings and non-boolean flags fail closed.
Loading config does not rewrite it. Normal saves make the effective mode explicit.

The inherited limits remain eight records, 2,000 characters per body, and 320 per
metadata field. Oversized eligible evidence remains incomplete; its prefix is
never promoted into a complete fact. Policy-excluded observations are marked
retention=metadata, have no content, and are not retried as missing evidence.
No later relaxation recreates discarded original content.

Document/attachment bodies require include_attachments=true and bounded mode.
HostRuntime and the standalone Hermes adapter classify structural file arguments
using the same tested contract (path/file_path/file_id/attachment_id/file URI,
including bounded nesting). This is not a claim to identify every file hidden
behind arbitrary terminal commands or undocumented remote tools. Direct callers
must truthfully identify document evidence with source_type=document.

## Lifecycle

- Pre-capture recording permission takes precedence over every retention mode.
- New host observations and pending-cache reads obey the effective mode.
- Core capture validates and reapplies it before inbox persistence.
- New planning calls filter evidence from already captured inbox events too,
  without mutating the original event identity or frozen-plan input checksum.
- Successful capture consumes only evidence verified in inbox under the same
  policy; failed capture does not discard eligible observations.
- No retrospective deletion of knowledge/history/inbox is implied. Frozen plans
  already prepared before a policy change recover via their existing contract.
  Explicit forget coordinates cancellation; a retention toggle is not forget.
- Old abandoned host sessions are not scanned by a daemon or globally purged.
  Current sessions consume their caches through the normal lifecycle.

Metadata is not anonymization: identifiers/titles may remain sensitive. Redaction
is best effort and local plaintext backups remain the user's responsibility.

## Verification

Contract tests cover legacy/no-capture config compatibility, round trips, explicit
precedence, denied documents, shared Hermes/core classification, bounded redaction,
cache-policy changes, direct and MCP fields, no model input after tightening,
no false coverage/retry from deliberate exclusion, and no reconstruction on opt-in.
Existing general evidence, native memory, lifecycle, retrieval and packaging tests
remain part of the full suite; no live model credentials are required here.
