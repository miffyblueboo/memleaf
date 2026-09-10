# P2 prompt/input slimming

This experiment starts from the P1 corrected baseline and changes only model input representation and prompt wording. It does not change Gate candidate semantics, evidence validation, coverage rules, writer commit semantics, model-call stages, retry caps, or review requirements.

## User priority

API price is non-blocking. Provider + exact model remain A/B control variables, but tariff verification and monetary caps are not prerequisites for P2 work. Primary goals are lower prompt/input token use and less prompt-induced unnecessary reasoning.

All measurements below are synthetic **UTF-8 byte proxies with zero model calls**. They are not tokenizer counts and are not real-model latency claims.

## Phase 1: canonical Gate conversation text

Before P2, each Gate batch serialized the full visible turn-event content and then serialized the same conversation text again inside Evidence units. Multi-batch turns repeated the complete event body for every Gate call.

P2 keeps event identity/timing metadata in the event envelope but makes Evidence units the single model-visible source of conversation text. Tool bodies remain excluded. Candidate/coverage/evidence-binding contracts are unchanged.

Synthetic proxy result:

- corrected-B0 Gate user input: 6,751 bytes
- P2 Gate user input: 4,193 bytes
- removed: 2,558 bytes
- reduction: 37.8907%
- user/assistant body markers: 2 occurrences -> 1 occurrence
- tool-body occurrences: 0

## Phase 2: automatic Summary comparison context

Automatic CREATE no longer receives unrelated memleaf memory bodies. Automatic UPDATE receives only the Gate-fixed memleaf target plus native comparison context. Native comparison remains available for `shadow_native_ids`; Scope background/registry remain unchanged. Explicit remember keeps the full previous related-memory context.

Synthetic proxy result after full-CI compatibility restoration:

- automatic CREATE: 6,734 -> 1,724 bytes, down 5,010 bytes (74.3986%)
- automatic UPDATE: 6,846 -> 3,764 bytes, down 3,082 bytes (45.0190%)
- CREATE retains native + Scope context and no unrelated/fixed-target memleaf body
- UPDATE retains native + the one fixed target and no unrelated memleaf body

The visible Summary section label remains backward-compatible with existing adapters; only the JSON payload inside that section is narrowed.

## Phase 3: compact, non-stepwise system prompts

The Gate and Summary system prompts were compacted by merging repeated semantic rules and removing stepwise reasoning cues such as `First enumerate...` and `First compare...`. The strict Gate output protocol/example and all tested safety/semantic contracts remain in place.

Static system-prompt result after restoring full-CI semantic anchors:

- Gate system: 8,508 -> 7,838 bytes, down 670 bytes (7.8749%)
- Summary system: 4,347 -> 4,160 bytes, down 187 bytes (4.3018%)

Product defaults remain `thinking=low` for Gate, Summary, and Compact. This phase deliberately does not switch to `disabled`; prompt wording is reduced first so the model is not instructed to perform unnecessary stepwise reasoning while preserving necessary judgment.

## Validation boundary

`static_prompt_audit.py`, `summary_prompt_audit.py`, and the P2 tests make zero model calls and protect structural/token-proxy invariants. Previously failing full-CI suites were rerun after restoring compact compatibility/safety anchors and passed before the final full matrix.

A future real-model A/B should keep provider, exact model, thinking mode, timeout, concurrency, fixture, and seed semantics fixed. API cost is optional metadata, not an acceptance gate. Real-model evidence is still required before claiming actual tokenizer token, latency, or semantic-quality improvements.
