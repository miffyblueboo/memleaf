# Gate evidence boundary

## Design decision

Keep Markdown as the permanent-memory source of truth, the shared Vault,
`_index/` / `_state/` separation, scope-directed retrieval, and the separate
Gate and Summarize stages. Do not repair extraction failures by accepting
unverified external text or by adding business-specific classifiers.

The Gate owns semantic decisions. The host owns visible-message capture and
accounting. A model must not be asked to re-decide a physical boundary that the
host already knows, such as whether a tool, mail, document, or attachment body
is outside the conversation.

## Input and accounting responsibilities

The local inventory may read a complete inbox event for audit and replay, but the
model-facing projection contains only visible conversation messages:

| Source | Model evidence | Local handling |
| --- | --- | --- |
| Visible user text, including questions, examples, and quotations | Yes | Gate decides future value and coverage |
| Complete visible Agent message | Yes | Gate evaluates the report as conversation text |
| Tool result, mail body, document, attachment, or terminal output | No | Discard at capture; legacy fields remain non-admissible |
| Assistant call metadata and tool names | No | Control/audit metadata only |
| Retrieved memleaf/native memory | No | Context only; no new evidence |
| Missing, failed, or incomplete external observation | No | Not a partial source; no retry from exclusion |

The physical boundary is therefore `source_role in {user, assistant}`. User
syntax such as a question, quotation, or example remains visible so the Gate can
make a source-neutral semantic decision. An Agent report is one complete visible
message and is represented as `assistant_report`; the underlying tool output is
never substituted for it.

An exact quote from visible text proves provenance, not truth or future value.
Conversely, a tool call ID, digest, subject, sender, file path, or attachment ID
cannot authorize a memory write. Context from related memory can explain a
pronoun, but it is not a bindable source unit.

## One Gate contract

The documented response contains `candidates`, `coverage`, and
`evidence_bindings`. Coverage accounts for every model-visible user or Agent
unit, including an explicit decision that no candidate is warranted. An empty
candidate list is not by itself proof of complete coverage when visible units
exist.

Candidates with exact `evidence_bindings` may omit `evidence_event_ids`. Core
validates the bound unit, quote, role, and span before deriving source event IDs.
Explicit event IDs remain strict constraints and are never silently replaced.
Unique exact quotes may omit numeric offsets; Core computes their positions
without asking the model to count them.

A single visible message may support several independently completable or
updateable requests. Shared coordination does not merge those requests into one
memory. Each candidate should bind its own passages and any necessary shared
context. Physical unit coverage alone does not prove semantic completeness.

Validated bindings are not subjected to whole-message keyword rejection for
negation, completion or ownership. The final semantic review judges those
properties for the candidate. It also receives the same current visible
message as `source_context` when needed to interpret a narrow quote. That
context may qualify or negate a bound claim, but cannot authorize adding
unbound facts. Tools and older conversation messages remain excluded.

The three empty lists describe a complete response only when there are no
model-visible units. With visible units, no admission still needs explicit
`NO_CHANGE` or `DEFERRED` accounting. Legacy candidate-only responses use the
same binding checks. Unknown unit IDs, contradictory coverage, invalid spans,
and unauthorized sources remain errors.

The Gate does not receive raw tool evidence, so excluding a tool, mail,
document, or attachment body cannot create an unresolved unit or `partial`
coverage. Model failures and invalid visible-message coverage retain their
existing retry and failure behavior.

## Visible-message batches

Visible user and Agent messages are sent in ordered batches subject to the Gate
unit and serialized-input limits. A complete visible message is never silently
replaced with an external result. Every unit retains its event identity and
character span. Candidate IDs are namespaced by batch before they enter the turn
audit; unit IDs and source spans remain global and immutable.

The host waits for all visible-message Gate batches before running admission or
summarization, so a hard failure in a later batch cannot commit an earlier
batch's proposals. Each batch receives the current visible conversation and the
same bounded related-memory and scope context. Cross-batch CREATE proposals with
the same validated type and scopes still use the bounded reconciliation step.

## Capture-policy boundary and legacy recovery

The effective capture status is always `tool_evidence_mode=off`,
`include_attachments=false`, and `body_retention=off`. Legacy `bounded`,
`metadata`, and `include_tool_output` settings remain readable for migration but
cannot re-enable raw bodies. `tool_evidence` remains an accepted compatibility
field and is discarded before pending state, inbox persistence, or model input.

Legacy inbox files may contain old tool-evidence fields. The parser can expose
them for compatibility diagnostics, but admission filters them out and the Gate
never sees their body text. Intentional exclusion is reported as disabled
external evidence with zero retained body bytes; it does not create partial
coverage, a deferred inbox turn, or a retry deadline.

Do not reconstruct an excluded body from an Agent summary, query a mail server,
open a document or attachment, or execute a terminal command merely to make a
Gate decision pass. Existing raw files are not silently rewritten by this
policy; explicit forget/cleanup remains a separate operation.

## Diagnostics and recovery

Preserve the existing failure category, retry bound, and watermark behavior for
visible-message and model failures. Record only an allowlisted evidence-check
identifier for a failing constraint. Diagnostics must not contain raw model
responses, message text, credentials, tool bodies, or arbitrary exception
strings.

An unknown unit reference identifies the exact response field, such as
`coverage[2].unit_id` or `evidence_bindings[0].claims[1].unit_id`. Persist only
the field path, value type/length/digest, and expected-set count/digest. The
invalid value and legal ID list do not belong in normal logs or failed state.

A bounded retry receives the failed constraint, exact field path, and legal IDs
from the same immutable visible-message inventory used by the validator. These
IDs constrain provenance; they are not a suggested semantic decision. The host
must not substitute a nearby ID, guess a source, or re-enable external capture.

A failed Gate cannot advance the turn watermark or create a cleanup deadline. A
successful no-change decision can advance the watermark and start normal
retention. Replaying a completed operation remains idempotent.

## Verification

- Visible user and Agent messages remain model evidence, including questions and
  quoted text; no keyword rule decides their future value.
- Tool, mail, document, attachment, and terminal bodies are absent from direct
  capture, HostRuntime pending state, and model prompts.
- Legacy inbox tool bodies cannot authorize a write or create partial coverage.
- Redaction and UTF-8 budget helpers remain deterministic pure functions but do
  not grant capture permission.
- Invalid unit IDs, duplicate coverage, invalid reasons, invalid spans, and
  binding/coverage conflicts produce safe distinguishable diagnostics.
- Failure, retry, watermark, cleanup eligibility, and replay are verified in an
  isolated Vault without changing an existing user's Vault.

A successful replay is evidence about that new run. If an earlier raw response
was not retained, its precise invalid value cannot be recovered from an
`unknown_unit` category alone. Verify recovery with controlled invalid visible
coverage and binding references, and distinguish those results from reproducing
an earlier external observation.
