# Incremental items planner: G3a executable preview

This is the first, **non-mutating** G3 increment of the v4.1 design. It adds a
single-request prompt, immutable planning context and five-action compiler. It
is not a second production writer, a released feature, or evidence that live
Flash semantics have passed acceptance.

## Enabled and deliberately not enabled

`Memleaf.preview_incremental()` prepares one system/user request from a complete
captured turn and bounded current-memory context. Given a model response and the
original snapshot token, it recompiles only if sources/targets are unchanged.
It makes **zero model calls and zero knowledge/history/processing-ledger writes**.
Callers may use its request in an explicitly authorized isolated evaluation.

Normal `process()`, `remember()`, MCP tools, the model route and existing request
budgets remain unchanged. There is no config switch silently routing automatic
traffic here. The preview is a Python API, not an additional MCP tool. It has no
new credentials, provider integration, automatic repair or background process.

Native-memory read-only comparison is now connected by the G3d follow-up; see
[incremental-native-comparison.md](incremental-native-comparison.md). Commit and
bounded model dispatch are separate opt-in APIs from G3b/G3c. Preview itself
still makes no writes or model calls. Default activation, selective unresolved
recovery and live host/model acceptance remain staged.

## Public workflow

Use a temporary Vault or an authorized existing source. No model is called by
either operation below:

```python
# Assumes these exact messages have already been captured in this Vault.
request = service.preview_incremental(
    source="hermes", session_id="session", turn_id="turn",
    scope="project:Atlas", priority_memory_ids=["mem-existing"],
)
# Send request["request"] only as part of an explicitly authorized evaluation.
# Or use deterministic fixture JSON when testing the execution contract.
result = service.preview_incremental(
    source="hermes", session_id="session", turn_id="turn",
    scope="project:Atlas", priority_memory_ids=["mem-existing"],
    expected_snapshot=request["snapshot_id"], response=response_json,
)
```

The same arguments must be used in both calls. A snapshot ID is a comparison
checksum, **not** a bearer permission, persisted work ID or write authorization.
Changes to source revision, relevant memory content, explicit boundary or candidate
mapping reject the result. Reading a memory and changing only its hit counter does
not invalidate the semantic snapshot. There is no hidden stale-result repair.

No caller should feed returned `memory` directly to `write_memory()`: execution
still needs fresh source/target validation and the existing common commit boundary.
CREATE proposals have no permanent ID. UPDATE proposals carry the original
`target` and `expected_revision`, but that revision must be rechecked at commit.

## Model protocol

The version is `incremental-items-v1`; only `{"items": [...]}` is accepted.

| action | Required fields besides action/evidence | Behavior |
|---|---|---|
| CREATE | memory.type/scope/title/body; todo also status | Independent new assertion; Core defaults validity=valid |
| UPDATE | target, nonempty patch | Apply changes to the complete old state; omission preserves |
| NO_CHANGE | target | Existing target already covers the observation; no replacement body |
| NO_MEMORY | none | Whole source block needs no new memory or maintenance |
| DEFERRED | reason, need | missing_identity / missing_context / conflict, with a concrete gap |

References are distinct `eN`, `mN`, `sN` namespaces. Unknown values never fall back
to another namespace, ordinal, title or nearest target. Whitespace/case normalization
is allowed only where unambiguous. Empty items do not settle new evidence.

UPDATE.patch supports title, body, scope, status, assignee, waiting_on, deadline,
and validity. An absent field is unchanged. Explicit nullable responsibility
fields use null; deadlines use an explicit `clear:true`. Type, IDs, versions,
source lists, timestamps and arbitrary custom fields are not model-editable.
Existing custom metadata is retained in the compiled full-state proposal.

All UPDATE rows for one target are a group. Conflicting values or one invalid,
stale or unauthorized member reject the **whole target group**; other groups and
valid NO_MEMORY decisions remain in the result. Field disjointness is not proof
of semantic independence. The compiler does not use keyword rules to claim that
arbitrary prose and status are semantically consistent.

