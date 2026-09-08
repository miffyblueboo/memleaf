# Evidence retention

Memleaf extracts memories from the visible conversation only. User messages and
complete Agent messages are the source boundary. Tool results, mail bodies,
documents, attachments, retrieved snippets, and terminal output are not
captured as memory evidence and are never sent to the extraction model.

The `tool_evidence` argument remains accepted at compatibility boundaries so an
older host can call a newer Vault. It has no retention authority: the effective
policy drops it before pending-cache, inbox, planning, or model input. A visible
Agent message may report a result in its own words; that message is evaluated as
conversation text, while the underlying tool result remains unavailable.

## Effective policy and compatibility

New Vaults write this explicit capture policy:

```yaml
capture:
  visible_messages_only: true
  tool_evidence_mode: off
  include_attachments: false
  redact_secrets: true
```

`capture_policy_status` always reports the effective boundary as:

```json
{
  "tool_evidence_mode": "off",
  "include_attachments": false,
  "body_retention": "off"
}
```

Older configurations remain readable. `bounded`, `metadata`, `off`, and the
legacy `include_tool_output` boolean are validated and normalized for migration,
but none of them can re-enable tool bodies. A legacy `true` or an explicit
`bounded` value is therefore readable configuration state, not permission to
collect raw evidence. Invalid modes and non-boolean flags fail closed.

Loading a configuration does not rewrite it. Normal saves may make the
compatibility fields explicit, but they do not restore discarded source text.

## Redaction and budget helpers

The redaction and UTF-8 budget functions remain deterministic defensive helpers
for callers that validate legacy or untrusted records. They may return a bounded
or redacted in-memory value for that pure-function contract. Their result is not
an admissible memory source: capture applies the conversation-only policy after
validation and returns no tool evidence.

Structural `path`, `file`, `file_id`, `file://`, and explicit `attachment_id`
arguments may still be classified for diagnostics. Classification never grants
permission to retain the referenced body. A path that happens to point at an
attachment cannot authorize attachment content.

## Ingress and legacy data

- Direct `Memleaf.capture(..., tool_evidence=...)` stores only the visible event.
- `HostRuntime.observe_external_tool` drops tool, mail, document, and attachment
  bodies before writing its pending state.
- The standalone Hermes adapter may keep its compatibility call shape, but Core
  applies the same no-body boundary.
- A legacy inbox can still be parsed for migration and audit compatibility. Its
  old `tool_evidence` fields are excluded from admission and model prompts; they
  do not become retryable or partial evidence.

The policy does not silently rewrite an existing user's inbox or knowledge. An
old raw field may remain in an already captured file until the user explicitly
forgets or cleans it, but processing never treats that field as source text.

## Processing status

Intentional tool-evidence exclusion is a complete policy decision. It reports
`external_evidence_status=disabled`, with zero retained body bytes and zero
unresolved evidence caused by the exclusion. It must not create a `partial`
coverage result, a deferred inbox turn, or a retry request. Partial coverage and
deferred recovery remain available for failures in the visible conversation
pipeline and model validation.

No later policy change reconstructs a body that was discarded at capture. The
host must not re-read a mail server, document, attachment, or terminal result to
make a Gate decision pass.

## Verification

The contract tests cover:

- default, legacy, and explicit bounded configuration compatibility;
- effective `off`/disabled status and invalid-setting rejection;
- pure redaction and UTF-8 budget behavior;
- direct capture and HostRuntime dropping raw bodies;
- legacy inbox bodies remaining outside model input and partial accounting;
- admission of visible user and Agent messages only;
- MCP, CLI, and Hermes status reporting the disabled policy.
