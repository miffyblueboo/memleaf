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

The shared capture budget allows 64 source records, 32 KiB of UTF-8 text per body,
128 KiB of total body text, and 320 characters per metadata field. Loss markers
have a separate allowance of 64 call identities plus one aggregate marker, with
fixed diagnostic bodies. Reapplying normalization does not reduce an
already bounded inventory or count existing omissions again. Oversized eligible
evidence remains incomplete; its prefix is never promoted into a complete fact.
Policy-excluded observations are marked
retention=metadata, have no content, and are not retried as missing evidence.
No later relaxation recreates discarded original content.

These are source-retention limits, not a guarantee that every configured model
can process the resulting prompt. See [capture budget design](capture-budget-design.md)
for the ingestion boundary, model-capacity limitation and acceptance contract.

Document bodies follow the selected `tool_evidence_mode`; ordinary structural
file arguments do not require `include_attachments=true`. Bodies explicitly
identified as attachments (`source_type=attachment`, or an `attachment_id`
argument in the host adapters) require `include_attachments=true` and bounded
mode. A path that happens to point at an attachment cannot be classified as an
attachment from the path alone. HostRuntime and the standalone Hermes adapter
use the same tested contract: path/file/file_id/file URI means `document`, while
an explicit `attachment_id` means `attachment`, including bounded nesting. This
is not a claim to identify every file hidden behind arbitrary terminal commands
or undocumented remote tools. Direct callers must truthfully identify document
or attachment evidence with the matching `source_type`.

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