`request_kind=explicit_remember` grants retention for the supplied selected new
input in the low-level snapshot builder, not authorization to invent facts or
cross scope. The public captured-turn preview uses automatic intent and returns a source-ref
map. The opt-in `remember_incremental()` wrapper selects from those immutable
refs and carries the actual retention request to this same compiler. The legacy
text `remember()` route is not switched.

## Evidence, time, lifecycle and scope

The first preview uses whole visible messages as evidence blocks. It includes the
previous available turn and available subsequent context as `context`, not new
assertions. Incomplete/missing known subsequent raw context is blocked; no LLM
summary is invented as replacement evidence. Top-K is never a proof of perfect
identity recall. Message-block coverage is not per-fact semantic recall.

Required targets are pinned. Other candidates use the existing pure lexical
retrieval functions, with a round-robin budget across source messages. Search can
consider closed and retracted heads and cross-type/cross-scope expressions, while
`writable` and the explicit write boundary separately constrain changes. Neither
scope is guessed from cwd nor aliases merged by approximate spelling. Registry
updates/new canonical scope creation are not committed by preview.

The model selects a deadline phrase; Core checks its quoted source and converts
only its calendar meaning. The source-local day, not UTC/capture/process date,
anchors relative expressions. Missing/conflicting dates preserve due_text and an
unresolved marker. A changed unresolved deadline invalidates the old calendar
value; an ordinary progress update retains the old value. Email dates are not
scanned and auto-filled as deadlines. This does **not** prove that a model-selected
ISO date was semantically a deadline: that remains a live-model acceptance item.

`field_basis` is self-contained source metadata, never eN or a list offset.
Provably older conflicting field changes are rejected. Explicit reopen/restore
requires a later comparable basis; legacy heads lacking it produce a visible
unverified-time issue rather than a fabricated business time. Unknown completion
time is not replaced with observation time. Future effective state changes are
blocked, while a fact describing a plan can still be proposed. No timer executes
future plans. This is bounded observation-order checking, not a bitemporal database.

Retraction keeps the same ID and an empty current assertion body; restore requires
validity=valid plus current body and newer evidence. These are proposals only:
archiving, committed lifecycle transitions and forget cancellation still belong
to the commit integration. Explicit audit identity is not a current fact.

## Errors and acceptance

Top-level invalid JSON, duplicate keys, non-finite constants, invalid items or
oversized responses are request errors. Once JSON is valid, row errors are local.
The compiler never calls a repair model. Technical failures are issues rather
than model-authored DEFERRED. Unlocated errors remain visible even when all known
blocks have other valid decisions. Complete coverage does not assert semantic
quality, a successful write, or permanent evidence settlement.

The input/response cap is 128 KiB and 64 source blocks/result rows, with at most
20 targets (public default 12). These are conservative staging limits, not measured
Flash context limits. Necessary content is never silently truncated. Malformed
knowledge or duplicate current IDs block this diagnostic preview rather than
allowing false empty context. Existing production retrieval behavior is untouched.

The staged prompt retains a concrete JSON example and adds only the v4.1 explicit
retention and validity semantics. Fixed text uses Unicode codepoint count, UTF-8
bytes, LF newlines and a trailing LF; fences and dynamic inputs are excluded.
Size is checked by the public tests against half of the v3.0 baseline. The old
production prompts and model settings are not replaced.

Run the source contract tests with:

```sh
PYTHONPATH=src python -m unittest discover -s tests_public -p 'test_*.py'
```

These tests cover the parser/compiler, calendar conversion, immutable snapshots,
read-only service boundary and old 110 regressions. They are not live semantic
or native Hermes/Windows/macOS acceptance. Before G3 production activation, finish
commit/recovery integration and bounded dispatch, test the installed artifacts,
and run the agreed isolated multi-turn Flash acceptance set.
