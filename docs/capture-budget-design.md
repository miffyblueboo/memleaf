# Capture budget compatibility

Automatic extraction uses only the current turn's visible user input and final assistant reply. Historical context, intermediate assistant messages, tool calls/results and hidden payloads are not new memory sources.

The shared evidence-budget helper remains available for legacy parsing and standalone provider compatibility. Its record, UTF-8 body and aggregate limits are implementation bounds, not permission to capture external content. Automatic capture discards tool evidence before inbox persistence, and processing excludes raw evidence in older inbox files. Legacy `bounded` or `metadata` configuration cannot restore it.

Synthetic tests retain deterministic budget, redaction, overflow and idempotency checks for that helper. Runtime acceptance verifies excluded payloads never reach model prompts or cause partial/deferred work. A final visible reply can support multiple independently useful memories through exact source bindings; repeated existing facts remain comparison-only and should produce NO_CHANGE.

Model context capacity is separate from capture compatibility. Visible messages still consume prompt capacity, and model failure must preserve retryable input rather than silently dropping it. Synthetic checks, live model results, installation and release are reported separately.
